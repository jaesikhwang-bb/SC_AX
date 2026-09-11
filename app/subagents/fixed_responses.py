"""서브에이전트 단계의 사용자 노출 고정답변을 한곳에서 관리한다.

마스터 EXCEPTION과는 별개다. 마스터는 정상 AGENT로 분류하고 서브에이전트가
특정 세부 시나리오를 선택한 뒤, 이 표에 등록된 응답을 MCP 호출 없이 반환한다.

조회형 MCP가 업무코드 1001을 반환한 경우의 문구도 세부 시나리오별로 이 파일에서
관리한다. 질문 문장을 Python 키워드로 비교하지 않고 LLM이 선택한 안정적인
``detail_scenario_code``를 키로 사용하므로, 질문 표현이 늘어나도 코드 분기문을
추가할 필요가 없다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from app.mcp.models import MCP_NO_DATA_MESSAGE


UNSUPPORTED_FEATURE_MESSAGE = "아직은 지원하지 않는 기능입니다."

# RAG 검색 결과가 없거나 문서만으로 답변할 수 없을 때의 사용자 문구다.
# QUALIFICATION/RP 전용 파이프라인은 아래 코드로 문구를 조회한다.
RAG_FIXED_RESPONSES: dict[tuple[str, str], str] = {
    (
        "QUALIFICATION",
        "NO_DOCUMENTS",
    ): (
        "질문과 관련된 자격기준 업무 문서를 찾지 못했습니다. 질문을 조금 더 "
        "구체적으로 입력해 주세요."
    ),
    (
        "QUALIFICATION",
        "NOT_ANSWERABLE",
    ): (
        "조회된 자격기준 업무 문서만으로는 질문에 정확히 답변할 수 없습니다. "
        "질문을 구체적으로 입력하거나 관련 업무 담당 채널을 통해 확인해 주세요."
    ),
    (
        "RP",
        "NO_DOCUMENTS",
    ): (
        "질문과 관련된 RP 업무 문서를 찾지 못했습니다. RP 업무명과 확인할 "
        "기준을 구체적으로 입력해 주세요."
    ),
    (
        "RP",
        "NOT_ANSWERABLE",
    ): (
        "조회된 RP 업무 문서만으로는 질문에 정확히 답변할 수 없습니다. 질문을 "
        "구체적으로 입력하거나 RP 업무 담당 채널을 통해 확인해 주세요."
    ),
}


@dataclass(frozen=True)
class SubagentFixedResponse:
    """하나의 세부 시나리오가 반환할 모집인 구분별 고정답변 설정.

    세부 시나리오는 모집인 유형마다 복제하지 않는다. 같은 detail에서
    ``recruitment_org_type_code``가 11/12/13이면 해당 문구를 선택하고, 코드가
    없거나 등록되지 않은 값이면 ``default_message``를 안전한 공통 답변으로 쓴다.
    """

    default_message: str
    messages_by_recruitment_org_type: Mapping[str, str] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        default_message = str(self.default_message).strip()
        if not default_message:
            raise ValueError("SubagentFixedResponse.default_message는 비어 있을 수 없습니다.")

        normalized_messages: dict[str, str] = {}
        for raw_code, raw_message in self.messages_by_recruitment_org_type.items():
            code = str(raw_code).strip()
            message = str(raw_message).strip()
            if not code:
                raise ValueError("유치조직구분코드는 비어 있을 수 없습니다.")
            if not message:
                raise ValueError(
                    f"유치조직구분코드 {code}의 고정답변은 비어 있을 수 없습니다."
                )
            normalized_messages[code] = message

        object.__setattr__(self, "default_message", default_message)
        object.__setattr__(
            self,
            "messages_by_recruitment_org_type",
            normalized_messages,
        )

    def message_for(
        self,
        recruitment_org_type_code: str | None,
        *,
        user_name: str | None = None,
    ) -> str:
        """모집인 구분 문구를 선택하고 선택적으로 직원명을 치환한다.

        고정답변에 ``{user_name}``을 적은 경우에만 직원정보 MCP에서 조회한
        이름으로 바뀐다. 다른 중괄호 표현은 건드리지 않아 문구 커스터마이징이
        안전하며, 이름이 없으면 빈 문자열로 치환한다.
        """

        normalized_code = str(recruitment_org_type_code or "").strip()
        message = self.messages_by_recruitment_org_type.get(
            normalized_code,
            self.default_message,
        )
        return message.replace("{user_name}", str(user_name or "").strip())


# 운영 커스터마이징 지점: 키는 (에이전트 코드, 세부 시나리오 코드)다.
SUBAGENT_FIXED_RESPONSES: dict[tuple[str, str], SubagentFixedResponse] = {
    (
        "PERFORMANCE_FEE",
        "FEE_PAYMENT_DATE_GUIDANCE",
    ): SubagentFixedResponse(
        default_message=(
            "수수료는 매월 16일에 지급됩니다. "
            "(주말·공휴일인 경우 직전 영업일에 지급)"
        ),
    ),
    (
        "PERFORMANCE_FEE",
        "FEE_FAX_UNAVAILABLE_GUIDANCE",
    ): SubagentFixedResponse(
        default_message=(
            "수수료 내역은 팩스 발송 불가합니다. "
            "자세한 내용은 지점을 통해 확인해주세요."
        ),
    ),
    (
        "PERFORMANCE_FEE",
        "FEE_LONG_TERM_HISTORY_GUIDANCE",
    ): SubagentFixedResponse(
        default_message="수수료 자세한 내용은 지점을 통해 확인해 주세요.",
    ),
    (
        "PERFORMANCE_FEE",
        "OTHER_RECRUITER_DATA_REQUEST",
    ): SubagentFixedResponse(
        default_message=(
            "로그인한 모집인 본인 이외의 다른 모집인 정보는 조회할 수 없습니다."
        ),
    ),
    (
        "PERFORMANCE_FEE",
        "CUSTOMER_DISPOSAL_REASON_REQUEST",
    ): SubagentFixedResponse(
        default_message=(
            "고객 폐기사유는 확인 불가합니다. "
            "자세한 내용은 지점을 통해 확인해주세요."
        ),
    ),
    (
        "PERFORMANCE_FEE",
        "CUSTOMER_SALES_PERFORMANCE_REQUEST",
    ): SubagentFixedResponse(
        default_message=(
            "고객 매출건은 지점을 통해 확인해주세요."
        ),
    ),
    (
        "PERFORMANCE_FEE",
        "CUSTOMER_PERFORMANCE_DETAIL_REQUEST",
    ): SubagentFixedResponse(
        default_message=(
            "특정 고객의 실적·환산·미등록 관련 상세정보는 확인할 수 없습니다. "
            "자세한 내용은 지점을 통해 확인해 주세요."
        ),
    ),
    (
        "QUALIFICATION",
        "PROVISIONAL_DISPOSITION_GUIDANCE",
    ): SubagentFixedResponse(
        default_message=(
            "고객 가처분 관련 문의는 확인 불가합니다. "
            "자세한 내용은 지점을 통해 확인해주세요."
        ),
    ),
    ("PERFORMANCE_FEE", "CUSTOMER_WITHDRAWAL_REQUEST"): SubagentFixedResponse(
        default_message="고객 탈회 이력은 확인 불가합니다. 자세한 내용은 지점을 통해 확인해주세요.",
    ),
    ("PERFORMANCE_FEE", "CUSTOMER_CONVERSION_REQUEST"): SubagentFixedResponse(
        default_message="고객별 환산은 확인 불가합니다. 자세한 내용은 지점을 통해 확인해주세요.",
    ),
    ("PERFORMANCE_FEE", "CUSTOMER_UNREGISTERED_IDENTITY_REQUEST"): SubagentFixedResponse(
        default_message="고객 내역은 확인 불가합니다. 자세한 내용은 지점을 통해 확인해주세요.",
    ),
    ("PERFORMANCE_FEE", "FEE_DETAIL_GUIDANCE"): SubagentFixedResponse(
        default_message="수수료 자세한 내용은 지점을 통해 확인해 주세요.",
    ),
    ("RP", "RP_APPLICATION_CANCEL_GUIDANCE"): SubagentFixedResponse(
        default_message="고객 RP 신청건은 취소 불가합니다. 자세한 내용은 지점을 통해 확인해주세요.",
    ),
    ("RP", "RP_CUSTOMER_PAYMENT_GUIDANCE"): SubagentFixedResponse(
        default_message="고객 자동납부 내역은 확인 불가합니다. 자세한 내용은 지점을 통해 확인해주세요.",
    ),
    ("RP", "RP_POST_ISSUANCE_APPLICATION_GUIDANCE"): SubagentFixedResponse(
        default_message="RP신청은 당사 대표번호 (1588-8700)로 고객이 직접 신청",
    ),
}


# ---------------------------------------------------------------------------
# MCP 업무코드 1001(NO_DATA) 고정답변 커스터마이징
# ---------------------------------------------------------------------------
# 운영자는 아래 표의 문자열만 바꾸면 된다. 등록되지 않은 새 세부 시나리오는
# MCP_NO_DATA_MESSAGE("조회 결과가 없습니다.")를 안전한 기본값으로 사용한다.
# 모집인 구분(11/12/13)마다 문구가 달라지면 SUBAGENT_FIXED_RESPONSES와 동일하게
# messages_by_recruitment_org_type을 채우면 된다.
SUBAGENT_NO_DATA_RESPONSES: dict[
    tuple[str, str],
    SubagentFixedResponse,
] = {
    (
        "PERFORMANCE_FEE",
        "PERFORMANCE_SUMMARY_TOTAL",
    ): SubagentFixedResponse(default_message="조회된 실적이 없습니다."),
    (
        "PERFORMANCE_FEE",
        "COMPOSITE_CONVERSION_SCORE",
    ): SubagentFixedResponse(default_message="복합환산 실적이 없습니다."),
    (
        "PERFORMANCE_FEE",
        "COMPOSITE_CONVERSION_EXCLUDED",
    ): SubagentFixedResponse(default_message="환산 미반영건이 없습니다."),
    (
        "PERFORMANCE_FEE",
        "UNREGISTERED_MEMBER_SUMMARY",
    ): SubagentFixedResponse(default_message="미등록건이 없습니다."),
    (
        "PERFORMANCE_FEE",
        "DISPOSAL_FEE_SUMMARY",
    ): SubagentFixedResponse(default_message="폐기·회입 수수료 내역이 없습니다."),
    (
        "PERFORMANCE_FEE",
        "FEE_ITEM_DETAILS",
    ): SubagentFixedResponse(default_message="수수료 지급내역이 없습니다."),
    (
        "PERFORMANCE_FEE",
        "WITHHOLDING_TAX",
    ): SubagentFixedResponse(default_message="원천징수 내역이 없습니다."),
    (
        "RP",
        "APARTMENT_RP_LIST",
    ): SubagentFixedResponse(
        default_message=(
            "조회된 데이터가 없습니다."
        )
    ),
    (
        "RP",
        "COMPOSITE_CONVERSION_SCORE",
    ): SubagentFixedResponse(default_message="복합환산 실적이 없습니다."),
    (
        "RP",
        "COMPOSITE_CONVERSION_EXCLUDED",
    ): SubagentFixedResponse(default_message="환산 미반영건이 없습니다."),
    # RAG MCP 자체가 업무코드 1001을 반환하면 RAG output handler까지 도달하기
    # 전에 AnswerService가 아래 문구를 선택하며 문서 배열은 전달하지 않는다.
    (
        "QUALIFICATION",
        "NEW_MEMBER_QUALIFICATION",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("QUALIFICATION", "NO_DOCUMENTS")],
    ),
    (
        "QUALIFICATION",
        "FOREIGNER_QUALIFICATION",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("QUALIFICATION", "NO_DOCUMENTS")],
    ),
    (
        "QUALIFICATION",
        "MINOR_QUALIFICATION",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("QUALIFICATION", "NO_DOCUMENTS")],
    ),
    (
        "QUALIFICATION",
        "FAMILY_CARD_ISSUANCE_QUALIFICATION",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("QUALIFICATION", "NO_DOCUMENTS")],
    ),
    (
        "RP",
        "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("RP", "NO_DOCUMENTS")],
    ),
    (
        "RP",
        "CITY_GAS_AUTOPAY_GUIDE",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("RP", "NO_DOCUMENTS")],
    ),
    (
        "RP",
        "SOCIAL_INSURANCE_AUTOPAY_GUIDE",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("RP", "NO_DOCUMENTS")],
    ),
    (
        "RP",
        "ELECTRICITY_TV_FEE_AUTOPAY_GUIDE",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("RP", "NO_DOCUMENTS")],
    ),
    (
        "RP",
        "SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("RP", "NO_DOCUMENTS")],
    ),
    (
        "RP",
        "PAYMENT_NOTIFICATION_SERVICE_GUIDE",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("RP", "NO_DOCUMENTS")],
    ),
    (
        "RP",
        "PAYMENT_DATE_CREDIT_PERIOD_GUIDE",
    ): SubagentFixedResponse(
        default_message=RAG_FIXED_RESPONSES[("RP", "NO_DOCUMENTS")],
    ),
}


def get_subagent_fixed_response(
    agent_code: str,
    detail_scenario_code: str,
) -> SubagentFixedResponse | None:
    """등록된 서브 시나리오 고정 답변을 반환한다."""

    return SUBAGENT_FIXED_RESPONSES.get(
        (agent_code.strip().upper(), detail_scenario_code.strip().upper())
    )


def get_subagent_no_data_response(
    agent_code: str,
    detail_scenario_code: str,
) -> SubagentFixedResponse:
    """세부 시나리오별 무데이터 답변 또는 공통 기본 답변을 반환한다."""

    return SUBAGENT_NO_DATA_RESPONSES.get(
        (agent_code.strip().upper(), detail_scenario_code.strip().upper()),
        SubagentFixedResponse(default_message=MCP_NO_DATA_MESSAGE),
    )


def get_rag_fixed_response(agent_code: str, reason_code: str) -> str:
    """RAG 전용 고정답변을 에이전트와 사유 코드로 반환한다."""

    key = (agent_code.strip().upper(), reason_code.strip().upper())
    try:
        return RAG_FIXED_RESPONSES[key]
    except KeyError as exc:
        raise KeyError(
            "등록된 RAG 고정답변이 없습니다: "
            f"agent_code={key[0]}, reason_code={key[1]}"
        ) from exc
