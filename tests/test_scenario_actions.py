import unittest
from typing import Any

from app.answers import _build_fixed_data_answer
from app.csv_trace import EmptyTraceRecorder
from app.domain import ClassificationType, IntentClassification
from app.graph import MasterIntentGraph, MasterResult
from app.hitl import build_hitl_request
from app.mcp.models import MCP_NO_DATA_MESSAGE, McpExecutionResult
from app.mcp.scenarios.performance_fee import (
    WITHHOLDING_TAX_FAX_NUMBER_ACTION,
)
from app.mcp.scenarios.registry import (
    SCENARIO_HANDLER_REGISTRY,
    ScenarioMcpHandlerSpec,
)
from app.recommended_questions import (
    RecommendedQuestionRegistry,
)
from app.scenario_actions import (
    ScenarioActionDefinition,
    ScenarioActionInput,
    ScenarioActionRequired,
    register_scenario_action,
)
from app.streaming import build_action_event
from app.subagents.models import SubagentResult
from app.subagents.prompt_loader import SubagentPromptLoader


def _detail(manifest: dict, detail_code: str) -> dict:
    return next(
        detail
        for scenario in manifest["scenarios"]
        for detail in scenario["details"]
        if detail["code"] == detail_code
    )


class ScenarioActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        loader = SubagentPromptLoader()
        cls.performance_bundle = loader.load_one(directory="performance-fee")
        cls.rp_bundle = loader.load_one(directory="rp")

    def _fax_subagent(
        self,
        fax_part_1: str = "",
        fax_part_2: str = "",
        fax_part_3: str = "",
    ) -> SubagentResult:
        return SubagentResult.model_validate(
            {
                "agent_code": "PERFORMANCE_FEE",
                "prompt_version": self.performance_bundle.version,
                "scenario_code": "FEE_DETAILS",
                "scenario_name": "수수료 내역 조회",
                "detail_scenario_code": "WITHHOLDING_TAX_FAX_SEND",
                "detail_scenario_name": "원천징수 내역 팩스 전송",
                "parameters": {
                    "reference_year": "",
                    "fax_number": "",
                    "fax_part_1": fax_part_1,
                    "fax_part_2": fax_part_2,
                    "fax_part_3": fax_part_3,
                },
            }
        )

    def test_active_actions_are_not_declared_in_yaml(self) -> None:
        fax_detail = _detail(
            self.performance_bundle.manifest,
            "WITHHOLDING_TAX_FAX_SEND",
        )
        apartment_detail = _detail(
            self.rp_bundle.manifest,
            "APARTMENT_RP_LIST",
        )
        self.assertNotIn("interaction", fax_detail)
        self.assertNotIn("interaction", apartment_detail)
        self.assertNotIn("fax_confirmation", fax_detail["parameters"])
        self.assertEqual(
            fax_detail["parameters"],
            ["reference_year", "fax_number"],
        )

    def test_fax_action_code_message_and_validation_come_from_python(self) -> None:
        with self.assertRaises(ScenarioActionRequired) as raised:
            WITHHOLDING_TAX_FAX_NUMBER_ACTION.require({"fax_number": "123"})

        required = raised.exception
        self.assertEqual(
            required.action_code,
            "INPUT_FAX",
        )
        self.assertIn("팩스 번호", required.message)
        self.assertEqual(
            set(required.errors),
            {"INPUT_FAX:1", "INPUT_FAX:2", "INPUT_FAX:3"},
        )
        self.assertTrue(required.definition.inputs[0].sensitive)
        self.assertEqual(
            WITHHOLDING_TAX_FAX_NUMBER_ACTION.require(
                {
                    "fax_part_1": "02",
                    "fax_part_2": "1234",
                    "fax_part_3": "5678",
                }
            ),
            {
                "fax_part_1": "02",
                "fax_part_2": "1234",
                "fax_part_3": "5678",
            },
        )

    def test_custom_action_code_is_sent_to_frontend(self) -> None:
        interrupt = build_hitl_request(
            hitl_type="MCP_PARAMETER_REQUIRED",
            action_code=WITHHOLDING_TAX_FAX_NUMBER_ACTION.action_code,
            message=WITHHOLDING_TAX_FAX_NUMBER_ACTION.message,
            fields=WITHHOLDING_TAX_FAX_NUMBER_ACTION.frontend_fields(),
            context={},
        )
        event = build_action_event("thread-1", interrupt)
        self.assertEqual(
            event[0]["code"],
            "INPUT_FAX",
        )
        self.assertEqual(
            [item["inputCode"] for item in event],
            ["INPUT_FAX", "INPUT_FAX", "INPUT_FAX"],
        )
        self.assertEqual(
            [item["order"] for item in event],
            [1, 2, 3],
        )
        self.assertNotIn("error", event[0])
        self.assertNotIn("expectedValue", event[0])
        self.assertNotIn("allowedValues", event[0])

    def test_graph_restores_python_action_and_updates_parameters(self) -> None:
        subagent = self._fax_subagent()
        action = WITHHOLDING_TAX_FAX_NUMBER_ACTION
        interrupt = build_hitl_request(
            hitl_type="MCP_PARAMETER_REQUIRED",
            action_code=action.action_code,
            message=action.message,
            fields=action.frontend_fields(),
            context={
                "agent_code": "PERFORMANCE_FEE",
                "scenario_code": "FEE_DETAILS",
                "detail_scenario_code": "WITHHOLDING_TAX_FAX_SEND",
                "match_index": 0,
                "scenario_action_code": action.action_code,
            },
        )
        update = MasterIntentGraph._validate_mcp_parameter_input(
            {
                "hitl_type": "MCP_PARAMETER_REQUIRED",
                "interrupt": interrupt,
                "human_input": {
                    "INPUT_FAX": [
                        {"order": 1, "input": "02"},
                        {"order": 2, "input": "1234"},
                        {"order": 3, "input": "5678"},
                    ]
                },
                "subagent": subagent.model_dump(mode="json"),
            }
        )
        restored = SubagentResult.model_validate(update["subagent"])
        self.assertEqual(update["status"], "PASS")
        self.assertEqual(
            [
                restored.matches[0].parameters["fax_part_1"],
                restored.matches[0].parameters["fax_part_2"],
                restored.matches[0].parameters["fax_part_3"],
            ],
            ["02", "1234", "5678"],
        )

    def test_switch_agent_yes_and_no_use_explicit_control_code(self) -> None:
        classification = IntentClassification(
            refined_query="태블릿 사용 방법을 알려줘",
            classification_type=ClassificationType.AGENT,
            agent_code="TABLET",
        )
        interrupt = build_hitl_request(
            hitl_type="AGENT_CODE_MISMATCH",
            action_code="SWITCH_AGENT",
            message="현재 다른 업무를 진행 중입니다. '태블릿' 업무로 전환하시겠습니까?",
            fields=[
                {
                    "name": "SWITCH_AGENT",
                    "label": "에이전트 전환 여부",
                    "type": "choice",
                    "required": True,
                    "allowed_values": ["yes", "no"],
                }
            ],
            context={"classified_agent_code": "TABLET"},
        )

        action = build_action_event("thread-switch", interrupt)
        self.assertEqual(len(action), 1)
        self.assertEqual(action[0]["code"], "SWITCH_AGENT")
        self.assertEqual(action[0]["switch_agent_code"], "TABLET")
        self.assertEqual(action[0]["inputCode"], "SWITCH_AGENT")
        self.assertEqual(action[0]["allowedValues"], ["yes", "no"])

        common = {
            "hitl_type": "AGENT_CODE_MISMATCH",
            "interrupt": interrupt,
            "classification": classification.model_dump(mode="json"),
        }
        accepted = MasterIntentGraph._validate_agent_code_mismatch(
            {**common, "human_input": {"SWITCH_AGENT": "yes"}}
        )
        self.assertTrue(accepted["approved"])
        self.assertFalse(accepted["agent_switch_cancelled"])

        cancelled = MasterIntentGraph._validate_agent_code_mismatch(
            {**common, "human_input": {"SWITCH_AGENT": "no"}}
        )
        self.assertFalse(cancelled["approved"])
        self.assertTrue(cancelled["agent_switch_cancelled"])
        self.assertEqual(
            cancelled["direct_answer"],
            "전환을 취소했습니다. 이어서 도와드리겠습니다.",
        )
        self.assertEqual(
            MasterIntentGraph._after_input_decision(cancelled),
            "clear_hitl_state",
        )

    def test_withholding_recommendation_is_a_plain_user_question(self) -> None:
        source = self._fax_subagent()
        source.detail_scenario_code = "WITHHOLDING_TAX"
        source.matches[0].detail_scenario_code = "WITHHOLDING_TAX"
        registry = RecommendedQuestionRegistry.from_bundles(
            {"PERFORMANCE_FEE": self.performance_bundle}
        )
        questions = registry.for_subagent(source)
        self.assertIn("원천징수 내역을 팩스로 보내줘", [item["question"] for item in questions])
        for item in questions:
            self.assertEqual(item["interactionType"], "prompt")
            self.assertNotIn("affirmativeFollowup", item)


