"""RP 조회형·RAG 세부 시나리오의 입력부터 최종 output까지 관리한다."""

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.mcp.models import McpExecutionResult
from app.mcp.scenario_runtime import ScenarioMcpHandlerContext
from app.mcp.scenarios.contracts import (
    ScenarioMcpOutput,
    ScenarioMcpOutputContext,
    ScenarioRagOutput,
    ScenarioRagOutputContext,
)
from app.mcp.scenarios.helpers import (
    keyword_list,
    month_and_reference_date,
    month_value,
    text,
)
from app.renderables import ScenarioAnswer, create_table_renderable
from app.observability import log_failure_diagnostic, logger
from app.scenario_actions import (
    ScenarioActionDefinition,
    ScenarioActionInput,
    register_scenario_action,
)
from app.streaming import split_text
from app.subagents.fixed_responses import get_rag_fixed_response


# ---------------------------------------------------------------------------
# RP RAG 커스터마이징 지점
# ---------------------------------------------------------------------------
# 공통 policy를 사용하지 않는다. RP RAG를 변경할 때는 이 파일만 확인한다.
RP_DOCUMENT_CHARACTER_THRESHOLD = 12_000
RP_FIRST_SEARCH_NUM_RESULTS = 5
RP_SECOND_SEARCH_NUM_RESULTS = 30

# 실제 MCP 계약의 output_type 값이 다르면 아래 두 줄만 변경한다.
RP_FIRST_SEARCH_OUTPUT_TYPE = "summary"
RP_SECOND_SEARCH_OUTPUT_TYPE = "full"

# 운영 RP 문서번호는 이 dict에서 직접 관리한다. 현재 값은 개발·mock에서
# 코드별 매핑을 확인하기 위한 식별자이므로 운영 연결 전에 실제 blsm_doc_id
# 값으로 교체한다. LLM parameters나 request_context에서는 문서번호를 받지 않는다.
RP_DOCUMENT_NUMBER_BY_DETAIL_CODE: dict[str, str] = {
    "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE": (
        "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"
    ),
    "CITY_GAS_AUTOPAY_GUIDE": "CITY_GAS_AUTOPAY_GUIDE",
    "SOCIAL_INSURANCE_AUTOPAY_GUIDE": "SOCIAL_INSURANCE_AUTOPAY_GUIDE",
    "ELECTRICITY_TV_FEE_AUTOPAY_GUIDE": "ELECTRICITY_TV_FEE_AUTOPAY_GUIDE",
    "SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE": "SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE",
    "PAYMENT_NOTIFICATION_SERVICE_GUIDE": "PAYMENT_NOTIFICATION_SERVICE_GUIDE",
    "PAYMENT_DATE_CREDIT_PERIOD_GUIDE": "PAYMENT_DATE_CREDIT_PERIOD_GUIDE",
}
RP_RAG_DETAIL_CODES = frozenset(RP_DOCUMENT_NUMBER_BY_DETAIL_CODE)

RP_RETRIEVAL_SCORE_THRESHOLD = 0.58
RP_RERANKING_ENABLED = True
RP_RERANKING_SCORE_THRESHOLD = 0.48
RP_RERANKING_TOP_N = 5
RP_ANSWERABILITY_CHECK_ENABLED = True
RP_NO_DOCUMENTS_ANSWER = get_rag_fixed_response("RP", "NO_DOCUMENTS")
RP_NOT_ANSWERABLE_ANSWER = get_rag_fixed_response("RP", "NOT_ANSWERABLE")

# 검색값이 없으면 주소구분 선택 후 일반 채팅창 입력을 안내한다.
# 실제 조회의 NO_DATA 답변은 fixed_responses.py의 RP/APARTMENT_RP_LIST에서 관리한다.
APARTMENT_RP_ADDRESS_REQUIRED_MESSAGE = "{address_label}를 입력해 주세요."
APARTMENT_RP_ADDRESS_RETRY_MESSAGE = "주소 또는 아파트명을 구체적으로 입력해 주세요."
APARTMENT_RP_NO_DATA_MESSAGE = "조회된 데이터가 없습니다."


