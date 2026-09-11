"""요청 서비스 별칭이 검증 오류 없이 Redis 논리 범위를 분리하는지 검증한다."""

import unittest

from app.csv_trace import EmptyTraceRecorder
from app.domain import ClassificationType, IntentClassification
from app.graph import MasterIntentGraph
from app.history import InMemoryChatHistoryStore, RedisChatHistoryStore
from app.hitl_store import InMemoryHitlStateStore, RedisHitlStateStore
from app.models import StreamingChatRequest


class ServiceAliasRequestTests(unittest.TestCase):
    def test_arbitrary_service_alias_is_accepted(self) -> None:
        request = StreamingChatRequest.model_validate(
            {
                "message": "조회해줘",
                "session_id": "session-1",
                "thread_id": None,
                "endpoint": "customer-service-v2",
                "agent_code": None,
                "humanInput": [],
                "user": None,
            }
        )

        self.assertEqual(request.endpoint, "customer-service-v2")

    def test_development_request_model_accepts_empty_ids_and_extra_fields(self) -> None:
        request = StreamingChatRequest.model_validate(
            {
                "message": "조회해줘",
                "session_id": "",
                "thread_id": "   ",
                "endpoint": "service-a",
                "agent_code": "",
                "humanInput": [
                    {
                        "code": "fax_number",
                        "input": None,
                        "frontendOnly": True,
                    }
                ],
                "user": {
                    "id": "사번-개발값",
                    "clientOnly": "ignored",
                },
                "newFrontendField": "ignored",
            }
        )

        self.assertIsNone(request.session_id)
        self.assertIsNone(request.thread_id)
        self.assertIsNone(request.agent_code)
        self.assertEqual(request.user.id, "사번-개발값")
        self.assertIsNone(request.human_input[0].input)
        self.assertFalse(hasattr(request, "newFrontendField"))

    def test_only_message_is_required_during_development(self) -> None:
        request = StreamingChatRequest.model_validate({"message": "조회해줘"})

        self.assertIsNone(request.session_id)
        self.assertIsNone(request.thread_id)
        self.assertIsNone(request.endpoint)
        self.assertIsNone(request.human_input)
        self.assertIsNone(request.user)

    def test_ordered_duplicate_human_inputs_are_preserved(self) -> None:
        request = StreamingChatRequest.model_validate(
            {
                "message": "팩스 번호를 입력합니다.",
                "humanInput": [
                    {"code": "INPUT_FAX", "input": "5678", "order": 3},
                    {"code": "INPUT_FAX", "input": "02", "order": 1},
                    {"code": "INPUT_FAX", "input": "1234", "order": 2},
                ],
            }
        )

        self.assertEqual(
            request.to_hitl_value(),
            {
                "INPUT_FAX": [
                    {"order": 1, "input": "02"},
                    {"order": 2, "input": "1234"},
                    {"order": 3, "input": "5678"},
                ]
            },
        )

    def test_optional_identifiers_accept_missing_empty_and_null(self) -> None:
        for fields in (
            {},
            {"session_id": "", "thread_id": "", "endpoint": ""},
            {"session_id": None, "thread_id": None, "endpoint": None},
            {"session_id": "   ", "thread_id": "   ", "endpoint": "   "},
        ):
            with self.subTest(fields=fields):
                request = StreamingChatRequest.model_validate(
                    {"message": "조회해줘", **fields}
                )
                self.assertIsNone(request.session_id)
                self.assertIsNone(request.thread_id)
                self.assertIsNone(request.endpoint)

    def test_redis_keys_use_the_request_alias(self) -> None:
        history = RedisChatHistoryStore.__new__(RedisChatHistoryStore)
        history._project_code = "acqsc"
        history._key_prefix = "chat:history"
        hitl = RedisHitlStateStore.__new__(RedisHitlStateStore)
        hitl._project_code = "acqsc"
        hitl._key_prefix = "hitl:state"

        self.assertEqual(
            history._history_key(
                "K1",
                "S1",
                "PERFORMANCE_FEE",
                "customer-service-v2",
            ),
            "customer-service-v2:chat:history:K1:S1:PERFORMANCE_FEE",
        )
        self.assertEqual(
            hitl._key("thread-1", "customer-service-v2"),
            "customer-service-v2:hitl:state:thread-1",
        )


class ServiceAliasRedisScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rag_answer_history_uses_only_final_classified_agent(self) -> None:
        store = InMemoryChatHistoryStore("acqsc")
        for agent_code, content in (
            ("PERFORMANCE_FEE", "내 수수료를 알려줘"),
            ("RP", "RP 신청 기준을 알려줘"),
        ):
            await store.append_message(
                employee_id="K1",
                session_id="S1",
                agent_code=agent_code,
                role="user",
                content=content,
                message_id=f"old-{agent_code}",
            )
        graph = MasterIntentGraph.__new__(MasterIntentGraph)
        graph._history_store = store
        graph._history_limit = 10
        graph._trace_recorder = EmptyTraceRecorder()
        classification = IntentClassification(
            refined_query="RP 연결 제한 조건을 알려줘",
            classification_type=ClassificationType.AGENT,
            agent_code="RP",
        )

        update = await graph._persist_user_message(
            {
                "employee_id": "K1",
                "session_id": "S1",
                "message_id": "current-thread",
                "frontend_agent_code": "PERFORMANCE_FEE",
                "classification": classification.model_dump(mode="json"),
                "request_context": {},
            }
        )

        self.assertEqual(
            [item["content"] for item in update["chat_history"]],
            ["RP 신청 기준을 알려줘"],
        )

    async def test_history_is_stored_under_each_request_alias(self) -> None:
        store = InMemoryChatHistoryStore("acqsc")

        first = await store.append_message(
            employee_id="K1",
            session_id="S1",
            agent_code="PERFORMANCE_FEE",
            role="user",
            content="첫 서비스 질문",
            message_id="same-message-id",
            project_code="service-a",
        )
        second = await store.append_message(
            employee_id="K1",
            session_id="S1",
            agent_code="PERFORMANCE_FEE",
            role="user",
            content="두 번째 서비스 질문",
            message_id="same-message-id",
            project_code="service-b",
        )

        self.assertTrue(first)
        self.assertTrue(second)
        service_a = await store.get_recent(
            "K1",
            "S1",
            "PERFORMANCE_FEE",
            10,
            project_code="service-a",
        )
        service_b = await store.get_recent(
            "K1",
            "S1",
            "PERFORMANCE_FEE",
            10,
            project_code="service-b",
        )
        self.assertEqual([item["content"] for item in service_a], ["첫 서비스 질문"])
        self.assertEqual(
            [item["content"] for item in service_b],
            ["두 번째 서비스 질문"],
        )

    async def test_hitl_state_is_stored_under_each_request_alias(self) -> None:
        store = InMemoryHitlStateStore("acqsc")
        interrupt = {"type": "MCP_PARAMETER_REQUIRED"}

        await store.save(
            thread_id="thread-1",
            hitl_type="MCP_PARAMETER_REQUIRED",
            graph_state={"value": "A"},
            interrupt=interrupt,
            project_code="service-a",
        )
        await store.save(
            thread_id="thread-1",
            hitl_type="MCP_PARAMETER_REQUIRED",
            graph_state={"value": "B"},
            interrupt=interrupt,
            project_code="service-b",
        )

        service_a = await store.get("thread-1", project_code="service-a")
        service_b = await store.get("thread-1", project_code="service-b")
        assert service_a is not None
        assert service_b is not None
        self.assertEqual(service_a.project_code, "service-a")
        self.assertEqual(service_b.project_code, "service-b")
        self.assertEqual(service_a.graph_state["value"], "A")
        self.assertEqual(service_b.graph_state["value"], "B")

    async def test_hitl_overwrite_preserves_creation_and_delete_clears_state(self) -> None:
        store = InMemoryHitlStateStore("acqsc")
        interrupt = {"type": "MCP_PARAMETER_REQUIRED"}

        await store.save(
            thread_id="thread-lifecycle",
            hitl_type="MCP_PARAMETER_REQUIRED",
            graph_state={"attempt": 1},
            interrupt=interrupt,
            project_code="service-a",
        )
        first = await store.get(
            "thread-lifecycle",
            project_code="service-a",
        )
        assert first is not None

        await store.save(
            thread_id="thread-lifecycle",
            hitl_type="MCP_PARAMETER_REQUIRED",
            graph_state={"attempt": 2},
            interrupt=interrupt,
            project_code="service-a",
        )
        second = await store.get(
            "thread-lifecycle",
            project_code="service-a",
        )
        assert second is not None
        self.assertEqual(second.created_at, first.created_at)
        self.assertGreaterEqual(second.updated_at, first.updated_at)
        self.assertEqual(second.graph_state["attempt"], 2)

        await store.delete(
            "thread-lifecycle",
            project_code="service-a",
        )
        self.assertIsNone(
            await store.get(
                "thread-lifecycle",
                project_code="service-a",
            )
        )


if __name__ == "__main__":
    unittest.main()
