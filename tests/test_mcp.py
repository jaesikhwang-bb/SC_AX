"""manifest 기반 GenOS MCP 매핑, 추적 ID, dict 결과를 검증한다."""

import unittest
from dataclasses import replace
import json

import httpx

from app.config import Settings
from app.mcp.client import (
    ManifestMcpToolExecutor,
    _parse_mcp_response,
    build_mcp_request_id,
)
from app.subagents.models import SubagentResult
from app.subagents.prompt_loader import SubagentPromptLoader


def mcp_settings() -> Settings:
    return Settings(
        genos_url="https://genos.genon.ai",
        genos_serving_id=850,
        genos_model="qwen/qwen3.7-flash",
        genos_bearer_token="test-token",
        prompt_version=None,
        history_backend="memory",
        redis_url="redis://localhost:6379/0",
        redis_history_key_prefix="chat:history",
        history_limit=10,
        redis_dedupe_ttl_seconds=3600,
        project_code="acqsc",
        mcp_backend="mock",
        mcp_id=475,
    )


class McpToolExecutorTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundles = SubagentPromptLoader().load_all()

    def test_all_performance_fee_details_have_mcp_mapping(self) -> None:
        bundle = self.bundles["PERFORMANCE_FEE"]
        details = [
            detail
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        ]

        self.assertEqual(11, len(details))
        for detail in details:
            with self.subTest(detail=detail["code"]):
                self.assertEqual("test_tool", detail["mcp"]["tool_name"])
                self.assertEqual(
                    "data1",
                    detail["mcp"]["arguments"]["param1"]["value"],
                )
                self.assertEqual(
                    "data2",
                    detail["mcp"]["arguments"]["param2"]["value"],
                )

    def test_request_id_contains_traceable_scope(self) -> None:
        request_id = build_mcp_request_id(
            project_code="acqsc",
            employee_id="EMP001",
            conversation_id="conversation-001",
            thread_id="thread-001",
        )

        self.assertEqual(
            "acqsc:EMP001:conversation-001:thread-001",
            request_id,
        )

    async def test_mock_execution_returns_tool_and_dict_result(self) -> None:
        executor = ManifestMcpToolExecutor(mcp_settings(), self.bundles)
        subagent = SubagentResult(
            agent_code="PERFORMANCE_FEE",
            prompt_version="v1",
            scenario_code="FEE_DETAILS",
            scenario_name="수수료 내역 조회",
            detail_scenario_code="FEE_TAX_NET_PAYMENT",
            detail_scenario_name="세금 및 실지급액 조회",
            parameters={
                "closing_year_month": "202606",
                "reference_date": None,
                "reference_year": None,
            },
        )

        result = await executor.execute(
            subagent=subagent,
            employee_id="EMP001",
            conversation_id="conversation-001",
            thread_id="thread-001",
        )

        self.assertIsNotNone(result)
        self.assertEqual("test_tool", result.tool_name)
        self.assertEqual(
            "acqsc:EMP001:conversation-001:thread-001:FEE_TAX_NET_PAYMENT",
            result.request_id,
        )
        self.assertEqual(
            {"param1": "data1", "param2": "data2"},
            result.result,
        )
        await executor.aclose()

    def test_sse_response_is_parsed(self) -> None:
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                "event: message\n"
                'data: {"jsonrpc":"2.0","id":"trace-1",'
                '"result":{"structuredContent":'
                '{"param1":"data1","param2":"data2"}}}\n\n'
            ),
        )

        parsed = _parse_mcp_response(response)

        self.assertEqual(
            {"param1": "data1", "param2": "data2"},
            parsed["result"]["structuredContent"],
        )

    async def test_http_execution_returns_structured_content_dict(self) -> None:
        """guide.ipynb 형식의 SSE에서 structuredContent dict만 반환해야 한다."""

        settings = replace(
            mcp_settings(),
            mcp_backend="http",
            mcp_bearer_token="mcp-test-token",
        )
        executor = ManifestMcpToolExecutor(settings, self.bundles)
        assert executor._http_client is not None
        await executor._http_client.aclose()

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(
                "https://genos.genon.ai/api/gateway/mcp/475/mcp",
                str(request.url),
            )
            self.assertEqual("tools/call", payload["method"])
            self.assertEqual("test_tool", payload["params"]["name"])
            self.assertEqual(
                {"param1": "data1", "param2": "data2"},
                payload["params"]["arguments"],
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    'data: {"jsonrpc":"2.0","id":"trace-1",'
                    '"result":{"structuredContent":'
                    '{"param1":"data1","param2":"data2"}}}\n\n'
                ),
            )

        executor._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={
                "Authorization": "Bearer mcp-test-token",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        subagent = SubagentResult(
            agent_code="PERFORMANCE_FEE",
            prompt_version="v1",
            scenario_code="PERFORMANCE_SUMMARY",
            scenario_name="실적 종합조회",
            detail_scenario_code="PERFORMANCE_SUMMARY_TOTAL",
            detail_scenario_name="실적 종합 조회",
            parameters={
                "closing_year_month": "202607",
                "reference_date": None,
                "reference_year": None,
            },
        )

        result = await executor.execute(
            subagent=subagent,
            employee_id="EMP001",
            conversation_id="conversation-001",
            thread_id="thread-001",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.succeeded)
        self.assertEqual(
            {"param1": "data1", "param2": "data2"},
            result.result,
        )
        await executor.aclose()