def normalize_apartment_search_address(value: Any) -> str:
    """LLM이 일반 업무명을 주소로 추출해도 MCP 검색값으로 사용하지 않는다.

    의도분류를 대신하는 규칙이 아니라 조회 파라미터의 최소 검증이다.
    실제 주소·단지명은 원문을 유지하고 일반 명칭만 있는 값은 미입력으로 본다.
    """
    address = text(value)
    compact = re.sub(r"[\s?？.!！]+", "", address).casefold()
    generic_values = {
        "", "null", "none", "아파트", "아파트명", "단지", "단지명",
        "아파트단지", "관리비", "아파트관리비", "자동이체", "자동납부",
        "아파트자동이체", "아파트자동납부", "주소", "지역", "rp",
        "아파트조회", "아파트조회해줘", "아파트검색", "아파트검색해줘",
    }
    return "" if compact in generic_values else address

APARTMENT_ADDRESS_TYPE_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="RP",
        detail_scenario_code="APARTMENT_RP_LIST",
        action_code="CF_INQRDVC",
        message="주소 구분을 선택해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="address_type",
                input_code="CF_INQRDVC",
                label="주소 구분",
                input_type="choice",
                allowed_values=("1", "2"),
                validation_message="구주소는 1, 신주소는 2를 선택해 주세요.",
                guardrail_enabled=False,
            ),
        ),
    )
)

# 주소구분 선택과 별개의 안내 코드다. 실제 주소는 humanInput이 아니라
# 일반 채팅 질문으로 받아 의도분류와 주소 추출을 다시 진행한다.
APARTMENT_ADDRESS_INPUT_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="RP",
        detail_scenario_code="APARTMENT_RP_LIST",
        action_code="INPUT_ADDRESS",
        message="주소 또는 아파트명을 입력해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="address",
                input_code="INPUT_ADDRESS",
                label="주소 또는 아파트명",
                input_type="text",
            ),
        ),
    )
)

APARTMENT_NEXT_PAGE_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="RP",
        detail_scenario_code="APARTMENT_RP_LIST",
        action_code="NEXT_PAGE",
        message="다음 페이지도 보여드릴까요?",
        inputs=(
            ScenarioActionInput(
                parameter_name="next_page_choice",
                input_code="NEXT_PAGE",
                label="다음 페이지 조회",
                input_type="choice",
                allowed_values=("yes", "no"),
                validation_message="yes 또는 no를 선택해 주세요.",
            ),
        ),
        invalidate_step_codes=("APARTMENT_RP_LIST",),
        complete_on_values=("no",),
    )
)


