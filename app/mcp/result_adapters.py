"""함수형 MCP 결과 전처리·프런트 출력 어댑터.

활성 세부 시나리오는 중앙 columns 설정을 사용하지 않는다. 각 detail은
``app/mcp/scenarios/<agent>.py``의 output 함수에서 원본 결과를 자유롭게 파싱하고
전처리 데이터, 답변 본문, table/card/file renderable을 직접 만든다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.mcp.models import McpExecutionResult
from app.mcp.scenarios.contracts import ScenarioMcpOutputContext
from app.mcp.scenarios.registry import get_scenario_handler_spec
from app.observability import logger, timed
from app.renderables import normalize_scenario_answer
from app.scenario_actions import redact_scenario_action_parameters
from app.subagents.models import SubagentResult


QUERY_RESULT_FORMAT = "query.v1"
RAG_RAW_RESULT_FORMAT = "raw.rag"


class McpResultFormatError(ValueError):
    """세부 시나리오 결과 전처리 함수 계약이 맞지 않을 때 발생한다."""


@timed("MCP 조회 결과 정제")
def adapt_mcp_result(
    *,
    execution: McpExecutionResult,
    subagent: SubagentResult,
    employee_id: str,
    session_id: str,
    thread_id: str,
    request_context: Mapping[str, Any],
    workflow_results: Sequence[McpExecutionResult] = (),
) -> McpExecutionResult:
    """세부 시나리오의 함수형 output handler를 실행해 프런트 결과를 만든다."""

    if (
        execution.outcome != "SUCCESS"
        or not execution.succeeded
        or execution.result is None
    ):
        return execution

    agent_code = subagent.agent_code.upper()
    detail_code = subagent.detail_scenario_code
    handler_spec = get_scenario_handler_spec(agent_code, detail_code)

    if handler_spec is not None and handler_spec.output_handler is not None:
        safe_request_context = _build_output_request_context(
            request_context=request_context,
            employee_id=employee_id,
            session_id=session_id,
            thread_id=thread_id,
            subagent=subagent,
        )
        output_context = ScenarioMcpOutputContext(
            execution=execution,
            subagent=subagent,
            employee_id=employee_id,
            session_id=session_id,
            thread_id=thread_id,
            request_context=safe_request_context,
            workflow_results=tuple(workflow_results),
        )
        output = handler_spec.output_handler(output_context)
        answer = normalize_scenario_answer(
            output.answer,
            default_renderable_code=f"{agent_code}:{detail_code}:renderable",
        )
        if not answer.text.strip():
            raise McpResultFormatError(
                "시나리오 output handler가 비어 있는 답변을 반환했습니다: "
                f"agent_code={agent_code}, detail_scenario_code={detail_code}"
            )
        renderables = _serialize_renderables(
            answer.renderables,
            agent_code=agent_code,
            scenario_code=subagent.scenario_code,
            detail_code=detail_code,
        )
        output_code = (
            handler_spec.output_handler_code
            or f"{handler_spec.output_handler.__module__}:"
            f"{handler_spec.output_handler.__name__}"
        )
        formatted_result = {
            "format": output.result_format or QUERY_RESULT_FORMAT,
            "adapter_code": f"{agent_code}:{detail_code}:function",
            "result_formatter_code": output_code,
            "output_handler_code": output_code,
            "data": output.data,
            "parameters": redact_scenario_action_parameters(
                agent_code,
                detail_code,
                subagent.parameters,
            ),
            "request_context": safe_request_context,
            "answer_text": answer.text,
            "renderables": renderables,
            "metadata": dict(output.metadata),
        }
        logger.info(
            "======== MCP 함수형 결과 전처리 완료 | 에이전트=%s | "
            "세부시나리오=%s | outputHandler=%s | 데이터개수=%d | "
            "답변길이=%d | 확장데이터개수=%d | 본문로그=생략",
            agent_code,
            detail_code,
            output_code,
            len(output.data) if isinstance(output.data, list) else 0,
            len(answer.text),
            len(renderables),
        )
        return execution.model_copy(
            update={
                "result_format": output.result_format or QUERY_RESULT_FORMAT,
                "formatted_result": formatted_result,
            }
        )

    if handler_spec is not None and handler_spec.rag_output_handler is not None:
        logger.info(
            "======== MCP 결과 전처리 생략 | 에이전트=%s | "
            "세부시나리오=%s | 결과형식=%s | 이유=RAG원본유지",
            agent_code,
            detail_code,
            RAG_RAW_RESULT_FORMAT,
        )
        return execution.model_copy(update={"result_format": RAG_RAW_RESULT_FORMAT})

    raise McpResultFormatError(
        "MCP 결과 output handler가 등록되지 않았습니다: "
        f"agent_code={agent_code}, detail_scenario_code={detail_code}. "
        "app/mcp/scenarios/registry.py에 output_handler를 연결하세요."
    )


def _serialize_renderables(
    renderables: Sequence[Any],
    *,
    agent_code: str,
    scenario_code: str,
    detail_code: str,
) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for renderable in renderables:
        renderable_metadata = {
            **renderable.metadata,
            "agent_code": agent_code,
            "scenario_code": scenario_code,
            "detail_scenario_code": detail_code,
        }
        renderable_code = renderable.code
        if ":" not in renderable_code:
            renderable_code = f"{agent_code}:{detail_code}:{renderable_code}"
        serialized.append(
            renderable.model_copy(
                update={
                    "code": renderable_code,
                    "metadata": renderable_metadata,
                }
            ).model_dump(mode="json")
        )
    return serialized


def _build_output_request_context(
    *,
    request_context: Mapping[str, Any],
    employee_id: str,
    session_id: str,
    thread_id: str,
    subagent: SubagentResult,
) -> dict[str, Any]:
    """출력 함수에 access token을 제외한 요청 정보만 전달한다."""

    user = request_context.get("user")
    if not isinstance(user, Mapping):
        user = {}
    output_context: dict[str, Any] = {
        "employee_id": employee_id,
        "session_id": session_id,
        "thread_id": thread_id,
        "agent_code": subagent.agent_code,
        "scenario_code": subagent.scenario_code,
        "detail_scenario_code": subagent.detail_scenario_code,
        "endpoint": request_context.get("endpoint", ""),
        "recruitment_org_type_code": request_context.get(
            "recruitment_org_type_code",
            "",
        ),
        "user": {
            "id": user.get("id"),
            "name": user.get("name"),
            "deptcode": user.get("deptcode"),
            "deptname": user.get("deptname"),
        },
    }
    return output_context
