"""Python 함수형 MCP 시나리오가 사용하는 안전한 공통 실행 문맥.

시나리오 함수는 이 문맥의 ``call``/``call_many``/``paginate``만 사용한다.
JSON-RPC envelope, 요청 ID, 인증, HTTP/SSE 응답 파싱, 민감값 마스킹은 기존
``GenosMcpToolExecutor``를 그대로 통과하므로 업무 함수가 전송 코드를 복제하지
않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.mcp.models import (
    MCP_NO_DATA_MESSAGE,
    MCP_SAFE_ERROR_MESSAGE,
    McpExecutionResult,
)
from app.renderables import extract_data_items
from app.subagents.models import SubagentResult


HandlerTraceCallback = Callable[
    [str, dict[str, Any], Sequence[McpExecutionResult]],
    None,
]
NextArgumentsFactory = Callable[
    [McpExecutionResult, int],
    Mapping[str, Any] | None,
]
FunctionExecutionMode = Literal[
    "function",
    "function_many",
    "pagination",
]
RagSearchPass = Literal["FIRST", "SECOND"]


class ScenarioMcpHandlerError(ValueError):
    """함수형 MCP handler 계약 위반을 나타낸다."""


@dataclass(frozen=True)
class ScenarioMcpHandlerOutcome:
    """함수형 handler의 terminal 결과와 전체 호출 원장."""

    terminal: McpExecutionResult
    results: tuple[McpExecutionResult, ...]


class ScenarioMcpHandlerContext:
    """도구별 자유로운 호출 흐름과 공통 안전 경계를 함께 제공한다."""

    def __init__(
        self,
        *,
        handler_code: str,
        executor: Any,
        subagent: SubagentResult,
        employee_id: str,
        session_id: str,
        thread_id: str,
        request_context: Mapping[str, Any],
        refined_query: str = "",
        rag_search_pass: RagSearchPass = "FIRST",
        initial_results: Sequence[McpExecutionResult] = (),
        trace_callback: HandlerTraceCallback | None = None,
    ) -> None:
        self.handler_code = str(handler_code).strip()
        self.executor = executor
        self.subagent = subagent
        self.employee_id = employee_id
        self.session_id = session_id
        self.thread_id = thread_id
        # 마스터가 대화 문맥과 오타를 반영해 만든 질문이다. RAG 검색 handler는
        # 서브에이전트가 다시 만든 문장 대신 이 값을 검색 본문으로 사용한다.
        self.refined_query = str(refined_query).strip()
        self.rag_search_pass = rag_search_pass
        self.request_context = dict(request_context)
        self._trace_callback = trace_callback
        self._results = [
            McpExecutionResult.model_validate(result)
            for result in initial_results
        ]
        self._step_count = max(
            (
                result.workflow_step_index + 1
                for result in self._results
            ),
            default=0,
        )

    @property
    def results(self) -> tuple[McpExecutionResult, ...]:
        """현재까지 생성된 개별 호출·집계 결과를 실행 순서대로 반환한다."""

        return tuple(self._results)

    def results_for(self, step_code: str) -> list[McpExecutionResult]:
        """특정 함수형 step의 호출·집계 원장을 반환한다."""

        normalized = str(step_code).strip()
        return [
            result
            for result in self._results
            if result.workflow_step_code == normalized
        ]

    def checkpoint(self, step_code: str) -> McpExecutionResult | None:
        """복원·현재 원장에서 step의 terminal 결과를 반환한다.

        단건은 마지막 결과, ``call_many``/``paginate``는 aggregate를 우선한다.
        action 재진입 뒤 handler가 처음부터 실행돼도 동일 step의 MCP HTTP 호출은
        다시 수행하지 않는다. 새 입력으로 재조회해야 하는 step은 action 정의의
        ``invalidate_step_codes``로 Redis 원장에서 제거해야 한다.
        """

        candidates = self.results_for(step_code)
        if not candidates:
            return None
        aggregates = [item for item in candidates if item.workflow_is_aggregate]
        return (aggregates or candidates)[-1]

    def _reuse_checkpoint(
        self,
        *,
        step_code: str,
        expected_mode: FunctionExecutionMode,
    ) -> McpExecutionResult | None:
        checkpoint = self.checkpoint(step_code)
        if checkpoint is None:
            return None
        if checkpoint.workflow_execution_mode != expected_mode:
            raise ScenarioMcpHandlerError(
                "같은 step_code를 서로 다른 실행 방식으로 재사용할 수 없습니다: "
                f"step={step_code}, saved={checkpoint.workflow_execution_mode}, "
                f"requested={expected_mode}"
            )
        self._trace(
            "MCP함수체크포인트재사용",
            {
                "handlerCode": self.handler_code,
                "stepCode": step_code,
                "toolName": checkpoint.tool_name,
                "requestId": checkpoint.request_id,
                "outcome": checkpoint.outcome,
                "backend": checkpoint.backend,
            },
        )
        return checkpoint

    async def call(
        self,
        *,
        step_code: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        enabled: bool = True,
        unavailable_message: str = (
            "요청한 MCP 도구가 아직 연결되지 않았습니다."
        ),
    ) -> McpExecutionResult:
        """도구를 한 번 호출한다. tool/arguments 모두 Python 함수가 결정한다."""

        restored = self._reuse_checkpoint(
            step_code=step_code,
            expected_mode="function",
        )
        if restored is not None:
            return restored
        step_index = self._next_step_index(step_code)
        result = await self._invoke(
            step_code=step_code,
            step_index=step_index,
            tool_name=tool_name,
            arguments=arguments,
            mode="function",
            enabled=enabled,
            unavailable_message=unavailable_message,
        )
        self._append_result("MCP함수호출완료", result)
        return result

    def fixed_message(
        self,
        *,
        step_code: str,
        message: str,
        business_code: str | None = None,
    ) -> McpExecutionResult:
        """외부 MCP를 호출하지 않고 시나리오 고정안내를 terminal로 만든다.

        입력 파라미터가 없어 조회 자체를 시작할 수 없는 경우처럼 오류나 HITL이
        아니라 즉시 안내하고 종료해야 하는 시나리오에서 사용한다. 결과를 일반
        MCP 원장과 같은 계약으로 기록하므로 그래프와 AnswerService에 별도 분기를
        추가하지 않아도 ``user_message``가 최종 답변으로 전달된다.
        """

        normalized_step = str(step_code).strip()
        normalized_message = str(message).strip()
        if not normalized_step:
            raise ScenarioMcpHandlerError("fixed_message.step_code는 비어 있을 수 없습니다.")
        if not normalized_message:
            raise ScenarioMcpHandlerError("fixed_message.message는 비어 있을 수 없습니다.")

        restored = self._reuse_checkpoint(
            step_code=normalized_step,
            expected_mode="function",
        )
        if restored is not None:
            return restored

        step_index = self._next_step_index(normalized_step)
        result = McpExecutionResult(
            backend="handler",
            tool_name=self.handler_code,
            request_id=(
                f"{self.thread_id}:{self.subagent.detail_scenario_code}:"
                f"FIXED-{step_index + 1:04d}"
            ),
            arguments={},
            succeeded=True,
            outcome="SUCCESS",
            business_code=(
                str(business_code).strip() if business_code is not None else None
            )
            or None,
            user_message=normalized_message,
            result=None,
            result_format="fixed_message",
            workflow_step_code=normalized_step,
            workflow_step_index=step_index,
            workflow_step_count=self._step_count,
            workflow_is_final=False,
            workflow_execution_mode="function",
            workflow_handler_code=self.handler_code,
        )
        self._append_result("MCP함수고정안내완료", result)
        return result

    async def call_many(
        self,
        *,
        step_code: str,
        tool_name: str,
        arguments_list: Sequence[Mapping[str, Any]],
        error_policy: Literal["fail_fast", "continue"] = "fail_fast",
        max_items: int = 100,
        enabled: bool = True,
        unavailable_message: str = (
            "요청한 MCP 도구가 아직 연결되지 않았습니다."
        ),
    ) -> McpExecutionResult:
        """Python 함수가 만든 0~N arguments로 같은 도구를 순서대로 호출한다."""

        restored = self._reuse_checkpoint(
            step_code=step_code,
            expected_mode="function_many",
        )
        if restored is not None:
            return restored
        normalized_arguments = self._validate_arguments_list(
            arguments_list,
            max_items=max_items,
            label=f"{step_code}.arguments_list",
        )
        step_index = self._next_step_index(step_code)
        children: list[McpExecutionResult] = []
        fail_fast_terminal: McpExecutionResult | None = None
        for item_index, arguments in enumerate(normalized_arguments):
            result = await self._invoke(
                step_code=step_code,
                step_index=step_index,
                tool_name=tool_name,
                arguments=arguments,
                mode="function_many",
                item_index=item_index,
                item_count=len(normalized_arguments),
                enabled=enabled,
                unavailable_message=unavailable_message,
            )
            children.append(result)
            self._append_result("MCP함수다중호출완료", result)
            failed = (
                not result.succeeded
                or result.outcome != "SUCCESS"
                or result.backend == "disabled"
            )
            if result.backend == "disabled" or (
                failed and error_policy == "fail_fast"
            ):
                fail_fast_terminal = result
                break

        if fail_fast_terminal is not None:
            return fail_fast_terminal

        aggregate = self._aggregate(
            step_code=step_code,
            step_index=step_index,
            tool_name=tool_name,
            mode="function_many",
            requested_count=len(normalized_arguments),
            results=children,
            metadata={
                "errorPolicy": error_policy,
                "requestedCount": len(normalized_arguments),
            },
        )
        self._append_result("MCP함수다중결과집계완료", aggregate)
        return aggregate

    def combine_results(
        self,
        *,
        step_code: str,
        tool_name: str,
        results: Sequence[McpExecutionResult],
        metadata: Mapping[str, Any] | None = None,
    ) -> McpExecutionResult:
        """이미 실행된 여러 terminal 결과를 시나리오의 최종 결과로 합친다.

        코드별 pagination처럼 MCP 호출 단위가 이중으로 반복되는 시나리오에서
        마지막 결과만 반환하면 output 계층은 앞선 NO_DATA를 알 수 없다. 이 함수는
        각 반복의 terminal 결과를 다시 집계해 전체가 NO_DATA인지 직접 계산한다.
        """

        normalized_results = list(results)
        unknown_results = [
            result for result in normalized_results if result not in self._results
        ]
        if unknown_results:
            raise ScenarioMcpHandlerError(
                "combine_results에는 현재 context가 실행한 결과만 전달할 수 있습니다."
            )
        step_index = self._next_step_index(step_code)
        aggregate = self._aggregate(
            step_code=step_code,
            step_index=step_index,
            tool_name=tool_name,
            mode="function_many",
            requested_count=len(normalized_results),
            results=normalized_results,
            metadata={
                "combinedTerminalCount": len(normalized_results),
                "sourceStepCodes": [
                    result.workflow_step_code for result in normalized_results
                ],
                **dict(metadata or {}),
            },
        )
        self._append_result("MCP함수최종결과집계완료", aggregate)
        return aggregate

    async def paginate(
        self,
        *,
        step_code: str,
        tool_name: str,
        initial_arguments: Mapping[str, Any],
        next_arguments: NextArgumentsFactory,
        max_pages: int = 100,
        error_policy: Literal["fail_fast", "continue"] = "fail_fast",
        enabled: bool = True,
        unavailable_message: str = (
            "요청한 MCP 도구가 아직 연결되지 않았습니다."
        ),
    ) -> McpExecutionResult:
        """도구 전용 next-key 함수로 페이지를 반복하고 결과를 한 건으로 합친다.

        ``next_arguments``는 현재 페이지 원본 결과를 해석해 다음 호출에 추가할
        arguments를 반환한다. 도구마다 next key 이름·개수·위치가 달라도 이
        callback만 해당 업무 함수 옆에서 바꾸면 된다. ``None`` 또는 빈 dict면
        마지막 페이지다.
        """

        restored = self._reuse_checkpoint(
            step_code=step_code,
            expected_mode="pagination",
        )
        if restored is not None:
            return restored
        if max_pages < 1 or max_pages > 1000:
            raise ScenarioMcpHandlerError("max_pages는 1~1000이어야 합니다.")
        base_arguments = self._validate_arguments(
            initial_arguments,
            label=f"{step_code}.initial_arguments",
        )
        step_index = self._next_step_index(step_code)
        page_arguments = dict(base_arguments)
        pages: list[McpExecutionResult] = []
        cursor_signatures: set[str] = set()
        stop_reason = "max_pages"
        fail_fast_terminal: McpExecutionResult | None = None

        for page_index in range(max_pages):
            page = await self._invoke(
                step_code=step_code,
                step_index=step_index,
                tool_name=tool_name,
                arguments=page_arguments,
                mode="pagination",
                item_index=page_index,
                item_count=None,
                enabled=enabled,
                unavailable_message=unavailable_message,
            )
            pages.append(page)
            self._append_result("MCP함수페이지호출완료", page)

            failed = (
                not page.succeeded
                or page.outcome != "SUCCESS"
                or page.backend == "disabled"
            )
            if failed:
                stop_reason = (
                    "disabled" if page.backend == "disabled" else "page_error"
                )
                if error_policy == "fail_fast" or page.backend == "disabled":
                    fail_fast_terminal = page
                    break

            raw_next = next_arguments(page, page_index + 1)
            if raw_next is None or not raw_next:
                stop_reason = "no_next_key"
                self._trace(
                    "MCP함수페이지판정완료",
                    {
                        "stepCode": step_code,
                        "toolName": tool_name,
                        "pageNumber": page_index + 1,
                        "hasNext": False,
                        "stopReason": stop_reason,
                    },
                )
                break
            cursor = self._validate_arguments(
                raw_next,
                label=f"{step_code}.next_arguments",
            )
            signature = repr(sorted((key, repr(value)) for key, value in cursor.items()))
            if signature in cursor_signatures:
                raise ScenarioMcpHandlerError(
                    "동일한 MCP next key가 반복되어 페이지 순환을 중단했습니다: "
                    f"step={step_code}, page={page_index + 1}"
                )
            cursor_signatures.add(signature)
            page_arguments = {**base_arguments, **cursor}
            self._trace(
                "MCP함수페이지판정완료",
                {
                    "stepCode": step_code,
                    "toolName": tool_name,
                    "pageNumber": page_index + 1,
                    "hasNext": True,
                    "nextArgumentKeys": sorted(cursor),
                },
            )

        if fail_fast_terminal is not None:
            return fail_fast_terminal

        aggregate = self._aggregate(
            step_code=step_code,
            step_index=step_index,
            tool_name=tool_name,
            mode="pagination",
            requested_count=len(pages),
            results=pages,
            metadata={
                "pageCount": len(pages),
                "maxPages": max_pages,
                "stopReason": stop_reason,
                "truncated": stop_reason == "max_pages",
                "errorPolicy": error_policy,
            },
        )
        self._append_result("MCP함수페이지결과집계완료", aggregate)
        return aggregate

    def complete(self, terminal: McpExecutionResult) -> ScenarioMcpHandlerOutcome:
        """step 수와 terminal 표시를 확정해 graph로 전달한다."""

        if not self._results:
            raise ScenarioMcpHandlerError("MCP handler가 도구를 한 번도 실행하지 않았습니다.")
        if terminal not in self._results:
            raise ScenarioMcpHandlerError(
                "MCP handler terminal 결과는 context가 생성한 결과여야 합니다."
            )
        step_count = self._step_count
        final_step_index = terminal.workflow_step_index
        finalized: list[McpExecutionResult] = []
        finalized_terminal: McpExecutionResult | None = None
        for result in self._results:
            updated = result.model_copy(
                update={
                    "workflow_step_count": step_count,
                    "workflow_is_final": (
                        result.request_id == terminal.request_id
                        and result.workflow_is_aggregate
                        == terminal.workflow_is_aggregate
                        and result.workflow_step_index == final_step_index
                    ),
                }
            )
            finalized.append(updated)
            if result is terminal:
                finalized_terminal = updated
        if finalized_terminal is None:
            raise ScenarioMcpHandlerError("terminal 결과 확정에 실패했습니다.")
        self._results = finalized
        self._trace(
            "MCP함수핸들러완료",
            {
                "handlerCode": self.handler_code,
                "stepCount": step_count,
                "resultCount": len(finalized),
                "terminalStepCode": finalized_terminal.workflow_step_code,
                "terminalOutcome": finalized_terminal.outcome,
            },
        )
        return ScenarioMcpHandlerOutcome(
            terminal=finalized_terminal,
            results=tuple(finalized),
        )

    def fail(self, exc: BaseException) -> ScenarioMcpHandlerOutcome:
        """handler 예외를 내부 오류 원장과 사용자용 안전 문구로 분리한다."""

        step_index = self._step_count
        self._step_count += 1
        failure = McpExecutionResult(
            backend="handler",
            tool_name=self.handler_code,
            request_id=(
                f"{self.thread_id}:{self.subagent.detail_scenario_code}:"
                f"HANDLER-ERROR-{step_index + 1:04d}"
            ),
            arguments={},
            succeeded=False,
            outcome="ERROR",
            user_message=MCP_SAFE_ERROR_MESSAGE,
            error=str(exc),
            workflow_step_code="HANDLER_ERROR",
            workflow_step_index=step_index,
            workflow_step_count=self._step_count,
            workflow_is_final=True,
            workflow_execution_mode="function",
            workflow_handler_code=self.handler_code,
        )
        self._results.append(failure)
        self._trace(
            "MCP함수핸들러오류",
            {
                "handlerCode": self.handler_code,
                "errorType": type(exc).__name__,
                "error": str(exc),
            },
        )
        return ScenarioMcpHandlerOutcome(
            terminal=failure,
            results=tuple(self._results),
        )

    def _next_step_index(self, step_code: str) -> int:
        normalized = str(step_code).strip()
        if not normalized:
            raise ScenarioMcpHandlerError("step_code는 비어 있을 수 없습니다.")
        index = self._step_count
        self._step_count += 1
        return index

    async def _invoke(
        self,
        *,
        step_code: str,
        step_index: int,
        tool_name: str,
        arguments: Mapping[str, Any],
        mode: FunctionExecutionMode,
        item_index: int | None = None,
        item_count: int | None = None,
        enabled: bool,
        unavailable_message: str,
    ) -> McpExecutionResult:
        normalized_tool = str(tool_name).strip()
        if not normalized_tool:
            raise ScenarioMcpHandlerError("tool_name은 비어 있을 수 없습니다.")
        normalized_arguments = self._validate_arguments(
            arguments,
            label=f"{step_code}.arguments",
        )
        execution = await self.executor.execute(
            subagent=self.subagent,
            employee_id=self.employee_id,
            session_id=self.session_id,
            thread_id=self.thread_id,
            tool_name=normalized_tool,
            arguments=normalized_arguments,
            request_context=dict(self.request_context),
            step_code=str(step_code).strip(),
            step_index=step_index,
            step_count=max(self._step_count, step_index + 1),
            execution_mode=mode,
            workflow_item_index=item_index,
            workflow_item_count=item_count,
            enabled=enabled,
            unavailable_message=unavailable_message,
        )
        if execution is None:
            raise ScenarioMcpHandlerError(
                f"MCP executor가 결과를 반환하지 않았습니다: {normalized_tool}"
            )
        return execution.model_copy(
            update={
                "workflow_step_code": str(step_code).strip(),
                "workflow_step_index": step_index,
                "workflow_step_count": self._step_count,
                "workflow_is_final": False,
                "workflow_execution_mode": mode,
                "workflow_item_index": item_index,
                "workflow_item_count": item_count,
                "workflow_is_aggregate": False,
                "workflow_handler_code": self.handler_code,
            }
        )

    def _aggregate(
        self,
        *,
        step_code: str,
        step_index: int,
        tool_name: str,
        mode: Literal["function_many", "pagination"],
        requested_count: int,
        results: Sequence[McpExecutionResult],
        metadata: Mapping[str, Any],
    ) -> McpExecutionResult:
        batches: list[dict[str, Any]] = []
        flattened_data: list[dict[str, Any]] = []
        success_count = 0
        no_data_count = 0
        error_count = 0
        disabled_count = 0
        for fallback_index, execution in enumerate(results):
            if execution.succeeded and execution.outcome == "SUCCESS" and (
                execution.backend != "disabled"
            ):
                success_count += 1
            elif execution.outcome == "NO_DATA":
                no_data_count += 1
            elif execution.backend == "disabled":
                disabled_count += 1
            else:
                error_count += 1
            batches.append(
                {
                    "index": (
                        execution.workflow_item_index
                        if execution.workflow_item_index is not None
                        else fallback_index
                    ),
                    "toolName": execution.tool_name,
                    "requestId": execution.request_id,
                    "arguments": execution.arguments,
                    "succeeded": execution.succeeded,
                    "outcome": execution.outcome,
                    "businessCode": execution.business_code,
                    "result": execution.result,
                    "error": execution.error,
                }
            )
            if not (
                execution.succeeded
                and execution.outcome == "SUCCESS"
                and isinstance(execution.result, Mapping)
            ):
                continue
            try:
                items = extract_data_items(execution.result)
            except ValueError:
                continue
            for item in items:
                flattened_data.append(
                    {
                        **dict(item),
                        "_function_call": {
                            "index": (
                                execution.workflow_item_index
                                if execution.workflow_item_index is not None
                                else fallback_index
                            ),
                            "requestId": execution.request_id,
                            "arguments": execution.arguments,
                        },
                    }
                )

        aggregate_result = {
            "data": flattened_data,
            "batches": batches,
            "execution": {
                "mode": mode,
                "stepCode": step_code,
                "requestedCount": requested_count,
                "callCount": len(results),
                "successCount": success_count,
                "noDataCount": no_data_count,
                "errorCount": error_count,
                "disabledCount": disabled_count,
                **dict(metadata),
            },
        }
        request_id = (
            f"{results[0].request_id}:AGGREGATE"
            if results
            else (
                f"{self.thread_id}:{self.subagent.detail_scenario_code}:"
                f"{step_code}:AGGREGATE"
            )
        )
        common: dict[str, Any] = {
            "backend": "pagination" if mode == "pagination" else "function_many",
            "tool_name": tool_name,
            "request_id": request_id,
            "arguments": {"requested_count": requested_count},
            "result": aggregate_result,
            "workflow_step_code": step_code,
            "workflow_step_index": step_index,
            "workflow_step_count": self._step_count,
            "workflow_is_final": False,
            "workflow_execution_mode": mode,
            "workflow_item_index": None,
            "workflow_item_count": requested_count,
            "workflow_is_aggregate": True,
            "workflow_handler_code": self.handler_code,
        }
        if success_count:
            return McpExecutionResult(
                **common,
                succeeded=True,
                outcome="SUCCESS",
                error=(
                    "일부 함수형 MCP 호출이 완료되지 않았습니다: "
                    f"success={success_count}, no_data={no_data_count}, "
                    f"error={error_count}, disabled={disabled_count}"
                    # 1001(NO_DATA)은 정상 업무 결과이므로 성공 데이터가 하나라도
                    # 있으면 오류로 승격하지 않는다. 실제 ERROR/disabled만 최종
                    # 안전 오류답변 정책에 전달한다.
                    if error_count or disabled_count
                    else None
                ),
            )
        # 여러 번 호출한 결과가 모두 업무코드 1001(NO_DATA)이면 집계 결과도
        # 명시적인 1001로 만든다. 최종 답변 계층은 원본 batch를 다시 해석하지
        # 않아도 동일한 무데이터 고정답변을 선택할 수 있다.
        if results and no_data_count == len(results):
            return McpExecutionResult(
                **common,
                succeeded=True,
                outcome="NO_DATA",
                business_code="1001",
                user_message=MCP_NO_DATA_MESSAGE,
            )
        if requested_count == 0:
            return McpExecutionResult(
                **common,
                succeeded=True,
                outcome="NO_DATA",
                user_message=MCP_NO_DATA_MESSAGE,
            )
        if disabled_count and not error_count:
            return McpExecutionResult(
                **{**common, "backend": "disabled"},
                succeeded=True,
                outcome="SUCCESS",
                user_message=next(
                    (
                        item.user_message
                        for item in results
                        if item.user_message
                    ),
                    "요청한 MCP 도구가 아직 연결되지 않았습니다.",
                ),
            )
        return McpExecutionResult(
            **common,
            succeeded=False,
            outcome="ERROR",
            user_message=MCP_SAFE_ERROR_MESSAGE,
            error=(
                "함수형 MCP 호출이 모두 실패했습니다: "
                f"error={error_count}, requested={requested_count}"
            ),
        )

    def _append_result(self, stage: str, result: McpExecutionResult) -> None:
        self._results.append(result)
        self._trace(
            stage,
            {
                "handlerCode": self.handler_code,
                "stepCode": result.workflow_step_code,
                "stepIndex": result.workflow_step_index,
                "toolName": result.tool_name,
                "executionMode": result.workflow_execution_mode,
                "itemIndex": result.workflow_item_index,
                "itemCount": result.workflow_item_count,
                "arguments": result.arguments,
                "outcome": result.outcome,
                "backend": result.backend,
                "requestId": result.request_id,
            },
        )

    def _trace(self, stage: str, payload: dict[str, Any]) -> None:
        if self._trace_callback is not None:
            self._trace_callback(stage, payload, tuple(self._results))

    @staticmethod
    def _validate_arguments(
        arguments: Mapping[str, Any],
        *,
        label: str,
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ScenarioMcpHandlerError(f"{label}는 object여야 합니다.")
        normalized = {str(key): value for key, value in arguments.items()}
        if any(not key.strip() for key in normalized):
            raise ScenarioMcpHandlerError(f"{label}에는 빈 key를 사용할 수 없습니다.")
        return normalized

    @classmethod
    def _validate_arguments_list(
        cls,
        arguments_list: Sequence[Mapping[str, Any]],
        *,
        max_items: int,
        label: str,
    ) -> list[dict[str, Any]]:
        if isinstance(arguments_list, (str, bytes)) or not isinstance(
            arguments_list,
            Sequence,
        ):
            raise ScenarioMcpHandlerError(f"{label}는 object 배열이어야 합니다.")
        if len(arguments_list) > max_items:
            raise ScenarioMcpHandlerError(
                f"{label} 항목 수가 max_items를 초과했습니다: "
                f"count={len(arguments_list)}, max_items={max_items}"
            )
        return [
            cls._validate_arguments(item, label=f"{label}[{index}]")
            for index, item in enumerate(arguments_list)
        ]
