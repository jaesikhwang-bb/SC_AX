"""GenOS Gateway MCP JSON-RPC 전송과 응답 처리를 담당하는 공통 실행기."""

import hashlib
import json
import re
from collections.abc import Mapping
from time import perf_counter
from typing import Any, Literal, Protocol

import httpx

from app.config import Settings
from app.mcp.models import (
    MCP_NO_DATA_MESSAGE,
    MCP_SAFE_ERROR_MESSAGE,
    McpExecutionResult,
)
from app.observability import (
    async_timed_block,
    compact_log_preview,
    log_failure_diagnostic,
    logger,
    timed,
)
from app.scenario_actions import redact_scenario_action_parameters
from app.subagents.models import SubagentResult


McpExecutionMode = Literal["function", "function_many", "pagination"]


class McpToolExecutor(Protocol):
    """LangGraph가 구체적인 MCP 전송 방식과 무관하게 사용하는 계약."""

    async def execute(
        self,
        *,
        subagent: SubagentResult,
        employee_id: str,
        session_id: str,
        thread_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        request_context: dict[str, Any] | None = None,
        step_code: str = "FINAL",
        step_index: int = 0,
        step_count: int = 1,
        execution_mode: McpExecutionMode = "function",
        workflow_item_index: int | None = None,
        workflow_item_count: int | None = None,
        enabled: bool = True,
        unavailable_message: str = "요청한 MCP 도구가 아직 연결되지 않았습니다.",
    ) -> McpExecutionResult | None: ...

    async def aclose(self) -> None: ...


class EmptyMcpToolExecutor:
    """MCP 단계가 필요 없는 그래프 단위 테스트용 빈 구현."""

    async def execute(
        self,
        *,
        subagent: SubagentResult,
        employee_id: str,
        session_id: str,
        thread_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        request_context: dict[str, Any] | None = None,
        step_code: str = "FINAL",
        step_index: int = 0,
        step_count: int = 1,
        execution_mode: McpExecutionMode = "function",
        workflow_item_index: int | None = None,
        workflow_item_count: int | None = None,
        enabled: bool = True,
        unavailable_message: str = "요청한 MCP 도구가 아직 연결되지 않았습니다.",
    ) -> McpExecutionResult | None:
        return None

    async def aclose(self) -> None:
        return None


