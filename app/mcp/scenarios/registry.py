"""에이전트/세부 시나리오 코드를 Python MCP handler에 연결한다.

YAML에는 더 이상 MCP tool 이름, step, next-key 규칙을 선언하지 않는다. 새
시나리오를 추가할 때 이 registry에 함수 하나를 연결하고 해당 에이전트 파일에서
호출 흐름을 구현한다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.mcp.models import McpExecutionResult
from app.mcp.scenario_runtime import (
    ScenarioMcpHandlerContext,
    ScenarioMcpHandlerOutcome,
)
from app.mcp.scenarios.contracts import (
    ScenarioMcpOutput,
    ScenarioMcpOutputContext,
    ScenarioRagOutput,
    ScenarioRagOutputContext,
)
from app.mcp.scenarios import performance_fee, qualification, rp
from app.observability import log_failure_diagnostic
from app.scenario_actions import ScenarioActionRequired


ScenarioMcpHandler = Callable[
    [ScenarioMcpHandlerContext],
    Awaitable[McpExecutionResult],
]
ScenarioMcpOutputHandler = Callable[
    [ScenarioMcpOutputContext],
    ScenarioMcpOutput,
]
ScenarioRagOutputHandler = Callable[
    [ScenarioRagOutputContext],
    Awaitable[ScenarioRagOutput],
]


@dataclass(frozen=True)
class ScenarioMcpHandlerSpec:
    """한 detail의 입력·MCP 실행·출력 전처리 함수 연결점.

    ``handler``는 도구명, arguments, 다단계·fan-out·pagination을 자유롭게
    구성한다. ``output_handler``는 원본 결과와 전체 호출 원장을 받아 컬럼 선택,
    파싱, 병합, 문장·테이블·카드 생성까지 세부 시나리오별로 직접 수행한다.
    """

    code: str
    handler: ScenarioMcpHandler
    output_handler: ScenarioMcpOutputHandler | None = None
    output_handler_code: str | None = None
    rag_output_handler: ScenarioRagOutputHandler | None = None
    rag_output_handler_code: str | None = None


def _spec(
    code: str,
    handler: ScenarioMcpHandler,
    *,
    output_handler: ScenarioMcpOutputHandler | None = None,
    output_handler_code: str | None = None,
    rag_output_handler: ScenarioRagOutputHandler | None = None,
    rag_output_handler_code: str | None = None,
) -> ScenarioMcpHandlerSpec:
    return ScenarioMcpHandlerSpec(
        code=code,
        handler=handler,
        output_handler=output_handler,
        output_handler_code=output_handler_code,
        rag_output_handler=rag_output_handler,
        rag_output_handler_code=rag_output_handler_code,
    )


SCENARIO_HANDLER_REGISTRY: dict[
    tuple[str, str],
    ScenarioMcpHandlerSpec,
] = {
    ("PERFORMANCE_FEE", "PERFORMANCE_SUMMARY_TOTAL"): _spec(
        "performance_fee.performance_summary_total.v1",
        performance_fee.performance_summary_total,
        output_handler=performance_fee.performance_summary_total_output,
        output_handler_code="performance_fee.performance_summary_total_output.v1",
    ),
    ("PERFORMANCE_FEE", "COMPOSITE_CONVERSION_SCORE"): _spec(
        "performance_fee.composite_conversion_score.v1",
        performance_fee.composite_conversion_score,
        output_handler=performance_fee.composite_conversion_score_output,
        output_handler_code="performance_fee.composite_conversion_score_output.v1",
    ),
    ("PERFORMANCE_FEE", "COMPOSITE_CONVERSION_EXCLUDED"): _spec(
        "performance_fee.composite_conversion_excluded.v1",
        performance_fee.composite_conversion_excluded,
        output_handler=performance_fee.composite_conversion_excluded_output,
        output_handler_code="performance_fee.composite_conversion_excluded_output.v1",
    ),
    ("PERFORMANCE_FEE", "UNREGISTERED_MEMBER_SUMMARY"): _spec(
        "performance_fee.unregistered_member_summary.v1",
        performance_fee.unregistered_member_summary,
        output_handler=performance_fee.unregistered_member_summary_output,
        output_handler_code="performance_fee.unregistered_member_summary_output.v1",
    ),
    ("PERFORMANCE_FEE", "DISPOSAL_FEE_SUMMARY"): _spec(
        "performance_fee.disposal_fee_summary.v1",
        performance_fee.disposal_fee_summary,
        output_handler=performance_fee.disposal_fee_summary_output,
        output_handler_code="performance_fee.disposal_fee_summary_output.v1",
    ),
    ("PERFORMANCE_FEE", "FEE_ITEM_DETAILS"): _spec(
        "performance_fee.fee_item_details.v1",
        performance_fee.fee_item_details,
        output_handler=performance_fee.fee_item_details_output,
        output_handler_code="performance_fee.fee_item_details_output.v1",
    ),
    ("PERFORMANCE_FEE", "WITHHOLDING_TAX"): _spec(
        "performance_fee.withholding_tax.v1",
        performance_fee.withholding_tax,
        output_handler=performance_fee.withholding_tax_output,
        output_handler_code="performance_fee.withholding_tax_output.v1",
    ),
    ("PERFORMANCE_FEE", "WITHHOLDING_TAX_FAX_SEND"): _spec(
        "performance_fee.withholding_tax_fax_send.v1",
        performance_fee.withholding_tax_fax_send,
        output_handler=performance_fee.withholding_tax_fax_send_output,
        output_handler_code="performance_fee.withholding_tax_fax_send_output.v1",
    ),
    ("QUALIFICATION", "NEW_MEMBER_QUALIFICATION"): _spec(
        "qualification.new_member_document_search.v1",
        qualification.qualification_document_search,
        rag_output_handler=qualification.qualification_rag_output,
        rag_output_handler_code="qualification.qualification_rag_output.v1",
    ),
    ("QUALIFICATION", "FOREIGNER_QUALIFICATION"): _spec(
        "qualification.foreigner_document_search.v1",
        qualification.qualification_document_search,
        rag_output_handler=qualification.qualification_rag_output,
        rag_output_handler_code="qualification.qualification_rag_output.v1",
    ),
    ("QUALIFICATION", "MINOR_QUALIFICATION"): _spec(
        "qualification.minor_document_search.v1",
        qualification.qualification_document_search,
        rag_output_handler=qualification.qualification_rag_output,
        rag_output_handler_code="qualification.qualification_rag_output.v1",
    ),
    ("QUALIFICATION", "FAMILY_CARD_ISSUANCE_QUALIFICATION"): _spec(
        "qualification.family_card_document_search.v1",
        qualification.qualification_document_search,
        rag_output_handler=qualification.qualification_rag_output,
        rag_output_handler_code="qualification.qualification_rag_output.v1",
    ),
    ("RP", "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"): _spec(
        "rp.apartment_management_fee_autopay_guide.v1",
        rp.rp_document_search,
        rag_output_handler=rp.rp_document_rag_output,
        rag_output_handler_code="rp.rp_document_rag_output.v1",
    ),
    ("RP", "CITY_GAS_AUTOPAY_GUIDE"): _spec(
        "rp.city_gas_autopay_guide.v1",
        rp.rp_document_search,
        rag_output_handler=rp.rp_document_rag_output,
        rag_output_handler_code="rp.rp_document_rag_output.v1",
    ),
    ("RP", "SOCIAL_INSURANCE_AUTOPAY_GUIDE"): _spec(
        "rp.social_insurance_autopay_guide.v1",
        rp.rp_document_search,
        rag_output_handler=rp.rp_document_rag_output,
        rag_output_handler_code="rp.rp_document_rag_output.v1",
    ),
    ("RP", "ELECTRICITY_TV_FEE_AUTOPAY_GUIDE"): _spec(
        "rp.electricity_tv_fee_autopay_guide.v1",
        rp.rp_document_search,
        rag_output_handler=rp.rp_document_rag_output,
        rag_output_handler_code="rp.rp_document_rag_output.v1",
    ),
    ("RP", "SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE"): _spec(
        "rp.samsung_postpaid_hipass_card_guide.v1",
        rp.rp_document_search,
        rag_output_handler=rp.rp_document_rag_output,
        rag_output_handler_code="rp.rp_document_rag_output.v1",
    ),
    ("RP", "PAYMENT_NOTIFICATION_SERVICE_GUIDE"): _spec(
        "rp.payment_notification_service_guide.v1",
        rp.rp_document_search,
        rag_output_handler=rp.rp_document_rag_output,
        rag_output_handler_code="rp.rp_document_rag_output.v1",
    ),
    ("RP", "PAYMENT_DATE_CREDIT_PERIOD_GUIDE"): _spec(
        "rp.payment_date_credit_period_guide.v1",
        rp.rp_document_search,
        rag_output_handler=rp.rp_document_rag_output,
        rag_output_handler_code="rp.rp_document_rag_output.v1",
    ),
    ("RP", "APARTMENT_RP_LIST"): _spec(
        "rp.apartment_rp_list.v1",
        rp.apartment_rp_list,
        output_handler=rp.apartment_rp_list_output,
        output_handler_code="rp.apartment_rp_list_output.v1",
    ),
    ("RP", "COMPOSITE_CONVERSION_SCORE"): _spec(
        "rp.composite_conversion_score.v1",
        rp.composite_conversion_score,
        output_handler=rp.composite_conversion_score_output,
        output_handler_code="rp.composite_conversion_score_output.v1",
    ),
    ("RP", "COMPOSITE_CONVERSION_EXCLUDED"): _spec(
        "rp.composite_conversion_excluded.v1",
        rp.composite_conversion_excluded,
        output_handler=rp.composite_conversion_excluded_output,
        output_handler_code="rp.composite_conversion_excluded_output.v1",
    ),
}


def get_scenario_handler_spec(
    agent_code: str,
    detail_scenario_code: str,
) -> ScenarioMcpHandlerSpec | None:
    """현재 detail에 등록된 함수형 MCP handler를 반환한다."""

    return SCENARIO_HANDLER_REGISTRY.get(
        (str(agent_code).upper().strip(), str(detail_scenario_code).strip())
    )
async def run_scenario_handler(
    *,
    spec: ScenarioMcpHandlerSpec,
    context: ScenarioMcpHandlerContext,
) -> ScenarioMcpHandlerOutcome:
    """handler 예외를 안전한 terminal 결과로 바꾸고 전체 원장을 확정한다."""

    try:
        terminal = await spec.handler(context)
        return context.complete(terminal)
    except ScenarioActionRequired:
        # 사용자 추가 입력은 graph가 action으로 변환해야 하므로 그대로 전달한다.
        raise
    except Exception as exc:
        log_failure_diagnostic(
            stage="Python 함수형 MCP handler",
            code_location=(
                f"{spec.handler.__module__}:{spec.handler.__name__}"
            ),
            exc=exc,
            likely_cause=(
                "도구별 결과 schema, next-key 판정, 다음 arguments 생성 또는 "
                "handler 반환 계약 불일치"
            ),
            corrective_action=(
                "tester의 마지막 성공 raw result와 handlerCode를 확인하고 "
                "app/mcp/scenarios의 해당 함수를 수정하세요."
            ),
            retry_count=0,
            context={
                "handler_code": spec.code,
                "agent_code": context.subagent.agent_code,
                "detail_scenario_code": (
                    context.subagent.detail_scenario_code
                ),
                "completed_result_count": len(context.results),
            },
        )
        return context.fail(exc)
