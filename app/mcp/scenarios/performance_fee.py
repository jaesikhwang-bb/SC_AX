"""PERFORMANCE_FEE 세부 시나리오별 MCP 실행 함수.

도구명, arguments, 호출 횟수, next-key 판정과 다음 도구 연결은 전부 이 파일의
각 함수에서 직접 수정한다. YAML에는 분류용 parameter와 추천질문만 남긴다.
"""

from collections.abc import Mapping
from datetime import date
from typing import Any

from app.mcp.models import McpExecutionResult
from app.mcp.scenario_runtime import ScenarioMcpHandlerContext
from app.mcp.scenarios.contracts import (
    ScenarioMcpOutput,
    ScenarioMcpOutputContext,
)
from app.mcp.scenarios.helpers import month_and_reference_date, month_value, text
from app.renderables import (
    ScenarioAnswer,
    create_table_renderable,
    extract_data_items,
)
from app.scenario_actions import (
    ScenarioActionDefinition,
    ScenarioActionInput,
    register_scenario_action,
)


# 복합환산 점수 개발용 조회 대상 override.
# 실제 첫 번째 test_tool 응답 대신 직접 대상을 주입해 테스트하려면 아래 tuple에
# {"code": "...", "code_name": "..."}를 넣는다. 비어 있으면 첫 MCP 결과를 사용한다.
COMPOSITE_SCORE_PARAMETER_OVERRIDE: tuple[Mapping[str, Any], ...] = ()


# -------------------------------------------------------------------------
# 원천징수 팩스 action/MCP 정책
# -------------------------------------------------------------------------
# action code, 안내문, 필드와 검증은 YAML이 아니라 이 Python 함수에서 바꾼다.
# 같은 detail에서 action을 더 이어가려면 정의를 하나 더 등록하고
# withholding_tax_fax_send() 안에서 원하는 순서로 ``require()``를 호출한다.


def _withholding_tax_fax_number_action() -> ScenarioActionDefinition:
    return ScenarioActionDefinition(
        agent_code="PERFORMANCE_FEE",
        detail_scenario_code="WITHHOLDING_TAX_FAX_SEND",
        action_code="INPUT_FAX",
        message="팩스 번호를 입력해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="fax_part_1",
                input_code="INPUT_FAX",
                order=1,
                label="지역번호",
                input_type="tel",
                pattern=r"^0\d{1,2}$",
                validation_message="팩스번호 지역번호를 확인해 주세요.",
                sensitive=True,
                guardrail_enabled=False,
            ),
            ScenarioActionInput(
                parameter_name="fax_part_2",
                input_code="INPUT_FAX",
                order=2,
                label="가운데 번호",
                input_type="tel",
                pattern=r"^\d{3,4}$",
                validation_message="팩스번호 가운데 자리를 확인해 주세요.",
                sensitive=True,
                guardrail_enabled=False,
            ),
            ScenarioActionInput(
                parameter_name="fax_part_3",
                input_code="INPUT_FAX",
                order=3,
                label="마지막 번호",
                input_type="tel",
                pattern=r"^\d{4}$",
                validation_message="팩스번호 마지막 자리를 확인해 주세요.",
                sensitive=True,
                guardrail_enabled=False,
            ),
        ),
    )


WITHHOLDING_TAX_FAX_NUMBER_ACTION = register_scenario_action(
    _withholding_tax_fax_number_action()
)

# 실제 MCP가 준비되면 아래 네 값과 handler arguments만 실제 계약에 맞춘다.
WITHHOLDING_TAX_FAX_TOOL_ENABLED = False
WITHHOLDING_TAX_FAX_TOOL_NAME = "withholding_tax_fax_send"
WITHHOLDING_TAX_FAX_UNAVAILABLE_MESSAGE = (
    "팩스 전송 도구가 아직 연결되지 않았습니다. 도구 연결 후 다시 시도해 주세요."
)
WITHHOLDING_TAX_FAX_SUCCESS_MESSAGE = (
    "원천징수 내역을 입력한 팩스번호로 전송했습니다."
)