class GenosMcpToolExecutor:
    """Python 코드로 만든 JSON-RPC 요청을 GenOS MCP에 전송한다.

    현재 시나리오별 도구명과 arguments는 ``app/mcp/scenarios``의 handler가
    결정한다. 이 클래스는 JSON-RPC 봉투, 공통 추적 ID, HTTP 전송, 응답 파싱,
    민감정보 마스킹만 맡는다.
    MCP 응답은 httpx ``post``로 body 전체를 수신한 뒤 단일 dict로 반환한다.
    GenOS가 Content-Type을 ``text/event-stream``으로 주는 경우도 내부에서 마지막
    JSON-RPC data를 추출할 뿐, MCP chunk를 FastAPI SSE로 전달하지 않는다.
    """

    @timed("MCP 실행기 초기화")
    def __init__(self, settings: Settings) -> None:
        if settings.mcp_backend not in {"mock", "http"}:
            raise ValueError("MCP_BACKEND는 mock 또는 http만 사용할 수 있습니다.")
        if settings.mcp_backend == "http" and not settings.mcp_bearer_token:
            raise ValueError("MCP_BACKEND=http인 경우 MCP_BEARER_TOKEN이 필요합니다.")

        self._settings = settings
        mcp_url = (
            settings.genos_mcp_url if settings.mcp_backend == "http" else "mock://local"
        )
        self._http_client = (
            httpx.AsyncClient(
                timeout=settings.mcp_timeout_seconds,
                transport=httpx.AsyncHTTPTransport(
                    retries=settings.mcp_max_retries,
                ),
                headers={
                    "Authorization": f"Bearer {settings.mcp_bearer_token}",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
            if settings.mcp_backend == "http"
            else None
        )
        logger.info(
            "======== MCP 실행기 준비 완료 | 백엔드=%s | MCP주소=%s | "
            "타임아웃=%.3f초 | 자동재시도=%d회 | "
            "업무호출설정=app/mcp/scenarios | "
            "HTTP호출코드=app/mcp/client.py:GenosMcpToolExecutor.execute",
            settings.mcp_backend,
            mcp_url,
            settings.mcp_timeout_seconds,
            settings.mcp_max_retries,
        )

    @timed("MCP 도구 호출")
    async def execute(
        self,
        *,
        subagent: SubagentResult,
        employee_id: str,
        session_id: str,
        thread_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        request_context: dict[str, Any] | None = None,
        step_code: str = "FINAL",
        step_index: int = 0,
        step_count: int = 1,
        execution_mode: McpExecutionMode = "function",
        workflow_item_index: int | None = None,
        workflow_item_count: int | None = None,
        enabled: bool = True,
        unavailable_message: str = "요청한 MCP 도구가 아직 연결되지 않았습니다.",
    ) -> McpExecutionResult | None:
        context = dict(request_context or {})
        normalized_tool = str(tool_name).strip()
        normalized_step = str(step_code).strip()
        normalized_arguments = {str(key): value for key, value in arguments.items()}
        if not normalized_tool:
            raise ValueError("MCP tool_name은 비어 있을 수 없습니다.")
        if not normalized_step:
            raise ValueError("MCP step_code는 비어 있을 수 없습니다.")
        if step_count < 1 or not 0 <= step_index < step_count:
            raise ValueError(
                "MCP step 순번이 올바르지 않습니다: "
                f"index={step_index}, count={step_count}"
            )
        started_at = perf_counter()

        def complete_step(result: McpExecutionResult) -> McpExecutionResult:
            completed = result.model_copy(
                update={
                    "workflow_step_code": normalized_step,
                    "workflow_step_index": step_index,
                    "workflow_step_count": step_count,
                    "workflow_is_final": step_index == step_count - 1,
                    "workflow_execution_mode": execution_mode,
                    "workflow_item_index": workflow_item_index,
                    "workflow_item_count": workflow_item_count,
                    "workflow_is_aggregate": False,
                }
            )
            preview_source: Any = (
                completed.result
                if completed.result is not None
                else completed.user_message or completed.error or ""
            )
            logger.info(
                "FLOW MCP 도구 결과 | 도구=%s | 상태=%s | 건수=%d | "
                "소요시간=%.3f초 | 결과미리보기=%s",
                completed.tool_name,
                completed.outcome,
                _structured_data_count(completed.result),
                perf_counter() - started_at,
                compact_log_preview(preview_source, limit=100),
            )
            return completed

        request_id = build_mcp_request_id(
            project_code=self._settings.project_code,
            employee_id=employee_id,
            session_id=session_id,
            thread_id=thread_id,
            detail_scenario_code=subagent.detail_scenario_code,
            workflow_step_code=normalized_step,
            workflow_item_index=workflow_item_index,
        )
        safe_parameters = redact_scenario_action_parameters(
            subagent.agent_code,
            subagent.detail_scenario_code,
            subagent.parameters,
        )
        sensitive_values = [
            value
            for name, value in subagent.parameters.items()
            if safe_parameters.get(name) == "***MASKED***"
        ]
        secrets = _collect_sensitive_values(
            context.get("access_token"),
            self._settings.mcp_bearer_token,
            *sensitive_values,
        )
        logger.info(
            "======== MCP 호출 입력 | 추적ID=%s | 에이전트=%s | "
            "세부시나리오=%s | 함수단계=%d/%d | "
            "파라미터본문로그=생략",
            request_id,
            subagent.agent_code,
            subagent.detail_scenario_code,
            step_index + 1,
            step_count,
        )
        request_body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": normalized_tool,
                "arguments": normalized_arguments,
            },
        }
        safe_arguments = _redact_sensitive(normalized_arguments, secrets)
        logger.info(
            "FLOW MCP 도구 조회 | 도구=%s | 세부시나리오=%s | "
            "입력미리보기=%s",
            normalized_tool,
            subagent.detail_scenario_code,
            compact_log_preview(safe_arguments, limit=100),
        )
        logger.info(
            "======== MCP 요청 조립 완료 | 도구=%s | 추적ID=%s | "
            "인자개수=%d | 인자키=%s | payload본문로그=생략",
            normalized_tool,
            request_id,
            len(normalized_arguments),
            sorted(normalized_arguments),
        )
        if not enabled:
            logger.info(
                "======== 상호작용 MCP 호출 대기 | 도구=%s | 추적ID=%s | "
                "manifest enabled=false | HTTP호출=생략",
                normalized_tool,
                request_id,
            )
            return complete_step(
                McpExecutionResult(
                    backend="disabled",
                    tool_name=normalized_tool,
                    request_id=request_id,
                    arguments=safe_arguments,
                    succeeded=True,
                    user_message=unavailable_message,
                )
            )

        if self._settings.mcp_backend == "mock":
            mock_result = _build_mock_structured_content(
                normalized_tool,
                normalized_arguments,
            )
            logger.info(
                "======== MCP Mock 조회 완료 | 도구=%s | 추적ID=%s | "
                "원본결과로그=생략 | data항목수=%d | "
                "다음단계=app/mcp/result_adapters.py",
                normalized_tool,
                request_id,
                len(mock_result.get("data", [])),
            )
            return complete_step(
                McpExecutionResult(
                    backend="mock",
                    tool_name=normalized_tool,
                    request_id=request_id,
                    arguments=safe_arguments,
                    succeeded=True,
                    result=mock_result,
                )
            )

        assert self._http_client is not None
        try:
            logger.info(
                "======== MCP HTTP 호출 시작 | 코드위치=app/mcp/client.py:"
                "GenosMcpToolExecutor.execute | 도구=%s | 추적ID=%s | "
                "URL=%s | 타임아웃=%.3f초 | 자동재시도=%d회",
                tool_name,
                request_id,
                self._settings.genos_mcp_url,
                self._settings.mcp_timeout_seconds,
                self._settings.mcp_max_retries,
            )
            async with async_timed_block("MCP HTTP 응답 대기"):
                print(
                    f"[MCP 호출] tool={normalized_tool}\n"
                    + json.dumps(
                        _without_bearer_tokens(safe_arguments),
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    flush=True,
                )
                response = await self._http_client.post(
                    self._settings.genos_mcp_url,
                    json=request_body,
                )
                logger.info(
                    "======== MCP HTTP 응답 메타데이터 수신 | 도구=%s | "
                    "추적ID=%s | 상태코드=%d | Content-Type=%s | "
                    "응답길이=%d | 원본응답로그=생략",
                    tool_name,
                    request_id,
                    response.status_code,
                    response.headers.get("content-type"),
                    len(response.text),
                )
                response.raise_for_status()
            logger.info(
                "======== MCP HTTP 응답 수신 | 도구=%s | 추적ID=%s | "
                "상태코드=%d | Content-Type=%s",
                tool_name,
                request_id,
                response.status_code,
                response.headers.get("content-type"),
            )

            envelope = _parse_mcp_response(response)
            logger.info(
                "======== MCP 응답 단건 변환 완료 | 도구=%s | 추적ID=%s | "
                "내부결과형식=dict | MCP결과프론트스트리밍=안함 | "
                "원본결과로그=생략 | 오류응답존재=%s",
                tool_name,
                request_id,
                envelope.get("error") is not None,
            )
            if envelope.get("error") is not None:
                error = _json_for_log(_redact_sensitive(envelope["error"], secrets))
                logger.error(
                    "======== MCP 도구 오류 응답 | 도구=%s | 추적ID=%s | "
                    "오류본문로그=생략",
                    tool_name,
                    request_id,
                )
                return complete_step(
                    _mcp_error_result(
                        backend="http",
                        tool_name=tool_name,
                        request_id=request_id,
                        arguments=safe_arguments,
                        error=error,
                    )
                )

            result_envelope = envelope.get("result")
            is_error = isinstance(result_envelope, dict) and bool(
                result_envelope.get("isError", False)
            )
            # 운영계 표준 계약인 JSON-RPC result.structuredContent 객체만
            # 허용한다. 망별 문자열 재파싱은 잘못된 서버 응답을 숨길 수 있어
            # 의도적으로 지원하지 않는다.
            structured_content = _extract_mcp_structured_content(result_envelope)

            # _parse_mcp_response()가 반환한 전체 응답 기준 업무 코드 경로는
            # result.structuredContext.result.code다. 여기서는 이미 바깥쪽
            # result를 꺼낸 result_envelope을 전달한다.
            # 실제 값은 숫자 또는 문자열로 올 수 있으므로 문자열로 정규화한다.
            business_code = _extract_mcp_business_code(result_envelope)
            if business_code is None and structured_content is not None:
                business_code = _extract_mcp_business_code(
                    {"structuredContent": structured_content}
                )
            if business_code == "1001":
                logger.info(
                    "======== MCP 조회 데이터 없음 | 도구=%s | 추적ID=%s | "
                    "업무코드=%s | 사용자답변본문로그=생략",
                    tool_name,
                    request_id,
                    business_code,
                )
                return complete_step(
                    McpExecutionResult(
                        backend="http",
                        tool_name=tool_name,
                        request_id=request_id,
                        arguments=safe_arguments,
                        succeeded=True,
                        outcome="NO_DATA",
                        business_code=business_code,
                        user_message=MCP_NO_DATA_MESSAGE,
                        result=(
                            _redact_sensitive(structured_content, secrets)
                            if isinstance(structured_content, dict)
                            else None
                        ),
                    )
                )

            if not is_error and not isinstance(structured_content, dict):
                raise ValueError(
                    "GenOS MCP 응답에서 result.structuredContent 객체를 "
                    "찾지 못했습니다."
                )

            safe_result = (
                _redact_sensitive(structured_content, secrets) if not is_error else None
            )
            logger.info(
                "======== MCP 호출 최종 결과 | 도구=%s | 추적ID=%s | "
                "성공=%s | data항목수=%d | 원본결과로그=생략 | "
                "다음단계=조회결과정제",
                tool_name,
                request_id,
                not is_error,
                _structured_data_count(safe_result),
            )
            return complete_step(
                McpExecutionResult(
                    backend="http",
                    tool_name=tool_name,
                    request_id=request_id,
                    arguments=safe_arguments,
                    succeeded=not is_error,
                    outcome="ERROR" if is_error else "SUCCESS",
                    business_code=business_code,
                    user_message=MCP_SAFE_ERROR_MESSAGE if is_error else None,
                    result=safe_result,
                    error=(
                        "MCP 도구가 isError=true를 반환했습니다."
                        if is_error
                        else None
                    ),
                )
            )
        except Exception as exc:
            safe_error = str(_redact_sensitive(str(exc), secrets))
            likely_cause, corrective_action = _mcp_failure_guidance(exc)
            response_context: dict[str, Any] = {}
            if isinstance(exc, httpx.HTTPStatusError):
                response_context = {
                    "status_code": exc.response.status_code,
                    "response_length": len(exc.response.text),
                }
            log_failure_diagnostic(
                stage="GenOS MCP HTTP 호출 또는 응답 파싱",
                code_location="app/mcp/client.py:GenosMcpToolExecutor.execute",
                exc=exc,
                likely_cause=likely_cause,
                corrective_action=corrective_action,
                retry_count=self._settings.mcp_max_retries,
                context={
                    "tool_name": tool_name,
                    "mcp_request_id": request_id,
                    "url": self._settings.genos_mcp_url,
                    "timeout_seconds": self._settings.mcp_timeout_seconds,
                    "argument_keys": sorted(arguments),
                    **response_context,
                },
            )
            # 오류 원문과 스택은 위 진단 로그에 남긴다. 프론트에는 내부 구조를
            # 노출하지 않고 최종 답변 단계에서 안전한 고정 문구만 스트리밍한다.
            return complete_step(
                _mcp_error_result(
                    backend="http",
                    tool_name=tool_name,
                    request_id=request_id,
                    arguments=safe_arguments,
                    error=safe_error,
                )
            )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            logger.info("======== MCP HTTP 클라이언트 종료 완료")


def _collect_sensitive_values(*values: Any) -> list[Any]:
    """로그와 외부 결과에서 숨길 실제 민감값만 모은다."""

    return [value for value in values if value is not None and str(value)]


def _structured_data_count(value: Any) -> int:
    """MCP 본문을 출력하지 않고 대표 data 배열의 건수만 반환한다."""

    if not isinstance(value, Mapping):
        return 0
    candidates: list[Any] = [value.get("data")]
    nested = value.get("result")
    if isinstance(nested, Mapping):
        candidates.append(nested.get("data"))
    return max(
        (len(candidate) for candidate in candidates if isinstance(candidate, list)),
        default=0,
    )


def _build_mock_structured_content(
    tool_name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """도구별 예정 structuredContent를 네트워크 없이 재현한다."""

    if tool_name == "databricks_hybrid_search":
        query = str(arguments.get("query", ""))
        index_name = str(arguments.get("index_name", ""))
        filters = arguments.get("filters")
        category = (
            str(filters.get("document_category", ""))
            if isinstance(filters, Mapping)
            else ""
        )
        # 현재 RAG handler는 실제 MCP 계약에 맞춰 filter를 JSON 문자열로
        # 전달한다. mock은 filter 내부를 업무 분류에 재사용하지 않고, payload에
        # 명시된 index_name으로 RP/자격기준 테스트 문서를 구분한다.
        if category == "RP" or index_name == "rp_documents":
            return {
                "data": [
                    {
                        "document_id": "RP-POLICY-001",
                        "title": "RP 자동납부 업무 운영기준",
                        "content": (
                            "테스트 문서입니다. RP 자동납부 신청과 변경은 대상 "
                            "요금의 운영기준 및 최신 업무지침을 확인하여 안내합니다."
                        ),
                        "source_uri": "databricks://rp/operation-policy",
                        "updated_at": "2026-08-01",
                        "score": 0.95,
                        "matched_query": query,
                    },
                    {
                        "document_id": "RP-POLICY-002",
                        "title": "RP 환산 업무 안내",
                        "content": (
                            "테스트 문서입니다. RP 환산 항목과 반영 기준은 승인된 "
                            "최신 RP 업무 문서를 기준으로 확인합니다."
                        ),
                        "source_uri": "databricks://rp/conversion-policy",
                        "updated_at": "2026-07-20",
                        "score": 0.89,
                        "matched_query": query,
                    },
                ],
                "search_metadata": {
                    "query_type": arguments.get("query_type", "HYBRID"),
                    "index_name": arguments.get("index_name", ""),
                    "result_count": 2,
                },
            }
        return {
            "data": [
                {
                    "document_id": "QUAL-POLICY-001",
                    "title": "개인회원 입회 자격기준 업무지침",
                    "content": (
                        "테스트 문서입니다. 개인회원 입회 심사 시 회원 유형과 "
                        "본인확인 서류를 확인하고, 세부 자격은 최신 업무지침을 "
                        "기준으로 안내합니다."
                    ),
                    "source_uri": ("databricks://qualification/personal-member-policy"),
                    "updated_at": "2026-08-01",
                    "score": 0.97,
                    "matched_query": query,
                },
                {
                    "document_id": "QUAL-POLICY-002",
                    "title": "외국인 회원 입회 확인서류 안내",
                    "content": (
                        "테스트 문서입니다. 외국인 회원은 체류자격과 "
                        "본인확인에 필요한 유효 서류를 확인해야 하며, 실제 "
                        "적용 기준은 조회된 최신 규정을 따릅니다."
                    ),
                    "source_uri": ("databricks://qualification/foreigner-policy"),
                    "updated_at": "2026-07-15",
                    "score": 0.91,
                    "matched_query": query,
                },
            ],
            "search_metadata": {
                "query_type": arguments.get("query_type", "HYBRID"),
                "index_name": arguments.get("index_name", ""),
                "result_count": 2,
            },
        }

    return {
        "data": [
            {
                "objId": "column1",
                "objType": "string",
                "objVal": arguments.get("param1", ""),
            },
            {
                "objId": "column2",
                "objType": "string",
                "objVal": arguments.get("param2", ""),
            },
            # 허용 목록 필터가 불필요한 컬럼을 제거하는지 함께 검증한다.
            {
                "objId": "unusedColumn",
                "objType": "string",
                "objVal": "정제 단계에서 제외되는 테스트 값",
            },
        ]
    }


def _without_bearer_tokens(value: Any) -> Any:
    """출력용 복사본에서 중첩된 bearerToken 키도 제거한다."""

    if isinstance(value, Mapping):
        return {
            key: _without_bearer_tokens(item)
            for key, item in value.items()
            if re.sub(r"[^a-z]", "", str(key).casefold())
            not in {"bearertoken", "beardertoken"}
        }
    if isinstance(value, (list, tuple)):
        return [_without_bearer_tokens(item) for item in value]
    return value


def _redact_sensitive(value: Any, secrets: list[Any]) -> Any:
    """로그·CSV·응답에 토큰이 남지 않도록 중첩 값까지 마스킹한다."""

    secret_texts = {str(secret) for secret in secrets}
    if isinstance(value, Mapping):
        return {
            str(key): _redact_sensitive(item, secrets) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item, secrets) for item in value)
    if isinstance(value, str):
        redacted = value
        for secret in secret_texts:
            redacted = redacted.replace(secret, "***MASKED***")
        return redacted
    return "***MASKED***" if str(value) in secret_texts else value


def _json_for_log(value: Any) -> str:
    """MCP 오류 결과에 넣을 dict/list를 한글이 보존된 JSON으로 만든다."""

    return json.dumps(value, ensure_ascii=False, default=str)


def _mcp_failure_guidance(exc: BaseException) -> tuple[str, str]:
    """MCP 예외 유형별 가능한 원인과 운영자가 확인할 지점을 반환한다."""

    if isinstance(exc, httpx.TimeoutException):
        return (
            "GenOS MCP가 MCP_TIMEOUT_SECONDS 안에 연결 또는 응답을 완료하지 못함",
            ".env의 MCP_TIMEOUT_SECONDS와 MCP_ID를 확인하고 GenOS MCP 도구의 "
            "실행 시간 및 네트워크 상태를 확인하세요.",
        )
    if isinstance(exc, httpx.ConnectError):
        return (
            "GenOS MCP 주소에 TCP/TLS 연결 실패 또는 DNS·폐쇄망 경로 문제",
            ".env의 GENOS_URL/MCP_ID, 인증서, DNS와 방화벽을 확인하세요.",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            cause = "MCP Bearer 토큰이 없거나 만료·권한 부족으로 인증 실패"
            action = ".env의 MCP_BEARER_TOKEN과 MCP 접근 권한을 확인하세요."
        elif status == 404:
            cause = "GENOS_URL 또는 MCP_ID로 만든 MCP 엔드포인트가 존재하지 않음"
            action = ".env의 GENOS_URL과 MCP_ID를 확인하세요."
        elif status in {400, 422}:
            cause = "MCP tool_name 또는 arguments가 도구 계약과 불일치"
            action = (
                "app/mcp/scenarios의 해당 세부 시나리오 handler에서 tool_name과 "
                "arguments를 MCP 도구 명세와 비교하세요."
            )
        else:
            cause = f"GenOS MCP가 HTTP {status} 오류를 반환"
            action = "응답미리보기와 GenOS MCP 서버 로그를 같은 추적ID로 확인하세요."
        return cause, action
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return (
            "MCP 응답이 JSON-RPC 또는 SSE data JSON 형식과 불일치",
            "app/mcp/client.py:_parse_mcp_response와 로그의 Content-Type/"
            "응답미리보기를 확인하고 MCP 반환 형식을 수정하세요.",
        )
    return (
        "MCP 요청 조립·HTTP 호출·응답 변환 중 예상하지 못한 애플리케이션 오류",
        "현재 로그의 스택트레이스 마지막 코드 줄과 app/mcp/scenarios 및 "
        "app/mcp/client.py를 확인하세요.",
    )


def build_mcp_request_id(
    *,
    project_code: str,
    employee_id: str,
    session_id: str,
    thread_id: str,
    detail_scenario_code: str | None = None,
    workflow_step_code: str | None = None,
    workflow_item_index: int | None = None,
) -> str:
    """사원·대화·시나리오·workflow step을 역추적할 JSON-RPC id를 만든다."""

    raw = f"{project_code}:{employee_id}:{session_id}:{thread_id}"
    if detail_scenario_code:
        raw = f"{raw}:{detail_scenario_code}"
    if workflow_step_code:
        raw = f"{raw}:{workflow_step_code}"
    if workflow_item_index is not None:
        raw = f"{raw}:ITEM-{workflow_item_index + 1:04d}"
    normalized = re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)
    if len(normalized) <= 240:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{normalized[:223]}:{digest}"


def _parse_mcp_response(response: httpx.Response) -> dict[str, Any]:
    """전체 수신된 JSON 또는 SSE body를 하나의 JSON-RPC dict로 변환한다.

    여기서 SSE는 GenOS MCP 서버가 사용하는 응답 포맷일 뿐이다. httpx streaming
    API를 사용하지 않으므로 data 줄을 실시간 전달하지 않고 응답 완료 후 마지막
    JSON-RPC 객체 한 건만 반환한다.
    """

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


def _extract_mcp_structured_content(result_envelope: Any) -> dict[str, Any] | None:
    """운영계 MCP 계약의 ``result.structuredContent`` 객체만 추출한다."""

    if not isinstance(result_envelope, Mapping):
        return None

    structured_content = result_envelope.get("structuredContent")
    if isinstance(structured_content, Mapping):
        return dict(structured_content)
    return None


def _extract_mcp_business_code(result_envelope: Any) -> str | None:
    """GenOS MCP 업무 결과 코드 경로에서 code를 안전하게 추출한다.

    전체 응답 기준 기본 계약은 ``result.structuredContext.result.code``다.
    일부 도구가 같은 메타데이터를 structuredContent에 넣는 경우도 허용해
    도구별 차이를 줄인다.
    """

    if not isinstance(result_envelope, Mapping):
        return None
    for container_name in ("structuredContext", "structuredContent"):
        container = result_envelope.get(container_name)
        if not isinstance(container, Mapping):
            continue
        business_result = container.get("result")
        if not isinstance(business_result, Mapping):
            continue
        code = business_result.get("code")
        if code is None:
            continue
        normalized = str(code).strip()
        return normalized or None
    return None


def _mcp_error_result(
    *,
    backend: str,
    tool_name: str,
    request_id: str,
    arguments: dict[str, Any],
    error: str,
) -> McpExecutionResult:
    """로그·추적용 오류와 사용자용 안전 문구를 분리한 실패 결과를 만든다."""

    return McpExecutionResult(
        backend=backend,
        tool_name=tool_name,
        request_id=request_id,
        arguments=arguments,
        succeeded=False,
        outcome="ERROR",
        user_message=MCP_SAFE_ERROR_MESSAGE,
        result=None,
        error=error,
    )


@timed("MCP 실행기 생성")
def create_mcp_tool_executor(settings: Settings) -> McpToolExecutor:
    """코드 기반 MCP 실행기를 생성한다."""

    return GenosMcpToolExecutor(settings)
