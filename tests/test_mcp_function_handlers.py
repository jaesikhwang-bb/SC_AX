"""Python 함수형 MCP handler와 도구별 pagination 계약을 검증한다."""

import unittest
from typing import Any

from app.csv_trace import EmptyTraceRecorder
from app.graph import MasterIntentGraph
from app.mcp.models import MCP_SAFE_ERROR_MESSAGE, McpExecutionResult
from app.mcp.result_adapters import adapt_mcp_result
from app.mcp.scenario_runtime import ScenarioMcpHandlerContext
from app.mcp.scenarios.performance_fee import (
    _all_next_key_arguments,
    composite_conversion_score,
)
from app.mcp.scenarios.qualification import (
    QUALIFICATION_DOCUMENT_CHARACTER_THRESHOLD,
    QUALIFICATION_FIRST_SEARCH_OUTPUT_TYPE,
    QUALIFICATION_SECOND_SEARCH_NUM_RESULTS,
    QUALIFICATION_SECOND_SEARCH_OUTPUT_TYPE,
    qualification_document_search,
)
from app.mcp.scenarios.registry import (
    SCENARIO_HANDLER_REGISTRY,
    ScenarioMcpHandlerSpec,
    get_scenario_handler_spec,
    run_scenario_handler,
)
from app.mcp.scenarios.rp import (
    RP_DOCUMENT_NUMBER_BY_DETAIL_CODE,
    RP_FIRST_SEARCH_NUM_RESULTS,
    RP_FIRST_SEARCH_OUTPUT_TYPE,
    RP_SECOND_SEARCH_NUM_RESULTS,
    RP_SECOND_SEARCH_OUTPUT_TYPE,
    apartment_rp_list,
    rp_document_search,
)
from app.scenario_actions import ScenarioActionRequired
from app.subagents.fixed_responses import get_subagent_fixed_response
from app.subagents.models import SubagentResult
from app.subagents.prompt_loader import SubagentPromptLoader


class PagingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> McpExecutionResult:
        arguments = dict(kwargs["arguments"])
        self.calls.append(
            {
                "arguments": arguments,
                "request_context": dict(kwargs["request_context"]),
            }
        )
        page_number = len(self.calls)
        next_values = (
            {"nextKey1": "A", "nextKey2": "B", "nextKey3": "C", "nextKey4": "D"}
            if page_number == 1
            else {"nextKey1": "", "nextKey2": "", "nextKey3": "", "nextKey4": ""}
        )
        return McpExecutionResult(
            backend="fake",
            tool_name="paged_tool",
            request_id=f"page-{page_number}",
            arguments=arguments,
            succeeded=True,
            result={
                "data": [
                    {"objId": "row", "objVal": f"page-{page_number}"},
                    *(
                        {"objId": key, "objVal": value}
                        for key, value in next_values.items()
                    ),
                    {"objId": "gridct", "objVal": 50},
                ]
            },
        )


def _subagent() -> SubagentResult:
    return SubagentResult.model_validate(
        {
            "agent_code": "PERFORMANCE_FEE",
            "prompt_version": "1.5.0",
            "scenario_code": "COMPOSITE_CONVERSION",
            "scenario_name": "복합환산조회",
            "detail_scenario_code": "COMPOSITE_CONVERSION_EXCLUDED",
            "detail_scenario_name": "환산 미반영 내역 조회",
            "parameters": {"closing_year_month": "202608"},
        }
    )


class FunctionHandlerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _rag_subagent(
        *,
        agent_code: str,
        detail_scenario_code: str,
    ) -> SubagentResult:
        return SubagentResult.model_validate(
            {
                "agent_code": agent_code,
                "prompt_version": "test",
                "scenario_code": "DOCUMENT_SEARCH",
                "scenario_name": "문서 검색",
                "detail_scenario_code": detail_scenario_code,
                "detail_scenario_name": "문서 검색",
                "parameters": {
                    "rag_query": "업무 기준을 알려줘",
                    "keywords": ["업무", "기준"],
                    "document_number": "128174",
                },
            }
        )

    @staticmethod
    def _rag_context(
        *,
        executor: Any,
        subagent: SubagentResult,
    ) -> ScenarioMcpHandlerContext:
        return ScenarioMcpHandlerContext(
            handler_code="test.conditional_document_search.v1",
            executor=executor,
            subagent=subagent,
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={"access_token": "access-token"},
            refined_query="보정된 업무 기준을 알려줘",
        )

    @staticmethod
    def _apartment_context(
        *,
        executor: Any,
        address: str | None,
    ) -> ScenarioMcpHandlerContext:
        subagent = SubagentResult.model_validate(
            {
                "agent_code": "RP",
                "prompt_version": "test",
                "scenario_code": "APARTMENT_MANAGEMENT_FEE_RP_ELIGIBILITY",
                "scenario_name": "아파트 관리비 자동납부 연결 가능 단지 조회",
                "detail_scenario_code": "APARTMENT_RP_LIST",
                "detail_scenario_name": "아파트 관리비 자동납부 연결 가능 단지 목록 조회",
                "parameters": {"address": address, "address_type": "1"},
            }
        )
        return ScenarioMcpHandlerContext(
            handler_code="rp.apartment_rp_list.v1",
            executor=executor,
            subagent=subagent,
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={"access_token": "access-token"},
            refined_query="아파트 자동이체 조회해줘",
        )

    async def test_apartment_lookup_without_address_requests_message_input_without_mcp(self) -> None:
        class FailingExecutor:
            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                raise AssertionError("주소가 없을 때 MCP를 호출하면 안 됩니다.")

        context = self._apartment_context(
            executor=FailingExecutor(),
            address=None,
        )

        with self.assertRaises(ScenarioActionRequired) as raised:
            await apartment_rp_list(context)
        self.assertEqual(raised.exception.action_code, "INPUT_ADDRESS")
        self.assertTrue(raised.exception.context["await_message_input"])

    async def test_apartment_lookup_accepts_road_or_dong_address_without_apartment_name(self) -> None:
        class AddressExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                self.calls.append(dict(kwargs))
                return McpExecutionResult(
                    backend="fake",
                    tool_name=kwargs["tool_name"],
                    request_id="apartment-1",
                    arguments=dict(kwargs["arguments"]),
                    succeeded=True,
                    result={"data": [{"objId": "column1", "objVal": "테스트"}]},
                )

        for address in ("서울시 강남구 테헤란로", "수원시 영통동"):
            with self.subTest(address=address):
                executor = AddressExecutor()
                context = self._apartment_context(
                    executor=executor,
                    address=address,
                )

                terminal = await apartment_rp_list(context)

                self.assertEqual(len(executor.calls), 1)
                self.assertEqual(executor.calls[0]["arguments"]["emdNm"], address)
                self.assertEqual(executor.calls[0]["arguments"]["inqrDvC"], "1")
                self.assertEqual(terminal.user_message, None)

    async def test_rp_document_search_keeps_first_result_under_threshold(self) -> None:
        class DocumentExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                arguments = dict(kwargs["arguments"])
                self.calls.append(arguments)
                return McpExecutionResult(
                    backend="fake",
                    tool_name="databricks_hybrid_search",
                    request_id="rp-first",
                    arguments=arguments,
                    succeeded=True,
                    result={
                        "data": [
                            {
                                "document_id": "rp-1",
                                "content": "짧은 RP 문서",
                                "score": 0.99,
                            }
                        ]
                    },
                )

        executor = DocumentExecutor()
        context = self._rag_context(
            executor=executor,
            subagent=self._rag_subagent(
                agent_code="RP",
                detail_scenario_code=(
                    "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"
                ),
            ),
        )

        terminal = await rp_document_search(context)

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(
            executor.calls[0]["num_results"],
            RP_FIRST_SEARCH_NUM_RESULTS,
        )
        self.assertEqual(
            executor.calls[0]["output_type"],
            RP_FIRST_SEARCH_OUTPUT_TYPE,
        )
        self.assertEqual(
            executor.calls[0]["filter"],
            (
                '{"blsm_doc_id":["'
                + RP_DOCUMENT_NUMBER_BY_DETAIL_CODE[
                    "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"
                ]
                + '"]}'
            ),
        )
        self.assertNotIn("\\", executor.calls[0]["filter"])
        self.assertEqual(terminal.request_id, "rp-first")

    async def test_rp_second_document_search_uses_batch_second_pass(self) -> None:
        class DocumentExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                arguments = dict(kwargs["arguments"])
                self.calls.append(arguments)
                content = "RP 2차 검색 문서"
                return McpExecutionResult(
                    backend="fake",
                    tool_name="databricks_hybrid_search",
                    request_id=f"rp-{len(self.calls)}",
                    arguments=arguments,
                    succeeded=True,
                    result={
                        "data": [
                            {
                                "document_id": f"rp-{len(self.calls)}",
                                "content": content,
                                "score": 0.99,
                            }
                        ]
                    },
                )

        executor = DocumentExecutor()
        context = self._rag_context(
            executor=executor,
            subagent=self._rag_subagent(
                agent_code="RP",
                detail_scenario_code=(
                    "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"
                ),
            ),
        )
        context.rag_search_pass = "SECOND"

        terminal = await rp_document_search(context)

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(
            executor.calls[0]["num_results"],
            RP_SECOND_SEARCH_NUM_RESULTS,
        )
        self.assertEqual(
            executor.calls[0]["output_type"],
            RP_SECOND_SEARCH_OUTPUT_TYPE,
        )
        self.assertEqual(terminal.request_id, "rp-1")
        self.assertEqual(terminal.workflow_step_code, "RP_DOCUMENT_SEARCH_SECOND")

    async def test_qualification_second_search_uses_batch_second_pass(self) -> None:
        detail_code = "NEW_MEMBER_QUALIFICATION"

        class DocumentExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                arguments = dict(kwargs["arguments"])
                self.calls.append(arguments)
                content = "자격기준 2차 검색 문서"
                return McpExecutionResult(
                    backend="fake",
                    tool_name="databricks_hybrid_search",
                    request_id=f"qualification-{len(self.calls)}",
                    arguments=arguments,
                    succeeded=True,
                    result={
                        "data": [
                            {
                                "document_id": f"qualification-{len(self.calls)}",
                                "content": content,
                                "score": 0.99,
                            }
                        ]
                    },
                )

        executor = DocumentExecutor()
        context = self._rag_context(
            executor=executor,
            subagent=self._rag_subagent(
                agent_code="QUALIFICATION",
                detail_scenario_code=detail_code,
            ),
        )
        context.rag_search_pass = "SECOND"

        terminal = await qualification_document_search(context)

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(
            executor.calls[0]["num_results"],
            QUALIFICATION_SECOND_SEARCH_NUM_RESULTS,
        )
        self.assertEqual(
            executor.calls[0]["output_type"],
            QUALIFICATION_SECOND_SEARCH_OUTPUT_TYPE,
        )
        self.assertEqual(
            executor.calls[0]["filter"],
            '{"blsm_doc_id":["128174"]}',
        )
        self.assertNotEqual(
            QUALIFICATION_FIRST_SEARCH_OUTPUT_TYPE,
            QUALIFICATION_SECOND_SEARCH_OUTPUT_TYPE,
        )
        self.assertEqual(terminal.request_id, "qualification-1")
        self.assertEqual(
            terminal.workflow_step_code,
            "QUALIFICATION_DOCUMENT_SEARCH_SECOND",
        )

    async def test_qualification_batch_counts_all_details_before_second_search(
        self,
    ) -> None:
        class SecondSearchExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                arguments = dict(kwargs["arguments"])
                self.calls.append(arguments)
                call_number = len(self.calls)
                return McpExecutionResult(
                    backend="fake",
                    tool_name="databricks_hybrid_search",
                    request_id=f"qualification-second-{call_number}",
                    arguments=arguments,
                    succeeded=True,
                    result={
                        "data": [
                            {
                                "document_id": f"second-{call_number}",
                                "content": f"2차 문서 {call_number}",
                                "score": 0.99,
                            }
                        ]
                    },
                )

        detail_codes = [
            "FOREIGNER_QUALIFICATION",
            "MINOR_QUALIFICATION",
        ]
        matches = [
            {
                "scenario_code": "PERSONAL_MEMBER_QUALIFICATION",
                "scenario_name": "개인회원 자격기준",
                "detail_scenario_code": detail_code,
                "detail_scenario_name": detail_code,
                "parameters": {
                    "rag_query": f"{detail_code} 기준",
                    "keywords": [detail_code],
                    "member_category": (
                        "FOREIGNER"
                        if detail_code == "FOREIGNER_QUALIFICATION"
                        else "MINOR"
                    ),
                },
            }
            for detail_code in detail_codes
        ]
        subagent = SubagentResult.model_validate(
            {
                "agent_code": "QUALIFICATION",
                "prompt_version": "test",
                **matches[0],
                "matches": matches,
            }
        )
        content_length = (
            QUALIFICATION_DOCUMENT_CHARACTER_THRESHOLD // 2 + 1
        )
        results = [
            McpExecutionResult(
                backend="fake",
                tool_name="databricks_hybrid_search",
                request_id=f"qualification-first-{index}",
                arguments={"num_results": 5},
                succeeded=True,
                result={
                    "data": [
                        {
                            "document_id": f"first-{index}",
                            "content": "가" * content_length,
                            "score": 0.99,
                        }
                    ]
                },
                result_format="raw.rag",
                workflow_step_code="QUALIFICATION_DOCUMENT_SEARCH",
                workflow_execution_mode="function",
                workflow_handler_code=(
                    "qualification.foreigner_document_search.v1"
                    if index == 1
                    else "qualification.minor_document_search.v1"
                ),
            )
            for index in (1, 2)
        ]
        workflow_results = list(results)
        executor = SecondSearchExecutor()
        graph = object.__new__(MasterIntentGraph)
        graph._mcp_executor = executor
        graph._trace_recorder = EmptyTraceRecorder()

        await graph._apply_rag_batch_second_search(
            state={
                "employee_id": "K3003980",
                "session_id": "session-1",
                "thread_id": "thread-1",
                "request_context": {"access_token": "access-token"},
            },
            subagent=subagent,
            refined_query="외국인과 미성년자 자격기준",
            results=results,
            workflow_results=workflow_results,
        )

        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(
            [call["num_results"] for call in executor.calls],
            [30, 30],
        )
        self.assertEqual(
            [result.workflow_step_code for result in results],
            [
                "QUALIFICATION_DOCUMENT_SEARCH_SECOND",
                "QUALIFICATION_DOCUMENT_SEARCH_SECOND",
            ],
        )
        self.assertEqual(len(workflow_results), 4)

    async def test_call_many_all_no_data_becomes_business_code_1001(self) -> None:
        class NoDataExecutor:
            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                arguments = dict(kwargs["arguments"])
                return McpExecutionResult(
                    backend="fake",
                    tool_name="multi_tool",
                    request_id=f"no-data-{arguments['item']}",
                    arguments=arguments,
                    succeeded=True,
                    outcome="NO_DATA",
                    business_code="1001",
                    result={"data": []},
                )

        context = ScenarioMcpHandlerContext(
            handler_code="test.function_many_no_data.v1",
            executor=NoDataExecutor(),
            subagent=_subagent(),
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={"access_token": "access-token"},
        )

        terminal = await context.call_many(
            step_code="MULTI_LOOKUP",
            tool_name="multi_tool",
            arguments_list=[{"item": "A"}, {"item": "B"}],
            error_policy="continue",
        )

        self.assertEqual(terminal.outcome, "NO_DATA")
        self.assertEqual(terminal.business_code, "1001")
        self.assertEqual(terminal.result["execution"]["callCount"], 2)
        self.assertEqual(terminal.result["execution"]["noDataCount"], 2)
        self.assertEqual(terminal.result["data"], [])

    async def test_tool_specific_next_keys_drive_multiple_pages(self) -> None:
        executor = PagingExecutor()
        context = ScenarioMcpHandlerContext(
            handler_code="test.pagination.v1",
            executor=executor,
            subagent=_subagent(),
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={"access_token": "access-token"},
        )
        terminal = await context.paginate(
            step_code="PAGED_LOOKUP",
            tool_name="paged_tool",
            initial_arguments={"base": "fixed"},
            next_arguments=_all_next_key_arguments,
            max_pages=10,
        )
        outcome = context.complete(terminal)

        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(
            executor.calls[1]["arguments"],
            {
                "base": "fixed",
                "nextKey1": "A",
                "nextKey2": "B",
                "nextKey3": "C",
                "nextKey4": "D",
                "no1PgeSize": 50,
            },
        )
        self.assertEqual(
            executor.calls[1]["request_context"]["access_token"],
            "access-token",
        )
        self.assertEqual(len(outcome.results), 3)
        self.assertTrue(outcome.terminal.workflow_is_aggregate)
        execution = outcome.terminal.result["execution"]
        self.assertEqual(execution["pageCount"], 2)
        self.assertEqual(execution["stopReason"], "no_next_key")
        self.assertEqual(len(outcome.terminal.result["batches"]), 2)

    async def test_score_looks_up_codes_then_paginates_each_code(self) -> None:
        class ScoreExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []
                self.pages_by_code: dict[str, int] = {}

            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                arguments = dict(kwargs["arguments"])
                self.calls.append(arguments)
                code = str(arguments.get("code", ""))
                if not code:
                    data = [
                        {"code": "A", "code_name": "A 유형"},
                        {"code": "B", "code_name": "B 유형"},
                    ]
                else:
                    page = self.pages_by_code.get(code, 0) + 1
                    self.pages_by_code[code] = page
                    data = [
                        {
                            "objId": "no1Grid",
                            "objVal": [[
                                {"objId": "value", "objVal": f"{code}-{page}"}
                            ]],
                        },
                        {
                            "objId": "nextKey1",
                            "objVal": f"NEXT-{code}" if page == 1 else "",
                        },
                        {"objId": "gridct", "objVal": 50},
                    ]
                return McpExecutionResult(
                    backend="fake",
                    tool_name="test_tool",
                    request_id=f"score-{len(self.calls)}",
                    arguments=arguments,
                    succeeded=True,
                    result={"data": data},
                )

        subagent = SubagentResult.model_validate(
            {
                "agent_code": "PERFORMANCE_FEE",
                "prompt_version": "1.5.0",
                "scenario_code": "COMPOSITE_CONVERSION",
                "scenario_name": "복합환산조회",
                "detail_scenario_code": "COMPOSITE_CONVERSION_SCORE",
                "detail_scenario_name": "복합환산 점수",
                "parameters": {
                    "closing_year_month": "202608",
                    "reference_date": "",
                },
            }
        )
        executor = ScoreExecutor()
        context = ScenarioMcpHandlerContext(
            handler_code="performance_fee.composite_conversion_score.v1",
            executor=executor,
            subagent=subagent,
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={},
        )

        terminal = await composite_conversion_score(context)
        outcome = context.complete(terminal)
        formatted = adapt_mcp_result(
            execution=outcome.terminal,
            subagent=subagent,
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={},
            workflow_results=outcome.results,
        ).formatted_result

        self.assertEqual(len(executor.calls), 5)
        self.assertEqual(
            [call.get("code") for call in executor.calls],
            [None, "A", "A", "B", "B"],
        )
        self.assertEqual(executor.calls[2]["nextKey1"], "NEXT-A")
        self.assertEqual(executor.calls[2]["no1PgeSize"], 50)
        self.assertEqual(
            [(group["code"], group["pageCount"]) for group in formatted["data"]["groups"]],
            [("A", 2), ("B", 2)],
        )
        self.assertEqual(
            [row["value"] for group in formatted["data"]["groups"] for row in group["gridRows"]],
            ["A-1", "A-2", "B-1", "B-2"],
        )

    async def test_score_all_code_details_no_data_returns_final_1001(self) -> None:
        class NoDataScoreExecutor:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def execute(self, **kwargs: Any) -> McpExecutionResult:
                arguments = dict(kwargs["arguments"])
                self.calls.append(arguments)
                code = str(arguments.get("code", ""))
                if not code:
                    return McpExecutionResult(
                        backend="fake",
                        tool_name="test_tool",
                        request_id="parameter-lookup",
                        arguments=arguments,
                        succeeded=True,
                        result={
                            "data": [
                                {"code": "A", "code_name": "A 유형"},
                                {"code": "B", "code_name": "B 유형"},
                            ]
                        },
                    )
                return McpExecutionResult(
                    backend="fake",
                    tool_name="test_tool",
                    request_id=f"detail-{code}",
                    arguments=arguments,
                    succeeded=True,
                    outcome="NO_DATA",
                    business_code="1001",
                    user_message="조회 결과가 없습니다.",
                    result={"data": []},
                )

        subagent = SubagentResult.model_validate(
            {
                "agent_code": "PERFORMANCE_FEE",
                "prompt_version": "1.5.0",
                "scenario_code": "COMPOSITE_CONVERSION",
                "scenario_name": "복합환산조회",
                "detail_scenario_code": "COMPOSITE_CONVERSION_SCORE",
                "detail_scenario_name": "복합환산 점수",
                "parameters": {
                    "closing_year_month": "202608",
                    "reference_date": "",
                },
            }
        )
        executor = NoDataScoreExecutor()
        context = ScenarioMcpHandlerContext(
            handler_code="performance_fee.composite_conversion_score.v1",
            executor=executor,
            subagent=subagent,
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={},
        )

        terminal = await composite_conversion_score(context)
        outcome = context.complete(terminal)

        self.assertEqual(
            [call.get("code") for call in executor.calls],
            [None, "A", "B"],
        )
        self.assertEqual(outcome.terminal.outcome, "NO_DATA")
        self.assertEqual(outcome.terminal.business_code, "1001")
        self.assertEqual(
            outcome.terminal.result["execution"]["callCount"],
            2,
        )
        self.assertEqual(
            outcome.terminal.result["execution"]["noDataCount"],
            2,
        )

    async def test_handler_exception_becomes_safe_terminal_result(self) -> None:
        async def fail_handler(_context: ScenarioMcpHandlerContext):
            raise ValueError("내부 업무 변환 오류")

        context = ScenarioMcpHandlerContext(
            handler_code="test.failure.v1",
            executor=PagingExecutor(),
            subagent=_subagent(),
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={},
        )
        outcome = await run_scenario_handler(
            spec=ScenarioMcpHandlerSpec(
                code="test.failure.v1",
                handler=fail_handler,
            ),
            context=context,
        )

        self.assertFalse(outcome.terminal.succeeded)
        self.assertEqual(outcome.terminal.backend, "handler")
        self.assertEqual(outcome.terminal.user_message, MCP_SAFE_ERROR_MESSAGE)
        self.assertNotIn("내부 업무 변환 오류", outcome.terminal.user_message)


