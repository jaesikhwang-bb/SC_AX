"""개발 목업용 함수 추적 이벤트의 보안 경계와 오류 정보를 검증한다."""

import unittest

from app.csv_trace import EmptyTraceRecorder
from app.observability import (
    developer_trace_context,
    error_code_for_exception,
    timed,
)


@timed("테스트 성공 단계")
async def _successful_step() -> str:
    return "ok"


@timed("테스트 실패 단계")
async def _failing_step() -> None:
    raise ValueError("잘못된 테스트 입력")


class DeveloperTraceTests(unittest.IsolatedAsyncioTestCase):
    def test_error_code_preserves_acronym_as_one_word(self) -> None:
        api_connection_error_type = type("APIConnectionError", (Exception,), {})
        self.assertEqual(
            error_code_for_exception(api_connection_error_type()),
            "API_CONNECTION_ERROR",
        )

    async def test_trace_context_records_function_source_and_phases(self) -> None:
        records: list[dict] = []

        with developer_trace_context(records.append):
            result = await _successful_step()

        self.assertEqual(result, "ok")
        self.assertEqual([item["phase"] for item in records], ["STARTED", "COMPLETED"])
        self.assertEqual(records[0]["stage"], "테스트 성공 단계")
        self.assertEqual(records[0]["source"]["function"], "_successful_step")
        self.assertTrue(records[0]["source"]["file"].endswith("test_developer_trace.py"))

    async def test_failed_trace_contains_stable_error_code_and_hint(self) -> None:
        records: list[dict] = []

        with developer_trace_context(records.append):
            with self.assertRaisesRegex(ValueError, "잘못된 테스트 입력"):
                await _failing_step()

        failed = records[-1]
        self.assertEqual(failed["phase"], "FAILED")
        self.assertEqual(failed["error"]["code"], "VALUE_ERROR")
        self.assertEqual(failed["error"]["type"], "ValueError")
        self.assertIn("test_developer_trace.py", failed["customizationHint"])

    async def test_trace_is_disabled_without_explicit_context(self) -> None:
        self.assertEqual(await _successful_step(), "ok")

    def test_state_trace_exposes_only_guarded_metadata(self) -> None:
        records: list[dict] = []
        state = {
            "message": "원본 질문",
            "history": [{"role": "user", "content": "이전 질문"}],
            "classification": {
                "classification_type": "AGENT",
                "agent_code": "RP",
                "refined_query": "보정된 질문",
            },
            "subagent": {
                "matches": [
                    {
                        "scenario_code": "RP01",
                        "scenario_name": "급여",
                        "detail_scenario_code": "RP0101",
                        "detail_scenario_name": "원천징수 조회",
                        "parameters": {"fax_number": "02-1234-5678"},
                    }
                ]
            },
            "mcp_results": [
                {
                    "tool_name": "send_fax",
                    "request_id": "mcp-1",
                    "arguments": {"fax_number": "02-1234-5678"},
                    "succeeded": True,
                    "result": {"status": "sent"},
                    "formatted_result": "팩스 전송 완료",
                }
            ],
            "mcp_workflow_results": [
                {
                    "workflow_step_code": "LOOKUP_MEMBER",
                    "workflow_step_index": 0,
                    "workflow_step_count": 2,
                    "workflow_is_final": False,
                    "tool_name": "lookup_member",
                    "request_id": "mcp-lookup-1",
                    "arguments": {"employee_id": "K3003980"},
                    "succeeded": True,
                    "outcome": "SUCCESS",
                    "result": {
                        "data": [
                            {
                                "objId": "memberId",
                                "objType": "String",
                                "objVal": "M-100",
                            }
                        ]
                    },
                },
                {
                    "workflow_step_code": "SEND_FAX",
                    "workflow_step_index": 1,
                    "workflow_step_count": 2,
                    "workflow_is_final": True,
                    "tool_name": "send_fax",
                    "request_id": "mcp-1",
                    "arguments": {"memberId": "M-100"},
                    "succeeded": True,
                    "outcome": "SUCCESS",
                    "result": {"status": "sent"},
                    "formatted_result": "팩스 전송 완료",
                },
            ],
            "request_context": {
                "access_token": "must-not-be-visible",
                "api_key": "also-secret",
            },
        }

        with developer_trace_context(records.append):
            EmptyTraceRecorder().record("MCP도구호출완료", state)

        self.assertEqual(len(records), 1)
        trace = records[0]
        self.assertEqual(trace["kind"], "state_transition")
        self.assertEqual(trace["source"]["file"], "app/mcp/client.py")
        focus = trace["details"]["focus"]
        self.assertEqual(focus["questionLength"], len("원본 질문"))
        self.assertEqual(focus["historyCount"], 1)
        self.assertEqual(focus["refinedQuestionLength"], len("보정된 질문"))
        self.assertEqual(
            focus["scenarioMatches"][0]["parameterKeys"],
            ["fax_number"],
        )
        self.assertEqual(
            focus["mcpRequests"][0]["argumentKeys"],
            ["employee_id"],
        )
        self.assertEqual(len(focus["mcpWorkflowResults"]), 2)
        self.assertEqual(
            focus["mcpWorkflowResults"][0]["stepCode"],
            "LOOKUP_MEMBER",
        )
        self.assertNotIn("state", trace["details"])
        serialized = str(trace)
        for secret in (
            "원본 질문",
            "이전 질문",
            "보정된 질문",
            "02-1234-5678",
            "K3003980",
            "M-100",
            "must-not-be-visible",
            "also-secret",
        ):
            self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