async def rp_document_search(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    """RP 1차/2차 MCP payload를 이 함수에서 직접 구성해 호출한다."""

    query = text(context.subagent.parameters.get("rag_query")) or context.refined_query
    keywords = keyword_list(
        context.subagent.parameters.get("keywords"),
        fallback_query=query,
    )
    detail_code = context.subagent.detail_scenario_code.strip().upper()
    document_number = RP_DOCUMENT_NUMBER_BY_DETAIL_CODE.get(detail_code, "")
    if not document_number:
        raise ValueError(
            "RP 문서번호 매핑이 없습니다. "
            f"detail_scenario_code={detail_code}, "
            "수정위치=app/mcp/scenarios/rp.py:"
            "RP_DOCUMENT_NUMBER_BY_DETAIL_CODE"
        )
    filter_value = f'{{"blsm_doc_id":["{document_number}"]}}'

    if context.rag_search_pass == "SECOND":
        # -------------------------- RP 2차 조회 payload -----------------------
        step_code = "RP_DOCUMENT_SEARCH_SECOND"
        arguments: dict[str, Any] = {
            "query": query,
            "keywords": keywords,
            "query_type": "HYBRID",
            "index_name": "rp_documents",
            "columns": [
                "document_id",
                "title",
                "content",
                "source_uri",
                "updated_at",
            ],
            "output_type": RP_SECOND_SEARCH_OUTPUT_TYPE,
            "num_results": RP_SECOND_SEARCH_NUM_RESULTS,
            "filter": filter_value,
        }
    else:
        # -------------------------- RP 1차 조회 payload -----------------------
        step_code = "RP_DOCUMENT_SEARCH"
        arguments = {
            "query": query,
            "keywords": keywords,
            "query_type": "HYBRID",
            "index_name": "rp_documents",
            "columns": [
                "document_id",
                "title",
                "content",
                "source_uri",
                "updated_at",
            ],
            "output_type": RP_FIRST_SEARCH_OUTPUT_TYPE,
            "num_results": RP_FIRST_SEARCH_NUM_RESULTS,
            "filter": filter_value,
        }

    logger.info(
        "======== RP 문서 검색 실행\n"
        "세부시나리오=%s\n검색차수=%s\noutput_type=%s\n조회건수=%d\n"
        "검색질문길이=%d\n키워드개수=%d\nMCP인자키=%s\n"
        "filter와 arguments 본문로그=생략",
        detail_code,
        context.rag_search_pass,
        arguments["output_type"],
        arguments["num_results"],
        len(query),
        len(keywords),
        sorted(arguments),
    )
    return await context.call(
        step_code=step_code,
        tool_name="databricks_hybrid_search",
        arguments=arguments,
    )


def rp_document_character_count(execution: McpExecutionResult) -> int:
    """RP 검색 결과의 모든 ``content`` 본문 글자 수를 합산한다."""

    raw_result = execution.result
    if not isinstance(raw_result, Mapping):
        return 0
    rows = raw_result.get("data")
    if not isinstance(rows, list):
        return 0
    return sum(
        len(str(row.get("content") or row.get("cnk_nm") or ""))
        for row in rows
        if isinstance(row, Mapping)
    )


def rp_second_search_decision(
    executions: Sequence[tuple[str, McpExecutionResult]],
) -> dict[str, Any]:
    """RP RAG 1차 결과 전체 길이로 2차 조회 여부를 직접 결정한다."""

    detail_character_counts = {
        detail_code: rp_document_character_count(execution)
        for detail_code, execution in executions
    }
    total_character_count = sum(detail_character_counts.values())
    already_second = any(
        execution.workflow_step_code == "RP_DOCUMENT_SEARCH_SECOND"
        for _, execution in executions
    )
    all_first_searches_succeeded = bool(executions) and all(
        execution.succeeded and execution.outcome == "SUCCESS"
        for _, execution in executions
    )
    should_search_again = (
        all_first_searches_succeeded
        and not already_second
        and total_character_count > RP_DOCUMENT_CHARACTER_THRESHOLD
    )
    return {
        "detail_character_counts": detail_character_counts,
        "total_character_count": total_character_count,
        "character_threshold": RP_DOCUMENT_CHARACTER_THRESHOLD,
        "all_first_searches_succeeded": all_first_searches_succeeded,
        "already_second": already_second,
        "should_search_again": should_search_again,
    }


async def rp_document_rag_output(
    context: ScenarioRagOutputContext,
) -> ScenarioRagOutput:
    """한 요청의 모든 RP 문서 결과를 합쳐 최종 LLM을 한 번 호출한다."""

    if not context.items:
        raise ValueError("RP RAG batch에는 하나 이상의 검색 결과가 필요합니다.")
    invalid_detail_codes = [
        item.detail_scenario_code
        for item in context.items
        if item.detail_scenario_code not in RP_RAG_DETAIL_CODES
    ]
    if invalid_detail_codes:
        raise ValueError(
            "RP RAG output handler에 일반 조회 세부 시나리오가 전달되었습니다: "
            f"detail_scenario_codes={invalid_detail_codes}"
        )

    documents: list[dict[str, Any]] = []
    for item in context.items:
        logger.info(
            "======== RP 조회 결과별 전처리 시작\n"
            "세부시나리오=%s\nMCP추적ID=%s\nMCP도구=%s",
            item.detail_scenario_code,
            item.execution.request_id,
            item.execution.tool_name,
        )
        detail_documents = preprocess_rp_documents(
            execution=item.execution,
            detail_scenario_code=item.detail_scenario_code,
        )
        documents.extend(detail_documents)
        logger.info(
            "======== RP 조회 결과별 전처리 완료\n"
            "세부시나리오=%s\n현재결과유효문서=%d\n누적유효문서=%d",
            item.detail_scenario_code,
            len(detail_documents),
            len(documents),
        )
    documents = _filter_rp_documents(
        documents=documents,
        threshold=RP_RETRIEVAL_SCORE_THRESHOLD,
    )
    if not documents:
        logger.info(
            "======== RP RAG 고정답변 선택\n"
            "세부시나리오=%s\n사유=검색 임계점수 통과 문서 없음",
            [item.detail_scenario_code for item in context.items],
        )
        return ScenarioRagOutput(
            answer=RP_NO_DOCUMENTS_ANSWER,
            metadata={"reason": "no_retrieval_documents"},
        )

    second_search_used = any(
        item.execution.workflow_step_code == "RP_DOCUMENT_SEARCH_SECOND"
        for item in context.items
    )
    reranking_query = _rp_batch_query(context)
    reranking_service_enabled = bool(getattr(context.reranker, "enabled", True))
    reranking_applied = (
        second_search_used
        and RP_RERANKING_ENABLED
        and reranking_service_enabled
    )
    if reranking_applied:
        documents = await context.reranker.rerank(
            query=reranking_query,
            documents=documents,
            top_n=RP_RERANKING_TOP_N,
            score_threshold=RP_RERANKING_SCORE_THRESHOLD,
        )
        if not documents:
            logger.info(
                "======== RP RAG 고정답변 선택\n"
                "세부시나리오=%s\n사유=Reranking 임계점수 통과 문서 없음",
                [item.detail_scenario_code for item in context.items],
            )
            return ScenarioRagOutput(
                answer=RP_NO_DOCUMENTS_ANSWER,
                metadata={"reason": "no_reranked_documents"},
            )
    else:
        logger.info(
            "======== RP Reranking 생략\n세부시나리오=%s\n검색차수=%s\n"
            "정책활성화=%s\n전역서비스활성화=%s\n사유=%s",
            [item.detail_scenario_code for item in context.items],
            "SECOND" if second_search_used else "FIRST",
            RP_RERANKING_ENABLED,
            reranking_service_enabled,
            (
                "1차 검색 결과는 바로 답변 가능성 판별로 전달"
                if not second_search_used
                else "시나리오 정책 또는 전역 Reranking 서비스 비활성화"
            ),
        )

    if RP_ANSWERABILITY_CHECK_ENABLED:
        answerable = await context.answerability.is_answerable(
            query=reranking_query,
            documents=documents,
        )
        if not answerable:
            logger.info(
                "======== RP RAG 고정답변 선택\n"
                "세부시나리오=%s\n사유=LLM 문서 답변 가능성 판별 false",
                [item.detail_scenario_code for item in context.items],
            )
            return ScenarioRagOutput(
                answer=RP_NOT_ANSWERABLE_ANSWER,
                metadata={"reason": "not_answerable"},
            )

    logger.info(
        "======== RP 통합 RAG output 준비 완료\n"
        "세부시나리오=%s\n검색차수=%s\nReranking적용=%s\n"
        "최종문서개수=%d\n통합질문길이=%d",
        [item.detail_scenario_code for item in context.items],
        "SECOND" if second_search_used else "FIRST",
        reranking_applied,
        len(documents),
        len(reranking_query),
    )
    return ScenarioRagOutput(
        answer=_stream_rp_document_answer(context, documents),
        source_documents=documents,
        metadata={
            "detailScenarioCodes": [
                item.detail_scenario_code for item in context.items
            ],
            "documentCount": len(documents),
            "documentCharacterCount": sum(
                rp_document_character_count(item.execution)
                for item in context.items
            ),
            "searchPass": "SECOND" if second_search_used else "FIRST",
            "rerankingApplied": reranking_applied,
        },
    )


def preprocess_rp_documents(
    *,
    execution: McpExecutionResult,
    detail_scenario_code: str,
) -> list[dict[str, Any]]:
    """RP Databricks MCP 결과를 RP 전용 RAG 문서 구조로 변환한다.

    실제 RP 검색 MCP의 컬럼명이나 중첩 구조가 변경되면 이 함수만 수정한다.
    아파트·복합환산·자격기준 결과 파싱에는 영향이 없다.
    """

    raw_result = execution.result
    if not isinstance(raw_result, Mapping):
        raise ValueError("RP 문서 MCP structuredContent는 object 형식이어야 합니다.")
    rows = raw_result.get("data")
    if not isinstance(rows, list):
        raise ValueError("RP 문서 MCP structuredContent.data는 list 형식이어야 합니다.")

    documents: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(
                "RP 문서 MCP data 각 항목은 object 형식이어야 합니다: "
                f"index={index}"
            )
        content = str(row.get("content") or row.get("cnk_nm") or "").strip()
        if not content:
            logger.info(
                "======== RP 검색 문서 제외\n순번=%d\n사유=content 없음",
                index,
            )
            continue
        document_id = str(
            row.get("document_id")
            or f"{execution.request_id}:rp-document:{index}"
        )
        documents.append(
            {
                "document_id": document_id,
                "title": str(row.get("title") or "RP 업무 조회 문서"),
                "source": execution.tool_name,
                "content": content,
                "metadata": {
                    "detail_scenario_code": detail_scenario_code,
                    "mcp_request_id": execution.request_id,
                    "source_uri": row.get("source_uri"),
                    "updated_at": row.get("updated_at"),
                    "score": row.get("score"),
                    "matched_query": row.get("matched_query"),
                    "search_arguments": execution.arguments,
                },
            }
        )
    logger.info(
        "======== RP MCP 검색 결과 전처리 완료\n"
        "조회행=%d\n유효문서=%d\n문서본문로그=생략",
        len(rows),
        len(documents),
    )
    return documents


def _filter_rp_documents(
    *,
    documents: list[dict[str, Any]],
    threshold: float,
) -> list[dict[str, Any]]:
    """RP 전용 검색 임계점수를 적용한다."""

    selected: list[dict[str, Any]] = []
    rejected_count = 0
    for document in documents:
        metadata = document.get("metadata")
        score = metadata.get("score") if isinstance(metadata, Mapping) else None
        if (
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and float(score) >= threshold
        ):
            selected.append(document)
            continue
        rejected_count += 1
    logger.info(
        "======== RP 검색 임계값 적용 완료\n"
        "임계점수=%.3f\n입력문서개수=%d\n통과문서개수=%d\n"
        "제외문서개수=%d",
        threshold,
        len(documents),
        len(selected),
        rejected_count,
    )
    return selected


def _rp_batch_query(context: ScenarioRagOutputContext) -> str:
    """마스터 질문과 모든 RP RAG detail의 검색 질문·키워드를 묶는다."""

    request_lines = [
        (
            f"- {item.detail_scenario_code}: {item.query} "
            f"(키워드: {', '.join(item.keywords)})"
        )
        for item in context.items
    ]
    return f"{context.master_query}\n세부 요청:\n" + "\n".join(request_lines)


async def _stream_rp_document_answer(
    context: ScenarioRagOutputContext,
    documents: list[dict[str, Any]],
) -> AsyncIterator[str]:
    """RP 질문·대화이력·정제 문서를 최종 LLM에 전달해 스트리밍한다."""

    document_text = json.dumps(documents, ensure_ascii=False, default=str)
    detail_requests = [
        {
            "detail_scenario_code": item.detail_scenario_code,
            "rag_query": item.query,
            "keywords": list(item.keywords),
        }
        for item in context.items
    ]
    detail_requests_text = json.dumps(
        detail_requests,
        ensure_ascii=False,
        default=str,
    )
    history_text = json.dumps(
        [dict(item) for item in context.chat_history],
        ensure_ascii=False,
        default=str,
    )
    user_text = json.dumps(
        dict(context.request_user),
        ensure_ascii=False,
        default=str,
    )
    human_prompt = (
        f"마스터 보정 사용자 질문:\n{context.master_query}\n\n"
        f"현재 요청 사용자 정보:\n{user_text}\n\n"
        f"현재 RP 문서 검색 질문과 키워드:\n{detail_requests_text}\n\n"
        f"같은 RP 에이전트의 이전 대화 이력:\n{history_text}\n\n"
        f"검색·임계값·Reranking을 통과한 RP 업무 문서:\n{document_text}"
    )
    if context.llm is None:
        fallback = (
            "테스트 RAG 답변입니다. 조회된 RP 업무 문서를 기반으로 안내합니다. "
            f"참고 문서: {document_text}"
        )
        for chunk in split_text(fallback):
            yield chunk
        return

    logger.info(
        "======== RP 최종 답변 LLM 스트리밍 시작\n"
        "코드위치=app/mcp/scenarios/rp.py:_stream_rp_document_answer\n"
        "엔드포인트=%s\n모델=%s\n질문길이=%d\n문서개수=%d\n"
        "문서JSON길이=%d\n대화이력개수=%d",
        context.endpoint,
        context.model,
        len(context.master_query),
        len(documents),
        len(document_text),
        len(context.chat_history),
    )
    try:
        async for chunk in context.llm.astream(
            [
                SystemMessage(content=context.system_prompt),
                HumanMessage(content=human_prompt),
            ]
        ):
            chunk_text = _message_content_text(getattr(chunk, "content", ""))
            if chunk_text:
                yield chunk_text
    except Exception as exc:
        log_failure_diagnostic(
            stage="RP 최종 RAG 답변 LLM 스트리밍",
            code_location="app/mcp/scenarios/rp.py:_stream_rp_document_answer",
            exc=exc,
            likely_cause=(
                "GenOS LLM 연결·인증·모델 오류, 입력 문서 크기 초과 또는 "
                "스트림 도중 연결 종료"
            ),
            corrective_action="rp.py의 문서 전처리 결과와 GenOS LLM 설정을 확인하세요.",
            retry_count=context.max_retries,
            context={
                "detail_scenario_codes": [
                    item.detail_scenario_code for item in context.items
                ],
                "document_count": len(documents),
                "document_json_length": len(document_text),
            },
        )
        raise
    logger.info(
        "======== RP 최종 답변 LLM 스트리밍 완료\n문서개수=%d",
        len(documents),
    )


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content) if content is not None else ""


