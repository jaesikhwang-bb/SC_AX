"""요청 사번으로 직원정보 MCP를 조회해 사용자 문맥을 보강한다.

업무 시나리오 MCP와 달리 직원정보 조회는 채팅 파이프라인의 공통 선행 단계다.
운영 MCP 계약이 바뀌면 이 파일의 ``build_employee_lookup_arguments``와
``extract_employee_name``만 수정하면 된다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.config import Settings
from app.mcp.client import McpToolExecutor
from app.observability import logger, timed
from app.subagents.models import SubagentResult


_LOOKUP_AGENT_CODE = "EMPLOYEE_CONTEXT"
_LOOKUP_SCENARIO_CODE = "EMPLOYEE_PROFILE_LOOKUP"


def build_employee_lookup_arguments(
    employee_id: str,
    request_context: Mapping[str, Any],
    settings: Settings,
) -> dict[str, Any]:
    """직원정보 MCP arguments를 만든다.

    실제 도구가 ``employee_id`` 대신 다른 키를 요구하면
    ``EMPLOYEE_LOOKUP_EMPLOYEE_ID_ARGUMENT``만 바꾸면 된다. 여러 파라미터나
    중첩 payload가 필요해지면 운영자가 이 함수만 직접 수정하면 된다.
    """

    return {
        # Authorization 헤더 또는 Body access_token에서 API 계층이 정규화한
        # Bearer 접두사 없는 실제 토큰이다. GenOS MCP가 요구하는 argument에
        # 넣되 공통 MCP 실행기가 로그에서는 자동으로 마스킹한다.
        "bearerToken": str(request_context.get("access_token") or "").strip(),
        # 직원정보 조회 대상은 프론트/WAS의 user.id에서 확정한 실제 사번이다.
        settings.employee_lookup_employee_id_argument: employee_id,
    }


def extract_employee_name(
    structured_content: Mapping[str, Any] | None,
    *,
    name_fields: Sequence[str],
) -> str:
    """MCP ``structuredContent``에서 직원명을 추출한다.

    다음 두 계약을 모두 지원한다.

    - ``data: [{"employee_name": "홍길동"}]`` 같은 일반 행 구조
    - ``data: [{"objId": "employee_name", "objVal": "홍길동"}]`` 구조
    """

    if not isinstance(structured_content, Mapping):
        return ""
    normalized_fields = {
        str(field).strip().casefold()
        for field in name_fields
        if str(field).strip()
    }
    if not normalized_fields:
        return ""

    direct = _value_for_named_field(structured_content, normalized_fields)
    if direct:
        return direct

    data = structured_content.get("data")
    rows: Sequence[Any]
    if isinstance(data, Mapping):
        rows = (data,)
    elif isinstance(data, Sequence) and not isinstance(data, (str, bytes, bytearray)):
        rows = data
    else:
        return ""

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = _value_for_named_field(row, normalized_fields)
        if value:
            return value

        object_id = str(row.get("objId") or row.get("obj_id") or "").strip()
        if object_id.casefold() not in normalized_fields:
            continue
        object_value = row.get("objVal", row.get("obj_value"))
        value = str(object_value or "").strip()
        if value:
            return value
    return ""


def _value_for_named_field(
    source: Mapping[str, Any],
    normalized_fields: set[str],
) -> str:
    """대소문자를 무시해 설정된 이름 필드의 문자열 값을 찾는다."""

    for key, raw_value in source.items():
        if str(key).strip().casefold() not in normalized_fields:
            continue
        value = str(raw_value or "").strip()
        if value:
            return value
    return ""


@timed("직원정보 MCP 선조회")
async def enrich_user_context(
    *,
    user: Mapping[str, Any] | None,
    employee_id: str,
    session_id: str,
    thread_id: str,
    request_context: Mapping[str, Any],
    executor: McpToolExecutor,
    settings: Settings,
) -> dict[str, Any]:
    """직원정보를 조회해 ``user.name``을 채운 새 dict를 반환한다.

    공통 문맥 조회 실패가 본 업무 전체를 막지 않도록 fail-open으로 동작한다.
    실패·무데이터이면 기존 사용자 정보는 유지하고 name만 빈 문자열로 둔다.
    """

    enriched = dict(user or {})
    enriched.setdefault("id", None)
    enriched.setdefault("deptcode", None)
    enriched.setdefault("deptname", None)
    enriched["name"] = str(enriched.get("name") or "").strip()

    actual_employee_id = str(enriched.get("id") or "").strip()
    if not settings.employee_lookup_enabled:
        logger.info("======== 직원정보 MCP 조회 생략 | 사유=설정비활성")
        return enriched
    if not actual_employee_id:
        logger.info("======== 직원정보 MCP 조회 생략 | 사유=실제사번없음")
        return enriched

    lookup_subagent = SubagentResult(
        agent_code=_LOOKUP_AGENT_CODE,
        prompt_version="request-context-v1",
        scenario_code=_LOOKUP_SCENARIO_CODE,
        scenario_name="직원정보 조회",
        detail_scenario_code=_LOOKUP_SCENARIO_CODE,
        detail_scenario_name="사번 기준 직원정보 조회",
        parameters={"employee_id": actual_employee_id},
    )
    try:
        execution = await executor.execute(
            subagent=lookup_subagent,
            employee_id=employee_id,
            session_id=session_id,
            thread_id=thread_id,
            tool_name=settings.employee_lookup_tool_name,
            arguments=build_employee_lookup_arguments(
                actual_employee_id,
                request_context,
                settings,
            ),
            request_context=dict(request_context),
            step_code="EMPLOYEE_PROFILE_LOOKUP",
        )
    except Exception as exc:
        logger.warning(
            "!!!!!!!! 직원정보 MCP 조회 실패 | 채팅처리계속=예 | 오류유형=%s",
            type(exc).__name__,
        )
        return enriched

    if execution is None or not execution.succeeded or execution.outcome != "SUCCESS":
        logger.warning(
            "!!!!!!!! 직원정보 MCP 조회 미완료 | 채팅처리계속=예 | 상태=%s | 업무코드=%s",
            getattr(execution, "outcome", "NO_RESULT"),
            getattr(execution, "business_code", None),
        )
        return enriched

    name = extract_employee_name(
        execution.result,
        name_fields=settings.employee_lookup_name_fields,
    )
    if name:
        enriched["name"] = name
    logger.info(
        "======== 직원정보 MCP 조회 완료 | 직원명조회=%s | 응답본문로그=생략",
        bool(name),
    )
    return enriched
