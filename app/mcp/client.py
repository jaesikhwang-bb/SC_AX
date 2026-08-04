"""guide.ipynb 방식의 GenOS Gateway MCP JSON-RPC 공통 실행기."""

import hashlib
import json
import re
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.mcp.models import McpExecutionResult
from app.observability import async_timed_block, logger, timed
from app.subagents.models import SubagentResult
from app.subagents.prompt_loader import (
    ScenarioPromptBundle,
    SubagentPromptLoader,
)


class McpToolExecutor(Protocol):
    """LangGraph가 구체적인 MCP 전송 방식과 무관하게 사용하는 계약."""

    async def execute(
        self,
        *,
        subagent: SubagentResult,
        employee_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> McpExecutionResult | None: ...

    async def aclose(self) -> None: ...


class EmptyMcpToolExecutor:
    """MCP 단계가 필요 없는 그래프 단위 테스트용 빈 구현."""

    async def execute(
        self,
        *,
        subagent: SubagentResult,
        employee_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> McpExecutionResult | None:
        return None

    async def aclose(self) -> None:
        return None


class ManifestMcpToolExecutor:
    """모든 서브에이전트 manifest의 세부 시나리오 MCP 선언을 실행한다."""

    @timed("MCP 실행기 초기화")
    def __init__(
        self,
        settings: Settings,
        bundles: dict[str, ScenarioPromptBundle],
    ) -> None:
        if settings.mcp_backend not in {"mock", "http"}:
            raise ValueError("MCP_BACKEND는 mock 또는 http만 사용할 수 있습니다.")
        if settings.mcp_backend == "http" and not settings.mcp_bearer_token:
            raise ValueError(
                "MCP_BACKEND=http인 경우 MCP_BEARER_TOKEN이 필요합니다."
            )

        self._settings = settings
        self._tool_specs: dict[tuple[str, str], dict[str, Any]] = {}
        for agent_code, bundle in bundles.items():
            for scenario in bundle.manifest["scenarios"]:
                for detail in scenario["details"]:
                    self._tool_specs[
                        (agent_code.upper(), str(detail["code"]))
                    ] = dict(detail["mcp"])

        self._http_client = (
            httpx.AsyncClient(
                timeout=settings.mcp_timeout_seconds,
                headers={
                    "Authorization": (
                        f"Bearer {settings.mcp_bearer_token}"
                    ),
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            if settings.mcp_backend == "http"
            else None
        )
        logger.info(
            "======== MCP 실행기 준비 완료 | 백엔드=%s | mcp_id=%d | "
            "세부시나리오매핑=%d",
            settings.mcp_backend,
            settings.mcp_id,
            len(self._tool_specs),
        )

    @timed("MCP 도구 호출")
    async def execute(
        self,
        *,
        subagent: SubagentResult,
        employee_id: str,
        conversation_id: str,
        thread_id: str,
    ) -> McpExecutionResult | None:
        spec = self._tool_specs.get(
            (subagent.agent_code.upper(), subagent.detail_scenario_code)
        )
        if spec is None:
            logger.info(
                "======== MCP 도구 매핑 없음 | 에이전트=%s | 세부시나리오=%s",
                subagent.agent_code,
                subagent.detail_scenario_code,
            )
            return None

        context = {
            "project_code": self._settings.project_code,
            "employee_id": employee_id,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "agent_code": subagent.agent_code,
            "scenario_code": subagent.scenario_code,
            "detail_scenario_code": subagent.detail_scenario_code,
        }
        arguments = _resolve_arguments(
            spec.get("arguments", {}),
            subagent.parameters,
            context,
        )
        request_id = build_mcp_request_id(
            project_code=self._settings.project_code,
            employee_id=employee_id,
            conversation_id=conversation_id,
            thread_id=thread_id,
            detail_scenario_code=subagent.detail_scenario_code,
        )
        tool_name = str(spec["tool_name"])
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        logger.info(
            "======== MCP Payload 생성 | 도구=%s | 추적ID=%s | payload=%s",
            tool_name,
            request_id,
            payload,
        )

        if self._settings.mcp_backend == "mock":
            # 단위 테스트에서만 사용하는 대체 응답이다. 실제 서비스의 기본값은
            # http이며, 목업 화면 또한 GenOS MCP를 실제로 호출한다.
            return McpExecutionResult(
                backend="mock",
                tool_name=tool_name,
                request_id=request_id,
                arguments=arguments,
                succeeded=True,
                result=dict(arguments),
            )

        assert self._http_client is not None
        try:
            async with async_timed_block("MCP HTTP 응답 대기"):
                response = await self._http_client.post(
                    self._settings.genos_mcp_url,
                    json=payload,
                )
                response.raise_for_status()
            envelope = _parse_mcp_response(response)
            if envelope.get("error") is not None:
                return McpExecutionResult(
                    backend="http",
                    tool_name=tool_name,
                    request_id=request_id,
                    arguments=arguments,
                    succeeded=False,
                    result=None,
                    error=json.dumps(
                        envelope["error"],
                        ensure_ascii=False,
                    ),
                )
            result_envelope = envelope.get("result")
            is_error = (
                isinstance(result_envelope, dict)
                and bool(result_envelope.get("isError", False))
            )
            structured_content = (
                result_envelope.get("structuredContent")
                if isinstance(result_envelope, dict)
                else None
            )
            if not is_error and not isinstance(structured_content, dict):
                return McpExecutionResult(
                    backend="http",
                    tool_name=tool_name,
                    request_id=request_id,
                    arguments=arguments,
                    succeeded=False,
                    result=None,
                    error=(
                        "GenOS MCP 응답의 result.structuredContent가 "
                        "dict 형식이 아닙니다."
                    ),
                )
            return McpExecutionResult(
                backend="http",
                tool_name=tool_name,
                request_id=request_id,
                arguments=arguments,
                succeeded=not is_error,
                # guide.ipynb의
                # obj["result"]["structuredContent"]와 동일한 값이다.
                result=structured_content if not is_error else None,
                error="MCP 도구가 isError=true를 반환했습니다."
                if is_error
                else None,
            )
        except Exception as exc:
            # MCP 장애가 이미 완료된 마스터·서브에이전트 분류를 500으로
            # 바꾸지 않도록 구조화된 실패 결과로 전환한다.
            logger.info(
                "======== MCP 호출 실패 | 도구=%s | 추적ID=%s | "
                "오류유형=%s | 오류=%s",
                tool_name,
                request_id,
                type(exc).__name__,
                exc,
            )
            return McpExecutionResult(
                backend="http",
                tool_name=tool_name,
                request_id=request_id,
                arguments=arguments,
                succeeded=False,
                result=None,
                error=str(exc),
            )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            logger.info("======== MCP HTTP 클라이언트 종료 완료")


def _resolve_arguments(
    mappings: dict[str, Any],
    parameters: dict[str, str | None],
    context: dict[str, str],
) -> dict[str, Any]:
    """manifest의 literal·parameter·context 선언을 실제 MCP 인자로 만든다."""

    arguments: dict[str, Any] = {}
    for argument_name, mapping in mappings.items():
        source = str(mapping["source"])
        if source == "literal":
            arguments[str(argument_name)] = mapping.get("value")
        elif source == "parameter":
            arguments[str(argument_name)] = parameters.get(
                str(mapping["name"])
            )
        elif source == "context":
            arguments[str(argument_name)] = context.get(str(mapping["name"]))
        else:
            raise ValueError(f"지원하지 않는 MCP 인자 source입니다: {source}")
    return arguments


def build_mcp_request_id(
    *,
    project_code: str,
    employee_id: str,
    conversation_id: str,
    thread_id: str,
    detail_scenario_code: str | None = None,
) -> str:
    """사원·대화·단일 실행을 사람이 역추적할 수 있는 JSON-RPC id를 만든다."""

    raw = f"{project_code}:{employee_id}:{conversation_id}:{thread_id}"
    if detail_scenario_code:
        raw = f"{raw}:{detail_scenario_code}"
    normalized = re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)
    if len(normalized) <= 240:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{normalized[:223]}:{digest}"


def _parse_mcp_response(response: httpx.Response) -> dict[str, Any]:
    """일반 JSON과 guide.ipynb가 허용한 SSE 응답을 모두 JSON-RPC로 변환한다."""

    content_type = response.headers.get("content-type", "").casefold()
    if "application/json" in content_type:
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise ValueError("MCP JSON-RPC 응답이 객체가 아닙니다.")
        return parsed

    candidates: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            candidates.append(parsed)
    if not candidates:
        raise ValueError("MCP SSE 응답에서 JSON-RPC 데이터를 찾지 못했습니다.")
    return candidates[-1]


@timed("MCP 실행기 생성")
def create_mcp_tool_executor(settings: Settings) -> McpToolExecutor:
    """활성 서브에이전트 manifest 전체를 공통 MCP 실행기로 연결한다."""

    return ManifestMcpToolExecutor(
        settings,
        SubagentPromptLoader().load_all(),
    )
