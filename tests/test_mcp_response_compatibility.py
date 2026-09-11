"""운영계 MCP structuredContent 응답 계약 테스트."""

import unittest
from types import SimpleNamespace

import httpx

from app.mcp.client import (
    GenosMcpToolExecutor,
    _extract_mcp_structured_content,
    _parse_mcp_response,
)
from app.subagents.models import SubagentResult


class McpResponseCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.items = [
            {
                "objId": "artNm",
                "objType": "String",
                "objNm": "유치자명",
                "objVal": "홍길동",
            },
            {
                "objId": "amount",
                "objType": "Number",
                "objNm": "금액",
                "objVal": 10000,
            },
        ]

    def test_extracts_production_structured_content(self) -> None:
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "result": {
                    "structuredContent": {"data": self.items},
                    "isError": False,
                },
            },
        )

        envelope = _parse_mcp_response(response)
        normalized = _extract_mcp_structured_content(envelope["result"])

        self.assertEqual(normalized, {"data": self.items})

    def test_rejects_development_content_text_fallback(self) -> None:
        result = {
            "content": {"text": '[{"objId":"artNm","objVal":"홍길동"}]'},
            "isError": False,
        }

        self.assertIsNone(_extract_mcp_structured_content(result))

    def test_requires_structured_content_to_be_an_object(self) -> None:
        self.assertIsNone(
            _extract_mcp_structured_content(
                {"structuredContent": [{"data": self.items}]}
            )
        )

    async def test_function_handler_arguments_are_sent_directly(self) -> None:
        settings = SimpleNamespace(
            mcp_backend="mock",
            mcp_bearer_token=None,
            mcp_timeout_seconds=1.0,
            mcp_max_retries=0,
            project_code="acqsc",
        )
        executor = GenosMcpToolExecutor(settings)  # type: ignore[arg-type]
        subagent = SubagentResult.model_validate(
            {
                "agent_code": "PERFORMANCE_FEE",
                "prompt_version": "test",
                "scenario_code": "TEST",
                "scenario_name": "테스트",
                "detail_scenario_code": "TEST_DETAIL",
                "detail_scenario_name": "테스트 상세",
                "parameters": {},
            }
        )

        result = await executor.execute(
            subagent=subagent,
            employee_id="S123456",
            session_id="session-1",
            thread_id="thread-1",
            tool_name="test_tool",
            arguments={"param1": "value1"},
            step_code="LOOKUP",
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.tool_name, "test_tool")
        self.assertEqual(result.arguments, {"param1": "value1"})
        self.assertEqual(result.workflow_step_code, "LOOKUP")


if __name__ == "__main__":
    unittest.main()
