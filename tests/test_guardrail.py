"""Bastion Guardian 계약과 출력 스트리밍 보안 경계 테스트."""

import json
import unittest

import httpx

from app.api import _guard_hitl_input_values, _guard_markdown_tables
from app.guardrail import (
    BastionGuardianClient,
    GuardrailContext,
    GuardrailDecision,
    parse_guardrail_response,
    restore_output_person_names,
    split_period_sentences,
)
from app.hitl import register_hitl_input_guardrail_policy


class GuardrailResponseTests(unittest.TestCase):
    def test_pass_uses_original_content(self) -> None:
        decision = parse_guardrail_response(
            {"action": "PASS", "input_results": []},
            original="원문",
        )
        self.assertEqual(decision.action, "PASS")
        self.assertEqual(decision.processed_content, "원문")

    def test_mask_uses_processed_content(self) -> None:
        decision = parse_guardrail_response(
            {
                "action": "MASK",
                "input_results": [
                    {
                        "index": 0,
                        "type": "text",
                        "action": "MASK",
                        "processed_content": "[PHONE_NUMBER_1]",
                    }
                ],
            },
            original="010-1234-5678",
        )
        self.assertEqual(decision.action, "MASK")
        self.assertEqual(decision.processed_content, "[PHONE_NUMBER_1]")

    def test_block_never_returns_original_content(self) -> None:
        decision = parse_guardrail_response(
            {"action": "BLOCK", "input_results": [None]},
            original="차단 대상",
        )
        self.assertEqual(decision.action, "BLOCK")
        self.assertIsNone(decision.processed_content)

    def test_common_error_envelope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "BAD_REQUEST"):
            parse_guardrail_response(
                {
                    "ok": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": "잘못된 요청",
                        "details": "상세",
                    },
                },
                original="원문",
            )

    def test_period_split_keeps_complete_sentences_and_tail(self) -> None:
        sentences, tail = split_period_sentences("첫 문장. 둘째 문장... 아직")
        self.assertEqual(sentences, ["첫 문장.", " 둘째 문장..."])
        self.assertEqual(tail, " 아직")
        self.assertEqual("".join(sentences) + tail, "첫 문장. 둘째 문장... 아직")

    def test_only_person_name_masks_are_restored(self) -> None:
        payload = _person_name_mask_payload()

        restored, count = restore_output_person_names(
            "이름은 [PERSON_NAME_1], 담당자는 PERSON_NAME_2, "
            "전화는 [PHONE_NUMBER_1]입니다.",
            payload,
        )

        self.assertEqual(
            restored,
            "이름은 황재식, 담당자는 김민경, 전화는 [PHONE_NUMBER_1]입니다.",
        )
        self.assertEqual(count, 2)

    def test_conflicting_person_name_mapping_is_not_restored(self) -> None:
        payload = _person_name_mask_payload()
        duplicated = payload["input_results"][0]["results"][0]["detected_items"]
        duplicated.append(
            {
                "rule_type": "ner",
                "rule_id": 0,
                "rule_name": "개인 이름 (한국)",
                "mask_word": "PERSON_NAME_1",
                "matched_text": "다른이름",
            }
        )

        restored, count = restore_output_person_names("[PERSON_NAME_1]", payload)

        self.assertEqual(restored, "[PERSON_NAME_1]")
        self.assertEqual(count, 0)


class BastionGuardianClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_api_key_passes_without_http_request(self) -> None:
        called = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(500)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = BastionGuardianClient(
                base_url="https://example.test",
                endpoint_path="/v1/huard/api",
                api_key=None,
                timeout_seconds=1,
                fail_open=False,
                http_client=http_client,
            )
            decision = await client.process_text(
                "개발 입력",
                role="user",
                process_type="INPUT",
                context=_context(),
            )
        self.assertFalse(called)
        self.assertEqual(decision.action, "PASS")
        self.assertEqual(decision.processed_content, "개발 입력")

    async def test_api_request_contract_and_mask_response(self) -> None:
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["header"] = request.headers.get(
                "X-Starfort-Guard-Api-Key"
            )
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "action": "MASK",
                    "input_results": [
                        {"index": 0, "processed_content": "마스킹 결과"}
                    ],
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = BastionGuardianClient(
                base_url="https://example.test",
                endpoint_path="/v1/huard/api",
                api_key="secret-key",
                timeout_seconds=1,
                fail_open=False,
                http_client=http_client,
            )
            decision = await client.process_text(
                "원문",
                role="assistant",
                process_type="OUTPUT",
                context=_context(),
            )

        self.assertEqual(captured["header"], "secret-key")
        self.assertEqual(captured["payload"]["processType"], "OUTPUT")
        self.assertEqual(captured["payload"]["messages"][0]["role"], "assistant")
        self.assertEqual(
            captured["payload"]["messages"][0]["content"],
            [{"type": "text", "text": "원문"}],
        )
        self.assertEqual(decision.processed_content, "마스킹 결과")

    async def test_error_envelope_is_fail_closed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"ok": False, "error": {"code": "BAD_REQUEST"}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = BastionGuardianClient(
                base_url="https://example.test",
                endpoint_path="/v1/huard/api",
                api_key="secret-key",
                timeout_seconds=1,
                fail_open=False,
                http_client=http_client,
            )
            decision = await client.process_text(
                "노출하면 안 되는 원문",
                role="assistant",
                process_type="OUTPUT",
                context=_context(),
            )
        self.assertEqual(decision.action, "BLOCK")
        self.assertIsNone(decision.processed_content)

    async def test_output_restores_names_but_keeps_other_masks(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_person_name_mask_payload())

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = BastionGuardianClient(
                base_url="https://example.test",
                endpoint_path="/v1/huard/api",
                api_key="secret-key",
                timeout_seconds=1,
                fail_open=False,
                http_client=http_client,
            )
            decision = await client.process_text(
                "내 이름은 황재식이고 연락처는 010-1234-5678입니다.",
                role="assistant",
                process_type="OUTPUT",
                context=_context(),
            )

        self.assertEqual(decision.action, "MASK")
        self.assertEqual(
            decision.processed_content,
            "내 이름은 황재식 입니다. 내 이름은 김민경 입니다. "
            "연락처는 [PHONE_NUMBER_1]입니다.",
        )
        self.assertEqual(decision.restored_person_name_count, 2)

    async def test_input_does_not_restore_person_names(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_person_name_mask_payload())

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as http_client:
            client = BastionGuardianClient(
                base_url="https://example.test",
                endpoint_path="/v1/huard/api",
                api_key="secret-key",
                timeout_seconds=1,
                fail_open=False,
                http_client=http_client,
            )
            decision = await client.process_text(
                "내 이름은 황재식입니다.",
                role="user",
                process_type="INPUT",
                context=_context(),
            )

        self.assertIn("[PERSON_NAME_1]", decision.processed_content or "")
        self.assertEqual(decision.restored_person_name_count, 0)


class _MaskingGuardrail:
    enabled = True

    async def process_text(self, content, *, role, process_type, context):
        return GuardrailDecision("MASK", "| 항목 | 값 |\n| --- | --- |\n| 전화 | [PHONE] |", True)

    async def aclose(self) -> None:
        return None


class _PassingGuardrail:
    enabled = False

    async def process_text(self, content, *, role, process_type, context):
        return GuardrailDecision("PASS", content, False, "NO_API_KEY")

    async def aclose(self) -> None:
        return None