async def apartment_rp_list(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    parameters = context.subagent.parameters
    address = normalize_apartment_search_address(parameters.get("address"))
    address_type = text(parameters.get("address_type"))

    # 주소/아파트명이 최초 질문에 있어도 구·신주소 구분은 반드시 받는다.
    if address_type not in {"1", "2"}:
        APARTMENT_ADDRESS_TYPE_ACTION.require(parameters)

    # 검색값이 없을 때만 일반 채팅 message로 주소를 받는다. 이 action은 같은
    # thread_id를 유지하며, 다음 message는 다시 마스터/서브 의도분류를 통과한다.
    if not address:
        address_label = "구주소" if address_type == "1" else "신주소"
        logger.info(
            "======== RP 아파트 분기 | 주소없음 | 주소구분=%s | MCP호출=생략 | 다음=채팅주소입력",
            address_type,
        )
        APARTMENT_ADDRESS_INPUT_ACTION.request(
            message=(
                APARTMENT_RP_ADDRESS_RETRY_MESSAGE
                if context.request_context.get("rp_apartment_pending")
                else APARTMENT_RP_ADDRESS_REQUIRED_MESSAGE.format(address_label=address_label)
            ),
            context={
                "await_message_input": True,
                "apartment_address_type": address_type,
            },
        )

    is_next_page = text(parameters.get("next_page_choice")).casefold() == "yes"
    page_number = apartment_page_number(parameters)
    next_text_key = text(parameters.get("nextEtxtKeyCn")) if is_next_page else ""
    no2_next_key = text(parameters.get("no2NextKeyCn")) if is_next_page else ""
    result = await context.call(
        step_code="APARTMENT_RP_LIST",
        tool_name="test_tool",
        arguments={
            # 실제 MCP 명세가 bearerToekn을 요구하는 경우 이 key만 변경한다.
            "bearerToken": text(context.request_context.get("access_token")),
            "emdNm": address,
            "atrRgno": context.employee_id,
            "inqrDvC": address_type,
            "inqrCt": 50,
            "nextEtxtKeyCn": next_text_key,
            "no2NextKeyCn": no2_next_key,
        },
        enabled=True,
        unavailable_message="아파트관리비 RP 조회 도구가 아직 연결되지 않았습니다.",
    )

    # code=1001은 공통 MCP client가 NO_DATA로 정규화한다. 다음 페이지 action은
    # 성공 결과의 nextEtxtYn=Y에서만 만들고, no1Grid 원본은 상태에 저장하지 않는다.
    if result.outcome == "NO_DATA" or result.business_code == "1001":
        result.user_message = APARTMENT_RP_NO_DATA_MESSAGE
        return result
    if result.outcome != "SUCCESS" or not _has_next_apartment_page(result.result):
        return result
    next_keys = _apartment_next_keys(result.result)
    logger.info(
        "======== RP 아파트 다음 페이지 판정 | nextEtxtYn=Y | "
        "nextEtxtKeyCn존재=%s | no2NextKeyCn존재=%s | action=NEXT_PAGE",
        bool(next_keys["nextEtxtKeyCn"]),
        bool(next_keys["no2NextKeyCn"]),
    )
    # complete()는 context.call()의 원장에 기록된 동일 객체를 요구한다.
    # model_copy로 복제해 반환하면 terminal 검증에서 실패하므로 원래 객체의
    # 후속 action 메타데이터만 갱신한다.
    result.post_answer_action = {
        "action_code": APARTMENT_NEXT_PAGE_ACTION.action_code,
        "message": APARTMENT_NEXT_PAGE_ACTION.message,
        "context": {
            "emdNm": address,
            "atrRgno": context.employee_id,
            "inqrDvC": address_type,
            "inqrCt": 50,
            **next_keys,
            # 현재 완료한 페이지 번호다. MCP 인자가 아니라 HITL 내부 상태이며,
            # 다음 yes 요청에서만 1 증가시켜 답변에 표시한다.
            "page_number": page_number,
        },
    }
    return result


def _apartment_page_source(result: Any) -> Mapping[str, Any]:
    """MCP envelope의 result/structuredContext 중첩을 끝까지 벗긴다."""
    source: Mapping[str, Any] = result if isinstance(result, Mapping) else {}
    # 실제 응답은 result → structuredContext → result처럼 같은 이름이 다시
    # 중첩될 수 있다. 한 번만 순회하면 nextEtxtYn을 찾지 못하므로, 더 이상
    # 내려갈 컨테이너가 없을 때까지 반복한다.
    while True:
        nested_source: Mapping[str, Any] | None = None
        for key in ("structuredContext", "structuredContent", "result"):
            nested = source.get(key)
            if isinstance(nested, Mapping):
                nested_source = nested
                break
        if nested_source is None or nested_source is source:
            return source
        source = nested_source


def _apartment_page_value(result: Any, key: str) -> str:
    """MCP 응답 전체에서 직접 key 또는 data(objId/objVal)를 찾는다."""
    pending: list[Any] = [result]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, Mapping):
            if isinstance(current, list):
                pending.extend(current)
            continue
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        direct = text(current.get(key))
        if direct:
            return direct
        if text(current.get("objId")) == key:
            return text(current.get("objVal"))
        pending.extend(current.values())
    return ""


