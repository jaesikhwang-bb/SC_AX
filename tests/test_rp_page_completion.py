"""페이지 Y/1001 반환 계약과 실제 graph 재개·최소 상태 저장을 검증한다."""

import json
import unittest

from app.csv_trace import EmptyTraceRecorder
from app.graph import MasterIntentGraph
from app.hitl_store import InMemoryHitlStateStore
from app.mcp.models import McpExecutionResult
from app.mcp.scenario_runtime import ScenarioMcpHandlerContext
from app.mcp.scenarios.registry import get_scenario_handler_spec, run_scenario_handler
from app.subagents.models import SubagentResult
from app.streaming import build_action_event


class PageExecutor:
    def __init__(self, no_data=False):
        self.calls = []
        self.no_data = no_data

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return McpExecutionResult(
            backend="fake", tool_name=kwargs["tool_name"],
            request_id=f"page-{len(self.calls)}", arguments=kwargs["arguments"],
            succeeded=True, outcome="NO_DATA" if self.no_data else "SUCCESS",
            business_code="1001" if self.no_data else "1000",
            result={"data": [
                {"objId": "column1", "objVal": "테스트 단지"},
                {"objId": "column2", "objVal": "서울시 테스트로"},
                {"objId": "nextEtxtYn", "objVal": "Y" if len(self.calls) < 3 else "N"},
                {"objId": "nextEtxtKeyCn", "objVal": "123"},
                {"objId": "no2NextKeyCn", "objVal": "456"},
                {"objId": "no1Grid", "objVal": [{"private_row": "저장금지"}]},
            ]},
        )


def apartment():
    return SubagentResult(
        agent_code="RP", prompt_version="test", scenario_code="APARTMENT",
        scenario_name="아파트", detail_scenario_code="APARTMENT_RP_LIST",
        detail_scenario_name="단지 조회", parameters={"address": "서울시 테스트로", "address_type": "2"},
    )


class PageCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_address_selection_then_message_action_never_calls_mcp(self):
        graph = object.__new__(MasterIntentGraph)
        graph._mcp_executor = PageExecutor(no_data=True)
        graph._trace_recorder = EmptyTraceRecorder()
        for missing in (None, "", "null", "None", "아파트", "아파트 검색해줘", "아파트 조회", "아파트 관리비"):
            for choice in ("1", "2"):
                subagent = apartment()
                subagent.matches[0].parameters = {"address": missing}
                subagent.synchronize_primary_match()
                state = {
                    "thread_id": "t", "employee_id": "S123456", "session_id": "s",
                    "subagent": subagent.model_dump(mode="json"),
                    "request_context": {},
                }
                state.update(await graph._call_mcp(state))
                self.assertEqual(state["status"], "INPUT_REQUIRED")
                self.assertEqual(state["interrupt"]["action_code"], "CF_INQRDVC")
                state.update(hitl_type="MCP_PARAMETER_REQUIRED", human_input={"CF_INQRDVC": choice})
                state.update(graph._validate_mcp_parameter_input(state))
                state.update(await graph._call_mcp(state))
                self.assertEqual(state["status"], "INPUT_REQUIRED")
                self.assertTrue(state["interrupt"]["context"]["await_message_input"])
                self.assertEqual(state["interrupt"]["action_code"], "INPUT_ADDRESS")
                action = build_action_event("t", state["interrupt"])[0]
                self.assertEqual(action["code"], "INPUT_ADDRESS")
                self.assertEqual(action["inputCode"], "INPUT_ADDRESS")
                self.assertEqual(action["thread_id"], "t")
                self.assertEqual(action["inputMode"], "message")
                self.assertNotIn("allowedValues", action)
                label = "구주소" if choice == "1" else "신주소"
                self.assertEqual(state["interrupt"]["message"], f"{label}를 입력해 주세요.")
                state["request_context"] = {"rp_apartment_pending": {"address_type": choice}}
                state.update(await graph._call_mcp(state))
                self.assertEqual(
                    state["interrupt"]["message"],
                    "주소 또는 아파트명을 구체적으로 입력해 주세요.",
                )
        self.assertEqual(graph._mcp_executor.calls, [])

    async def test_handler_completion_accepts_y_and_no_data(self):
        for no_data in (False, True):
            with self.subTest(no_data=no_data):
                context = ScenarioMcpHandlerContext(
                    handler_code="rp.apartment_rp_list.v1", executor=PageExecutor(no_data),
                    subagent=apartment(), employee_id="S123456", session_id="s",
                    thread_id="t", request_context={"access_token": "current-token"},
                )
                outcome = await run_scenario_handler(
                    spec=get_scenario_handler_spec("RP", "APARTMENT_RP_LIST"), context=context,
                )
                self.assertTrue(outcome.terminal.succeeded, outcome.terminal.error)
                if no_data:
                    self.assertEqual(outcome.terminal.outcome, "NO_DATA")
                    self.assertIsNone(outcome.terminal.post_answer_action)
                else:
                    self.assertEqual(outcome.terminal.post_answer_action["action_code"], "NEXT_PAGE")

    async def test_y_save_yes_next_call_and_no_cleanup(self):
        graph = object.__new__(MasterIntentGraph)
        graph._mcp_executor = PageExecutor()
        graph._trace_recorder = EmptyTraceRecorder()
        graph._hitl_store = InMemoryHitlStateStore()
        state = {
            "thread_id": "t", "message_id": "m", "employee_id": "S123456",
            "session_id": "s", "frontend_agent_code": "RP", "classification": None,
            "subagent": apartment().model_dump(mode="json"),
            "request_context": {"access_token": "current-token", "endpoint": "acqsc"},
        }
        state.update(await graph._call_mcp(state))
        self.assertEqual(state["status"], "PASS")
        self.assertIn("1페이지 조회 결과입니다.", state["mcp"]["formatted_result"]["answer_text"])
        self.assertEqual(state["post_answer_interrupt"]["action_code"], "NEXT_PAGE")
        await graph._save_post_answer_hitl_state(state)
        entry = await graph._hitl_store.get("t")
        stored_json = json.dumps(entry.model_dump())
        self.assertNotIn("no1Grid", stored_json)
        self.assertNotIn("current-token", stored_json)
        self.assertEqual(entry.interrupt["context"]["nextEtxtKeyCn"], "123")
        resumed = {
            **entry.graph_state, "interrupt": entry.interrupt,
            "hitl_type": entry.hitl_type, "entry_stage": "HITL_RESUME",
            "human_input": {"NEXT_PAGE": "yes"},
            "request_context": {"access_token": "fresh-token", "endpoint": "acqsc"},
        }
        resumed.update(graph._validate_mcp_parameter_input(resumed))
        resumed.update(await graph._call_mcp(resumed))
        self.assertEqual(len(graph._mcp_executor.calls), 2)
        args = graph._mcp_executor.calls[-1]["arguments"]
        self.assertEqual(args["nextEtxtKeyCn"], "123")
        self.assertEqual(args["no2NextKeyCn"], "456")
        self.assertEqual(args["bearerToken"], "fresh-token")
        self.assertNotIn("page_number", args)
        self.assertIn("2페이지 조회 결과입니다.", resumed["mcp"]["formatted_result"]["answer_text"])
        await graph._save_post_answer_hitl_state(resumed)
        second_entry = await graph._hitl_store.get("t")
        self.assertEqual(second_entry.interrupt["context"]["page_number"], 2)
        resumed = {
            **second_entry.graph_state, "interrupt": second_entry.interrupt,
            "hitl_type": second_entry.hitl_type, "entry_stage": "HITL_RESUME",
            "human_input": {"NEXT_PAGE": "yes"},
            "request_context": {"access_token": "fresh-token", "endpoint": "acqsc"},
        }
        resumed.update(graph._validate_mcp_parameter_input(resumed))
        resumed.update(await graph._call_mcp(resumed))
        self.assertIn("3페이지 조회 결과입니다.", resumed["mcp"]["formatted_result"]["answer_text"])
        self.assertIsNone(resumed["post_answer_interrupt"])
        self.assertEqual(graph._after_mcp_call(resumed), "clear_hitl_state")
        resumed["interrupt"] = entry.interrupt
        resumed["human_input"] = {"NEXT_PAGE": "no"}
        resumed.update(graph._validate_mcp_parameter_input(resumed))
        self.assertEqual(graph._after_mcp_parameter_input(resumed), "clear_hitl_state")
        await graph._clear_hitl_state(resumed)
        self.assertIsNone(await graph._hitl_store.get("t"))
        self.assertEqual(len(graph._mcp_executor.calls), 3)