class MarkdownTableGuardrailTests(unittest.IsolatedAsyncioTestCase):
    async def test_markdown_table_uses_processed_content_and_drops_raw_data(self) -> None:
        renderables = [
            {
                "code": "TEST:TABLE",
                "type": "table",
                "format": "markdown",
                "content": "| 항목 | 값 |\n| --- | --- |\n| 전화 | 010-1234-5678 |",
                "data": {
                    "columns": ["항목", "값"],
                    "rows": [["전화", "010-1234-5678"]],
                },
                "metadata": {},
            },
            {"code": "TEST:CARD", "type": "card", "format": "json"},
        ]

        guarded, blocked = await _guard_markdown_tables(
            renderables,
            guardrail_client=_MaskingGuardrail(),
            context_factory=lambda surface: _context(surface),
        )

        self.assertFalse(blocked)
        self.assertIn("[PHONE]", guarded[0]["content"])
        self.assertIsNone(guarded[0]["data"])
        self.assertTrue(guarded[0]["metadata"]["guardrailProcessed"])
        self.assertEqual(guarded[1], renderables[1])

    async def test_pass_through_table_preserves_structured_data(self) -> None:
        renderables = [
            {
                "code": "TEST:TABLE",
                "type": "table",
                "format": "markdown",
                "content": "| 항목 | 값 |\n| --- | --- |\n| 이름 | 홍길동 |",
                "data": {"columns": ["항목", "값"], "rows": [["이름", "홍길동"]]},
                "metadata": {},
            }
        ]

        guarded, blocked = await _guard_markdown_tables(
            renderables,
            guardrail_client=_PassingGuardrail(),
            context_factory=lambda surface: _context(surface),
        )

        self.assertFalse(blocked)
        self.assertEqual(guarded[0]["data"], renderables[0]["data"])
        self.assertEqual(guarded[0]["metadata"]["guardrailAction"], "PASS")
        self.assertFalse(guarded[0]["metadata"]["guardrailProcessed"])


class HitlInputGuardrailPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_input_code_keeps_original_lookup_value(self) -> None:
        input_code = "test_exact_lookup_value"
        register_hitl_input_guardrail_policy(input_code, enabled=False)

        guarded = await _guard_hitl_input_values(
            {input_code: "010-1234-5678"},
            guardrail_client=_MaskingGuardrail(),
            context_factory=lambda surface: _context(surface),
        )

        self.assertEqual(guarded[input_code], "010-1234-5678")

    async def test_unregistered_input_code_is_guarded_by_default(self) -> None:
        guarded = await _guard_hitl_input_values(
            {"test_default_guarded_value": "010-1234-5678"},
            guardrail_client=_MaskingGuardrail(),
            context_factory=lambda surface: _context(surface),
        )

        self.assertIn("[PHONE]", guarded["test_default_guarded_value"])


def _context(surface: str = "test") -> GuardrailContext:
    return GuardrailContext(
        trace_id="trace-1",
        session_id="session-1",
        user_id="user-1",
        metadata={"surface": surface},
        tags=("test",),
    )


def _person_name_mask_payload() -> dict:
    """이름과 전화번호가 함께 MASK된 고객사 응답 예시."""

    return {
        "action": "MASK",
        "input_results": [
            {
                "index": 0,
                "type": "text",
                "action": "MASK",
                "processed_content": (
                    "내 이름은 [PERSON_NAME_1] 입니다. "
                    "내 이름은 [PERSON_NAME_2] 입니다. "
                    "연락처는 [PHONE_NUMBER_1]입니다."
                ),
                "results": [
                    {
                        "policy_name": "PII_Default",
                        "policy_type": "PII",
                        "action": "MASK",
                        "detected_items": [
                            {
                                "rule_type": "ner",
                                "rule_id": 0,
                                "rule_name": "개인 이름 (한국)",
                                "mask_word": "PERSON_NAME_1",
                                "matched_text": "황재식",
                            },
                            {
                                "rule_type": "ner",
                                "rule_id": 0,
                                "rule_name": "개인 이름 (한국)",
                                "mask_word": "PERSON_NAME_2",
                                "matched_text": "김민경",
                            },
                            {
                                "rule_type": "regex",
                                "rule_id": 1,
                                "rule_name": "전화번호",
                                "mask_word": "PHONE_NUMBER_1",
                                "matched_text": "010-1234-5678",
                            },
                        ],
                    }
                ],
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