def _successful(result: McpExecutionResult) -> bool:
    return (
        result.succeeded
        and result.outcome == "SUCCESS"
        and result.backend != "disabled"
    )


async def performance_summary_total(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    """실적 종합 조회: 운영 도구 1회 호출."""

    closing_month, reference_date = month_and_reference_date(
        context.subagent.parameters,
        today=date.today(),
        default_month="CURRENT",
    )
    return await context.call(
        step_code="PERFORMANCE_SUMMARY",
        tool_name="performance_get_summary",
        arguments={
            "bearerToken": text(context.request_context.get("access_token")),
            "arOgDvC": text(
                context.request_context.get("recruitment_org_type_code")
            ),
            "clWkYm": closing_month,
            "bsdt": reference_date,
            "atrRgno": context.employee_id,
        },
    )


async def composite_conversion_score(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    """조회 대상 code 목록을 얻은 뒤 code별로 모든 페이지를 조회한다."""

    closing_month, reference_date = month_and_reference_date(
        context.subagent.parameters,
        today=date.today(),
        default_month="CURRENT",
    )

    # 1단계: 향후 실제 목록 MCP로 tool_name/arguments만 교체할 수 있다.
    parameter_lookup = await context.call(
        step_code="COMPOSITE_SCORE_PARAMETERS",
        tool_name="test_tool",
        arguments={"param1": closing_month, "param2": reference_date},
    )
    if not _successful(parameter_lookup):
        return parameter_lookup

    parameter_rows = _composite_score_parameter_rows(parameter_lookup)
    if not parameter_rows:
        # 빈 override와 실제 MCP 결과 모두 대상이 없으면 첫 조회 결과를 terminal로 쓴다.
        return parameter_lookup

    detail_results: list[McpExecutionResult] = []
    for parameter_index, parameter_row in enumerate(parameter_rows):
        code = str(parameter_row["code"]).strip()
        detail_result = await context.paginate(
            # code 문자열 대신 index를 써서 특수문자·중복 code가 request ID를 깨지 않게 한다.
            step_code=f"COMPOSITE_SCORE_PAGES_{parameter_index:04d}",
            tool_name="test_tool",
            initial_arguments={
                "param1": closing_month,
                "param2": reference_date,
                # 실제 MCP 요청 필드명이 다르면 이 key만 변경한다.
                "code": code,
            },
            next_arguments=_all_next_key_arguments,
            max_pages=1000,
            # 한 코드가 1001이어도 나머지 코드 조회를 계속해야 전체 무데이터 여부를
            # 판정할 수 있다. 실제 ERROR는 아래에서 즉시 terminal로 반환한다.
            error_policy="continue",
        )
        if (
            not detail_result.succeeded
            or detail_result.outcome == "ERROR"
            or detail_result.backend == "disabled"
            or detail_result.error
        ):
            return detail_result
        detail_results.append(detail_result)

    # 마지막 detail_result만 반환하지 않는다. 코드별 terminal 결과를 다시 합쳐
    # 모두 1001이면 최종 business_code=1001/NO_DATA가 output으로 전달되게 한다.
    return context.combine_results(
        step_code="COMPOSITE_SCORE_DETAILS",
        tool_name="test_tool",
        results=detail_results,
        metadata={"parameterCount": len(parameter_rows)},
    )


def _composite_score_parameter_rows(
    lookup: McpExecutionResult,
) -> list[dict[str, Any]]:
    """첫 MCP 결과 또는 개발용 override를 ``code/code_name`` 목록으로 정규화한다.

    실제 결과가 ``data=[{"code": ...}]``이거나 objVal 내부에 같은 목록을 담는
    경우를 모두 찾는다. 출력 schema가 확정되면 이 함수만 해당 구조에 맞게 좁힌다.
    """

    source: Any = (
        list(COMPOSITE_SCORE_PARAMETER_OVERRIDE)
        if COMPOSITE_SCORE_PARAMETER_OVERRIDE
        else extract_data_items(lookup.result or {})
    )
    candidates: list[Mapping[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            if "code" in value:
                candidates.append(value)
            for child in value.values():
                if isinstance(child, (Mapping, list, tuple)):
                    collect(child)
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(source)
    rows: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for item in candidates:
        code = str(item.get("code", "")).strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        rows.append(
            {
                "code": code,
                "code_name": str(item.get("code_name", "")).strip(),
            }
        )
    return rows


def _all_next_key_arguments(
    page: McpExecutionResult,
    _page_number: int,
) -> Mapping[str, Any] | None:
    """nextkey 전체가 비었을 때만 종료하고 gridct를 다음 인자로 전달한다.

    ``gridct``는 이 MCP가 반환하는 다음 페이지 크기이며, MCP 요청 계약에 맞춰
    ``no1PgeSize``라는 이름으로 넘긴다. 이 종료 규칙은 이 도구 전용이므로 다른
    MCP는 해당 시나리오 파일에 별도 callback을 작성한다.
    """

    if not _successful(page) or page.result is None:
        return None
    data_items = extract_data_items(page.result)
    next_items = [
        item
        for item in data_items
        if "nextkey" in str(item.get("objId", "")).casefold()
    ]
    if not next_items:
        return None
    next_arguments: dict[str, Any] = {
        str(item.get("objId", "")).strip(): item.get("objVal")
        for item in next_items
        if str(item.get("objId", "")).strip()
    }
    if not next_arguments:
        return None

    # 하나라도 채워져 있으면 빈 값을 포함한 모든 nextkey를 다음 요청으로 보낸다.
    if all(
        value is None or not str(value).strip()
        for value in next_arguments.values()
    ):
        return None

    gridct_item = next(
        (
            item
            for item in data_items
            if str(item.get("objId", "")).strip().casefold() == "gridct"
        ),
        None,
    )
    if gridct_item is None:
        raise ValueError("다음 페이지 nextkey는 존재하지만 gridct 항목이 없습니다.")
    raw_gridct = gridct_item.get("objVal")
    if isinstance(raw_gridct, bool):
        raise ValueError("gridct는 bool이 아닌 정수여야 합니다.")
    try:
        gridct = int(str(raw_gridct).strip().replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"gridct를 정수로 변환할 수 없습니다: {raw_gridct!r}"
        ) from exc
    if gridct < 0:
        raise ValueError("gridct는 0 이상의 정수여야 합니다.")

    next_arguments["no1PgeSize"] = gridct
    return next_arguments


async def composite_conversion_excluded(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    """환산 미반영 내역을 nextkey가 끝날 때까지 모두 조회한다."""

    return await context.paginate(
        step_code="EXCLUDED_PAGES",
        tool_name="test_tool",
        initial_arguments={
            "param1": month_value(
                context.subagent.parameters,
                today=date.today(),
                default_month="CURRENT",
            ),
            "param2": context.employee_id,
        },
        next_arguments=_all_next_key_arguments,
        max_pages=1000,
        error_policy="fail_fast",
    )


async def unregistered_member_summary(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    return await context.call(
        step_code="UNREGISTERED_SUMMARY",
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


async def disposal_fee_summary(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    return await context.call(
        step_code="DISPOSAL_SUMMARY",
        tool_name="test_tool",
        arguments={
            "param1": month_value(
                context.subagent.parameters,
                today=date.today(),
                default_month="PREVIOUS",
            ),
            "param2": "",
        },
    )


async def fee_item_details(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    return await context.call(
        step_code="FEE_ITEMS",
        tool_name="test_tool",
        arguments={
            "param1": month_value(
                context.subagent.parameters,
                today=date.today(),
                default_month="PREVIOUS",
            ),
            "param2": "",
        },
    )


async def withholding_tax(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    reference_year = text(context.subagent.parameters.get("reference_year"))
    return await context.call(
        step_code="WITHHOLDING_TAX",
        tool_name="test_tool",
        arguments={
            "param1": reference_year or str(date.today().year - 1),
            "param2": "",
        },
    )


async def withholding_tax_fax_send(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    """독립 팩스 detail: Python action 완료 후 실제 전송 도구를 호출한다."""

    action_values = WITHHOLDING_TAX_FAX_NUMBER_ACTION.require(
        context.subagent.parameters
    )
    # 프론트가 order 1~3으로 입력한 세 필드는 action 검증 단계에서 각각의
    # parameter로 복원된다. MCP에는 숫자를 순서대로 결합한 한 값만 전달한다.
    fax_number = "".join(
        (
            action_values["fax_part_1"],
            action_values["fax_part_2"],
            action_values["fax_part_3"],
        )
    )
    return await context.call(
        step_code="WITHHOLDING_TAX_FAX_SEND",
        tool_name=WITHHOLDING_TAX_FAX_TOOL_NAME,
        arguments={
            "bearerToken": text(context.request_context.get("access_token")),
            "employeeId": context.employee_id,
            "faxNumber": fax_number,
            "referenceYear": (
                text(context.subagent.parameters.get("reference_year"))
                or str(date.today().year - 1)
            ),
        },
        enabled=WITHHOLDING_TAX_FAX_TOOL_ENABLED,
        unavailable_message=WITHHOLDING_TAX_FAX_UNAVAILABLE_MESSAGE,
    )


# -------------------------------------------------------------------------
# 세부 시나리오별 MCP 결과 전처리·출력 함수
# -------------------------------------------------------------------------
# 아래 함수는 공통 columns 설정을 거치지 않는다. context.raw_result, data_items(),
# workflow, workflow_results를 원하는 방식으로 읽어 결과 구조와 화면 출력을 직접
# 결정한다. 도구 호출 함수와 같은 파일에 두어 입력·출력 계약을 함께 관리한다.


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _first_value(items: list[dict[str, Any]], obj_id: str) -> Any:
    target = str(obj_id).strip()
    return next(
        (
            item.get("objVal")
            for item in items
            if str(item.get("objId", "")).strip() == target
        ),
        "",
    )


def _two_column_output(
    context: ScenarioMcpOutputContext,
    *,
    title: str,
    first_label: str,
    second_label: str,
    first_obj_id: str = "column1",
    second_obj_id: str = "column2",
) -> ScenarioMcpOutput:
    """단건 2개 컬럼 도구의 선택적 편의 함수.

    사용은 선택 사항이다. 목록·그리드·다단계 결과처럼 모양이 다른 시나리오는
    이 helper를 쓰지 않고 각 output 함수에서 context를 직접 파싱한다.
    """

    items = context.data_items()
    first_value = _first_value(items, first_obj_id)
    second_value = _first_value(items, second_obj_id)
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
                    rows=(
                        (first_label, first_value),
                        (second_label, second_value),
                    ),
                )
            ],
        ),
    )


def performance_summary_total_output(
    context: ScenarioMcpOutputContext,
) -> ScenarioMcpOutput:
    items = context.data_items()
    rows = [(item.get("objId", ""), item.get("objVal", "")) for item in items]
    text_value = "[실적 종합 조회 결과]"
    if rows:
        text_value += "\n" + "\n".join(
            f"- {_display(name)}: {_display(value)}" for name, value in rows
        )
    else:
        text_value += "\n- 조회된 실적 항목이 없습니다."
    return ScenarioMcpOutput(
        data=items,
        answer=ScenarioAnswer(
            text=text_value,
            renderables=[
                create_table_renderable(
                    code="result-table",
                    title="실적 종합 조회 결과",
                    format="markdown",
                    columns=("항목", "값"),
                    rows=rows,
                )
            ],
        ),
    )


def composite_conversion_score_output(
    context: ScenarioMcpOutputContext,
) -> ScenarioMcpOutput:
    """code별 페이지 결과를 그룹화한다.

    최종 업무 컬럼·문장·표는 이 함수에서 하드코딩해 변경한다. 현재 기본 출력은
    code/code_name별 페이지 수와 no1Grid 행 수를 보여 주고, ``data.groups``에는
    각 페이지 원본과 평탄화된 grid 전체를 보존한다.
    """

    lookup = next(
        (
            result
            for result in context.workflow_results
            if result.workflow_step_code == "COMPOSITE_SCORE_PARAMETERS"
        ),
        None,
    )
    parameter_rows = (
        _composite_score_parameter_rows(lookup)
        if lookup is not None and _successful(lookup)
        else []
    )
    groups: list[dict[str, Any]] = []

    for result in context.workflow_results:
        step_code = str(result.workflow_step_code)
        if not (
            result.workflow_is_aggregate
            and step_code.startswith("COMPOSITE_SCORE_PAGES_")
            and isinstance(result.result, Mapping)
        ):
            continue
        try:
            parameter_index = int(step_code.rsplit("_", 1)[-1])
        except ValueError:
            continue
        parameter_row = (
            parameter_rows[parameter_index]
            if parameter_index < len(parameter_rows)
            else {"code": "", "code_name": ""}
        )
        raw_pages: list[dict[str, Any]] = []
        grid_rows: list[dict[str, Any]] = []
        batches = result.result.get("batches", [])
        if not isinstance(batches, list):
            batches = []

        for page_index, batch in enumerate(batches):
            if not isinstance(batch, Mapping):
                continue
            raw_result = batch.get("result")
            raw_pages.append(
                {
                    "page": page_index + 1,
                    "arguments": dict(batch.get("arguments", {}))
                    if isinstance(batch.get("arguments"), Mapping)
                    else {},
                    "result": raw_result,
                }
            )
            try:
                page_items = extract_data_items(raw_result)
            except ValueError:
                continue
            for item in page_items:
                if str(item.get("objId", "")).strip().casefold() != "no1grid":
                    continue
                for field_items in _flatten_grid_field_rows(item.get("objVal")):
                    row = {
                        str(field.get("objId", "")).strip(): field.get("objVal")
                        for field in field_items
                        if str(field.get("objId", "")).strip()
                    }
                    if row:
                        grid_rows.append({"_page": page_index + 1, **row})

        groups.append(
            {
                "code": parameter_row.get("code", ""),
                "code_name": parameter_row.get("code_name", ""),
                "pageCount": len(raw_pages),
                "gridRows": grid_rows,
                # Postman/tester에서 각 페이지 MCP 원본을 그대로 확인할 수 있다.
                "rawPages": raw_pages,
            }
        )

    summary_rows = [
        (
            group["code"],
            group["code_name"],
            group["pageCount"],
            len(group["gridRows"]),
        )
        for group in groups
    ]
    total_grid_rows = sum(len(group["gridRows"]) for group in groups)
    return ScenarioMcpOutput(
        data={
            "parameterRows": parameter_rows,
            "groups": groups,
            "totalGridRowCount": total_grid_rows,
        },
        answer=ScenarioAnswer(
            text=(
                "[복합환산 점수 및 실적 조회 결과]\n"
                f"- 조회 코드: {len(groups)}개\n"
                f"- 상세 데이터: {total_grid_rows}건"
            ),
            renderables=[
                create_table_renderable(
                    code="composite-score-summary-table",
                    title="복합환산 코드별 조회 결과",
                    format="markdown",
                    columns=("코드", "코드명", "페이지 수", "상세 건수"),
                    rows=summary_rows,
                )
            ],
        ),
        metadata={
            "preprocessor": "performance_fee.composite_conversion_score_output",
            "parameterSource": (
                "hardcoded_override"
                if COMPOSITE_SCORE_PARAMETER_OVERRIDE
                else "COMPOSITE_SCORE_PARAMETERS"
            ),
        },
    )


def _flatten_grid_field_rows(value: Any) -> list[list[dict[str, Any]]]:
    """중첩 list인 no1Grid objVal을 ``[{objId, objVal}, ...]`` 행으로 푼다."""

    if not isinstance(value, list):
        return []
    if value and all(
        isinstance(item, Mapping) and "objId" in item
        for item in value
    ):
        return [[dict(item) for item in value]]
    rows: list[list[dict[str, Any]]] = []
    for child in value:
        rows.extend(_flatten_grid_field_rows(child))
    return rows


def composite_conversion_excluded_output(
    context: ScenarioMcpOutputContext,
) -> ScenarioMcpOutput:
    """모든 페이지의 no1Grid만 행 단위로 합치고 공통 항목은 1회만 보존한다."""

    items = context.data_items()
    common_items: list[dict[str, Any]] = []
    grid_rows: list[dict[str, Any]] = []
    control_ids = {"gridct"}

    for item in items:
        obj_id = str(item.get("objId", "")).strip()
        call = item.get("_function_call", {})
        page_index = int(call.get("index", 0)) if isinstance(call, Mapping) else 0

        # 페이지마다 바뀌는 내부 grid만 전체 페이지에서 합친다.
        if obj_id.casefold() == "no1grid":
            for field_items in _flatten_grid_field_rows(item.get("objVal")):
                row = {
                    str(field.get("objId", "")).strip(): field.get("objVal")
                    for field in field_items
                    if str(field.get("objId", "")).strip()
                }
                if row:
                    grid_rows.append({"_page": page_index + 1, **row})
            continue

        # nextkey/gridct와 2페이지 이후의 반복 공통 항목은 화면 데이터에서 제외한다.
        if (
            page_index == 0
            and "nextkey" not in obj_id.casefold()
            and obj_id.casefold() not in control_ids
        ):
            common_items.append(dict(item))

    grid_columns = [
        column
        for column in sorted(
            {key for row in grid_rows for key in row if key != "_page"}
        )
    ]
    table_rows = [
        tuple([row.get("_page", ""), *[row.get(column, "") for column in grid_columns]])
        for row in grid_rows
    ]
    aggregate_result = (
        context.execution.result
        if isinstance(context.execution.result, Mapping)
        else {}
    )
    execution_summary = aggregate_result.get("execution", {})
    page_count = (
        execution_summary.get("pageCount", 0)
        if isinstance(execution_summary, Mapping)
        else 0
    )
    return ScenarioMcpOutput(
        data={
            "commonItems": common_items,
            "gridRows": grid_rows,
            "pageCount": page_count,
        },
        answer=ScenarioAnswer(
            text=(
                "[환산 미반영 내역 조회 결과]\n"
                f"- 조회 페이지: {page_count}회\n"
                f"- 미반영 상세: {len(grid_rows)}건"
            ),
            renderables=[
                create_table_renderable(
                    code="excluded-grid-table",
                    title="환산 미반영 내역 조회 결과",
                    format="markdown",
                    columns=("페이지", *grid_columns),
                    rows=table_rows,
                )
            ],
        ),
        metadata={
            "preprocessor": "performance_fee.composite_conversion_excluded_output",
            "commonItemSource": "first_page_only",
            "gridSource": "all_pages.no1Grid",
        },
    )


def unregistered_member_summary_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    return _two_column_output(context, title="미등록 회원 현황 조회 결과", first_label="유형", second_label="건수")


def disposal_fee_summary_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    return _two_column_output(context, title="폐기 환산 및 회입 수수료 조회 결과", first_label="폐기환산정보", second_label="회입수수료")


def fee_item_details_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    return _two_column_output(context, title="항목별 수수료 조회 결과", first_label="수수료항목", second_label="수수료금액")


def withholding_tax_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    return _two_column_output(context, title="원천징수 내역 조회 결과", first_label="원천징수항목", second_label="원천징수금액")


def withholding_tax_fax_send_output(
    context: ScenarioMcpOutputContext,
) -> ScenarioMcpOutput:
    if context.execution.backend == "disabled":
        message = (
            context.execution.user_message
            or WITHHOLDING_TAX_FAX_UNAVAILABLE_MESSAGE
        )
    elif context.execution.succeeded and context.execution.outcome == "SUCCESS":
        message = WITHHOLDING_TAX_FAX_SUCCESS_MESSAGE
    else:
        message = context.execution.user_message or "팩스 전송 요청을 완료하지 못했습니다."
    return ScenarioMcpOutput(
        data={"raw_result": context.raw_result},
        answer=ScenarioAnswer(text=message),
    )