class FunctionHandlerRegistryTests(unittest.TestCase):
    def test_every_non_fixed_manifest_detail_has_python_handler(self) -> None:
        bundles = SubagentPromptLoader().load_all()
        missing: list[tuple[str, str]] = []
        for bundle in bundles.values():
            for scenario in bundle.manifest["scenarios"]:
                for detail in scenario["details"]:
                    detail_code = str(detail["code"])
                    if get_subagent_fixed_response(bundle.agent_code, detail_code):
                        continue
                    if get_scenario_handler_spec(bundle.agent_code, detail_code) is None:
                        missing.append((bundle.agent_code, detail_code))
        self.assertEqual(missing, [])

    def test_handlers_have_fixed_or_rag_output_handlers(self) -> None:
        missing = [
            key
            for key, spec in SCENARIO_HANDLER_REGISTRY.items()
            if spec.output_handler is None and spec.rag_output_handler is None
        ]
        self.assertEqual(missing, [])

    def test_output_handler_keeps_duplicate_columns_and_raw_grid_values(self) -> None:
        subagent = _subagent()
        execution = McpExecutionResult(
            backend="fake",
            tool_name="test_tool",
            request_id="aggregate-1",
            arguments={},
            succeeded=True,
            result={
                "data": [
                    {
                        "objId": "no1Grid",
                        "objVal": [
                            [
                                {"objId": "customerId", "objVal": "C-001"},
                                {"objId": "amount", "objVal": "100"},
                            ]
                        ],
                        "_function_call": {"index": 0},
                    },
                    {
                        "objId": "no1Grid",
                        "objVal": [
                            [
                                {"objId": "customerId", "objVal": "C-002"},
                                {"objId": "amount", "objVal": "200"},
                            ]
                        ],
                        "_function_call": {"index": 1},
                    },
                    {"objId": "summary", "objVal": "공통값", "_function_call": {"index": 0}},
                    {"objId": "summary", "objVal": "공통값", "_function_call": {"index": 1}},
                ],
                "batches": [],
                "execution": {"mode": "pagination", "pageCount": 2},
            },
        )

        formatted = adapt_mcp_result(
            execution=execution,
            subagent=subagent,
            employee_id="K3003980",
            session_id="session-1",
            thread_id="thread-1",
            request_context={},
            workflow_results=(execution,),
        ).formatted_result

        self.assertEqual(
            formatted["output_handler_code"],
            "performance_fee.composite_conversion_excluded_output.v1",
        )
        self.assertEqual(
            [
                row["customerId"]
                for row in formatted["data"]["gridRows"]
            ],
            ["C-001", "C-002"],
        )
        self.assertEqual(
            formatted["renderables"][0]["data"]["rows"],
            [[1, "100", "C-001"], [2, "200", "C-002"]],
        )


if __name__ == "__main__":
    unittest.main()