def _apartment_next_keys(result: Any) -> dict[str, str]:
    return {
        "nextEtxtKeyCn": _apartment_page_value(result, "nextEtxtKeyCn"),
        "no2NextKeyCn": _apartment_page_value(result, "no2NextKeyCn"),
    }


def _has_next_apartment_page(result: Any) -> bool:
    return _apartment_page_value(result, "nextEtxtYn").upper() == "Y"


async def composite_conversion_score(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    closing_month, reference_date = month_and_reference_date(
        context.subagent.parameters,
        today=date.today(),
        default_month="CURRENT",
    )
    return await context.call(
        step_code="RP_COMPOSITE_SCORE",
        tool_name="test_tool",
        arguments={"param1": closing_month, "param2": reference_date},
    )


async def composite_conversion_excluded(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    return await context.call(
        step_code="RP_COMPOSITE_EXCLUDED",
        tool_name="test_tool",
        arguments={
            "param1": month_value(
                context.subagent.parameters,
                today=date.today(),
                default_month="CURRENT",
            ),
            "param2": "",
        },
    )


# RP 결과도 중앙 columns 표가 아닌 detail별 Python 함수에서 직접 전처리한다.


def _display(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _two_column_output(
    context: ScenarioMcpOutputContext,
    *,
    title: str,
    first_label: str,
    second_label: str,
) -> ScenarioMcpOutput:
    items = context.data_items()
    values = {
        str(item.get("objId", "")).strip(): item.get("objVal")
        for item in items
        if str(item.get("objId", "")).strip()
    }
    first_value = values.get("column1", "")
    second_value = values.get("column2", "")
    return ScenarioMcpOutput(
        data=items,
        answer=ScenarioAnswer(
            text=(
                f"[{title}]\n"
                f"- {first_label}: {_display(first_value)}\n"
                f"- {second_label}: {_display(second_value)}"
            ),
            renderables=[
                create_table_renderable(
                    code="result-table",
                    title=title,
                    format="markdown",
                    columns=("항목", "값"),
                    rows=((first_label, first_value), (second_label, second_value)),
                )
            ],
        ),
    )


def apartment_page_number(parameters: Mapping[str, Any]) -> int:
    """첫 조회는 1, 다음 페이지 승인 요청은 저장된 페이지 번호에 1을 더한다."""
    if text(parameters.get("next_page_choice")).casefold() != "yes":
        return 1
    try:
        previous_page = max(1, int(parameters.get("page_number") or 1))
    except (ValueError, TypeError):
        previous_page = 1
    return previous_page + 1


def apartment_rp_list_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    output = _two_column_output(
        context,
        title="아파트관리비 RP 연결 가능 단지 조회 결과",
        first_label="아파트명",
        second_label="주소",
    )
    page_number = apartment_page_number(context.parameters)
    return ScenarioMcpOutput(
        data=output.data,
        answer=ScenarioAnswer(
            text=f"{page_number}페이지 조회 결과입니다.\n\n{output.answer.text}",
            renderables=output.answer.renderables,
        ),
        metadata={**output.metadata, "page_number": page_number},
    )


def composite_conversion_score_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    return _two_column_output(
        context,
        title="복합환산 점수 및 실적 조회 결과",
        first_label="복합환산점수",
        second_label="실적건수",
    )


def composite_conversion_excluded_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    return _two_column_output(
        context,
        title="환산 미반영 내역 조회 결과",
        first_label="미반영내역",
        second_label="미반영사유",
    )