class _ResumeExecutor:
    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.outcomes = list(outcomes or [])

    async def execute(self, **kwargs: Any) -> McpExecutionResult:
        arguments = dict(kwargs["arguments"])
        self.calls.append(
            {
                "tool_name": kwargs["tool_name"],
                "arguments": arguments,
            }
        )
        outcome = self.outcomes.pop(0) if self.outcomes else "SUCCESS"
        return McpExecutionResult(
            backend="fake",
            tool_name=self.calls[-1]["tool_name"],
            request_id=f"resume-{len(self.calls)}",
            arguments=arguments,
            succeeded=outcome != "ERROR",
            outcome=outcome,
            result=(
                {"data": [{"content": "조회 성공"}]}
                if outcome == "SUCCESS"
                else None
            ),
            user_message=(MCP_NO_DATA_MESSAGE if outcome == "NO_DATA" else None),
        )


class ResultDrivenActionResumeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _graph(executor: _ResumeExecutor) -> MasterIntentGraph:
        graph = MasterIntentGraph.__new__(MasterIntentGraph)
        graph._mcp_executor = executor
        graph._trace_recorder = EmptyTraceRecorder()
        return graph

    @staticmethod
    def _state(subagent: SubagentResult) -> dict[str, Any]:
        return {
            "subagent": subagent.model_dump(mode="json"),
            "employee_id": "K3003980",
            "session_id": "session-resume",
            "thread_id": "thread-resume",
            "request_context": {"access_token": "token"},
            "mcp_workflow_results": [],
            "mcp_results": [],
            "mcp_start_index": 0,
        }

    async def test_action_after_first_mcp_resumes_at_second_mcp(self) -> None:
        registry_key = ("PERFORMANCE_FEE", "WITHHOLDING_TAX_FAX_SEND")
        original = SCENARIO_HANDLER_REGISTRY[registry_key]

        async def first_action_second(context):
            first = await context.call(
                step_code="FIRST_LOOKUP",
                tool_name="first_tool",
                arguments={"employeeId": context.employee_id},
            )
            fax = WITHHOLDING_TAX_FAX_NUMBER_ACTION.require(
                context.subagent.parameters
            )
            return await context.call(
                step_code="SECOND_SEND",
                tool_name="second_tool",
                arguments={
                    "sourceRequestId": first.request_id,
                    "faxNumber": "".join(
                        (
                            fax["fax_part_1"],
                            fax["fax_part_2"],
                            fax["fax_part_3"],
                        )
                    ),
                },
            )

        SCENARIO_HANDLER_REGISTRY[registry_key] = ScenarioMcpHandlerSpec(
            code="test.mcp_action_mcp.v1",
            handler=first_action_second,
            output_handler=original.output_handler,
            output_handler_code=original.output_handler_code,
        )
        executor = _ResumeExecutor()
        graph = self._graph(executor)
        subagent = SubagentResult.model_validate(
            {
                "agent_code": "PERFORMANCE_FEE",
                "prompt_version": "1.5.0",
                "scenario_code": "FEE_DETAILS",
                "scenario_name": "수수료 내역 조회",
                "detail_scenario_code": "WITHHOLDING_TAX_FAX_SEND",
                "detail_scenario_name": "원천징수 내역 팩스 전송",
                "parameters": {"reference_year": "", "fax_number": ""},
            }
        )
        common = self._state(subagent)
        try:
            interrupted = await graph._call_mcp(common)
            self.assertEqual(
                [item["tool_name"] for item in executor.calls],
                ["first_tool"],
            )
            self.assertEqual(len(interrupted["mcp_workflow_results"]), 1)

            resumed = graph._validate_mcp_parameter_input(
                {
                    **common,
                    **interrupted,
                    "hitl_type": "MCP_PARAMETER_REQUIRED",
                    "human_input": {
                        "INPUT_FAX": [
                            {"order": 1, "input": "02"},
                            {"order": 2, "input": "1234"},
                            {"order": 3, "input": "5678"},
                        ]
                    },
                }
            )
            completed = await graph._call_mcp(
                {**common, **interrupted, **resumed}
            )
        finally:
            SCENARIO_HANDLER_REGISTRY[registry_key] = original

        self.assertEqual(
            [item["tool_name"] for item in executor.calls],
            ["first_tool", "second_tool"],
        )
        self.assertEqual(completed["status"], "PASS")
        self.assertEqual(len(completed["mcp_workflow_results"]), 2)

    async def test_rag_uses_refined_query_and_keywords_without_action(self) -> None:
        executor = _ResumeExecutor(["NO_DATA"])
        graph = self._graph(executor)
        subagent = SubagentResult.model_validate(
            {
                "agent_code": "RP",
                "prompt_version": "1.4.0",
                "scenario_code": "RP_DOCUMENTS",
                "scenario_name": "RP 업무 문서 조회",
                "detail_scenario_code": (
                    "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"
                ),
                "detail_scenario_name": "아파트 관리비 자동납부 서비스",
                "parameters": {
                    "rag_query": "RP 연결 제한 기준 문서를 찾아줘",
                    "keywords": ["RP", "연결 기준"],
                },
            }
        )
        common = self._state(subagent)
        common["classification"] = IntentClassification(
            refined_query="RP 연결 제한 기준을 알려줘",
            classification_type=ClassificationType.AGENT,
            agent_code="RP",
        ).model_dump(mode="json")
        completed = await graph._call_mcp(common)

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(
            executor.calls[0]["arguments"]["query"],
            "RP 연결 제한 기준 문서를 찾아줘",
        )
        self.assertEqual(
            executor.calls[0]["arguments"]["keywords"],
            ["RP", "연결 기준"],
        )
        self.assertEqual(completed["status"], "PASS")
        self.assertIsNone(completed["interrupt"])

    async def test_no_data_can_finish_without_action_and_use_fixed_answer(self) -> None:
        executor = _ResumeExecutor(["NO_DATA"])
        graph = self._graph(executor)
        subagent = SubagentResult.model_validate(
            {
                "agent_code": "PERFORMANCE_FEE",
                "prompt_version": "1.5.0",
                "scenario_code": "PERFORMANCE_SUMMARY",
                "scenario_name": "실적 종합 조회",
                "detail_scenario_code": "PERFORMANCE_SUMMARY_TOTAL",
                "detail_scenario_name": "실적 종합 조회",
                "parameters": {"closing_year_month": "202608"},
            }
        )

        completed = await graph._call_mcp(self._state(subagent))

        self.assertEqual(completed["status"], "PASS")
        self.assertIsNone(completed["interrupt"])
        execution = McpExecutionResult.model_validate(completed["mcp"])
        self.assertEqual(execution.outcome, "NO_DATA")
        self.assertEqual(execution.user_message, MCP_NO_DATA_MESSAGE)
        master_result = MasterResult(
            status="PASS",
            thread_id="thread-resume",
            classification=IntentClassification(
                refined_query="실적을 조회해줘",
                classification_type=ClassificationType.AGENT,
                agent_code="PERFORMANCE_FEE",
            ),
        )
        self.assertEqual(
            _build_fixed_data_answer(
                master_result,
                mcp_results=[execution],
            ),
            MCP_NO_DATA_MESSAGE,
        )

    async def test_invalid_action_is_repeated_then_value_selects_next_mcp(self) -> None:
        registry_key = ("PERFORMANCE_FEE", "WITHHOLDING_TAX_FAX_SEND")
        original = SCENARIO_HANDLER_REGISTRY[registry_key]
        route_action = register_scenario_action(
            ScenarioActionDefinition(
                agent_code=registry_key[0],
                detail_scenario_code=registry_key[1],
                action_code="TEST_RESULT_ROUTE_REQUIRED",
                message="후속 조회 유형을 선택해 주세요.",
                inputs=(
                    ScenarioActionInput(
                        parameter_name="result_route",
                        input_code="result_route",
                        label="후속 조회 유형",
                        allowed_values=("SUMMARY", "DETAIL"),
                        validation_message="SUMMARY 또는 DETAIL을 입력해 주세요.",
                    ),
                ),
            )
        )

        async def lookup_action_branch(context):
            first = await context.call(
                step_code="ROUTE_SOURCE_LOOKUP",
                tool_name="route_source_tool",
                arguments={"employeeId": context.employee_id},
            )
            route = route_action.require(context.subagent.parameters)[
                "result_route"
            ]
            return await context.call(
                step_code=f"ROUTE_{route}",
                tool_name=(
                    "summary_tool" if route == "SUMMARY" else "detail_tool"
                ),
                arguments={"sourceRequestId": first.request_id},
            )

        SCENARIO_HANDLER_REGISTRY[registry_key] = ScenarioMcpHandlerSpec(
            code="test.action_result_branch.v1",
            handler=lookup_action_branch,
            output_handler=original.output_handler,
            output_handler_code=original.output_handler_code,
        )
        executor = _ResumeExecutor()
        graph = self._graph(executor)
        subagent = self._fax_subagent_for_route()
        common = self._state(subagent)
        try:
            interrupted = await graph._call_mcp(common)
            invalid = graph._validate_mcp_parameter_input(
                {
                    **common,
                    **interrupted,
                    "hitl_type": "MCP_PARAMETER_REQUIRED",
                    "human_input": {"result_route": "UNKNOWN"},
                }
            )
            self.assertEqual(invalid["status"], "INPUT_REQUIRED")
            self.assertEqual(
                invalid["interrupt"]["action_code"],
                route_action.action_code,
            )
            self.assertIn("result_route", invalid["interrupt"]["errors"])
            self.assertEqual(len(executor.calls), 1)

            valid = graph._validate_mcp_parameter_input(
                {
                    **common,
                    **interrupted,
                    **invalid,
                    "hitl_type": "MCP_PARAMETER_REQUIRED",
                    "human_input": {"result_route": "DETAIL"},
                }
            )
            completed = await graph._call_mcp(
                {**common, **interrupted, **valid}
            )
        finally:
            SCENARIO_HANDLER_REGISTRY[registry_key] = original

        self.assertEqual(completed["status"], "PASS")
        self.assertEqual(
            [item["tool_name"] for item in executor.calls],
            ["route_source_tool", "detail_tool"],
        )

    @staticmethod
    def _fax_subagent_for_route() -> SubagentResult:
        return SubagentResult.model_validate(
            {
                "agent_code": "PERFORMANCE_FEE",
                "prompt_version": "1.5.0",
                "scenario_code": "FEE_DETAILS",
                "scenario_name": "수수료 내역 조회",
                "detail_scenario_code": "WITHHOLDING_TAX_FAX_SEND",
                "detail_scenario_name": "원천징수 내역 팩스 전송",
                "parameters": {
                    "reference_year": "",
                    "fax_number": "",
                    "result_route": "",
                },
            }
        )


if __name__ == "__main__":
    unittest.main()
