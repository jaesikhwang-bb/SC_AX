"""사번 기준 직원정보 MCP 선조회와 user.name 보강 테스트."""

from __future__ import annotations

import unittest
from typing import Any

from app.config import Settings
from app.employee_context import enrich_user_context, extract_employee_name
from app.mcp.models import McpExecutionResult


class _EmployeeExecutor:
    def __init__(self, result: McpExecutionResult | None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> McpExecutionResult | None:
        self.calls.append(kwargs)
        return self.result


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "genos_url": "https://example.test",
        "genos_serving_id": 1,
        "genos_model": "model",
        "genos_bearer_token": None,
        "prompt_version": None,
        "history_backend": "memory",
        "redis_url": "redis://localhost:6379/0",
        "redis_history_key_prefix": "chat:history",
        "history_limit": 10,
        "redis_dedupe_ttl_seconds": 3600,
        "mcp_backend": "mock",
        "employee_lookup_enabled": True,
        "employee_lookup_tool_name": "employee_lookup_tool",
        "employee_lookup_employee_id_argument": "employeeId",
        "employee_lookup_name_fields": ("employee_name", "name"),
    }
    values.update(overrides)
    return Settings(**values)


class EmployeeNameExtractionTests(unittest.TestCase):
    def test_extracts_name_from_obj_id_rows(self) -> None:
        name = extract_employee_name(
            {
                "data": [
                    {"objId": "department", "objVal": "영업"},
                    {"objId": "employee_name", "objVal": "황재식"},
                ]
            },
            name_fields=("employee_name", "name"),
        )
        self.assertEqual(name, "황재식")

    def test_extracts_name_from_regular_row(self) -> None:
        name = extract_employee_name(
            {"data": [{"employee_name": "김민경"}]},
            name_fields=("employee_name",),
        )
        self.assertEqual(name, "김민경")


class EmployeeContextEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_enriches_user_name_and_uses_configured_argument(self) -> None:
        executor = _EmployeeExecutor(
            McpExecutionResult(
                backend="test",
                tool_name="employee_lookup_tool",
                request_id="lookup",
                arguments={},
                succeeded=True,
                result={
                    "data": [
                        {"objId": "employee_name", "objVal": "황재식"}
                    ]
                },
            )
        )

        user = await enrich_user_context(
            user={"id": "K3003980", "deptcode": "D1", "deptname": "영업"},
            employee_id="K3003980",
            session_id="session",
            thread_id="thread",
            request_context={"access_token": "secret", "endpoint": "acqsc"},
            executor=executor,  # type: ignore[arg-type]
            settings=_settings(),
        )

        self.assertEqual(user["name"], "황재식")
        self.assertEqual(
            executor.calls[0]["arguments"],
            {
                "bearerToken": "secret",
                "employeeId": "K3003980",
            },
        )

    async def test_missing_real_employee_id_skips_lookup(self) -> None:
        executor = _EmployeeExecutor(None)

        user = await enrich_user_context(
            user={"id": None},
            employee_id="anonymous:session",
            session_id="session",
            thread_id="thread",
            request_context={},
            executor=executor,  # type: ignore[arg-type]
            settings=_settings(),
        )

        self.assertEqual(user["name"], "")
        self.assertEqual(executor.calls, [])
