"""Redis 없는 개발 모드의 멀티턴 대화이력 범위를 검증한다."""

import unittest

from app.history import InMemoryChatHistoryStore


class InMemoryChatHistoryStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_same_conversation_and_agent_returns_previous_turn(self):
        store = InMemoryChatHistoryStore(project_code="acqsc")

        await store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-1",
            agent_code="PERFORMANCE_FEE",
            role="user",
            content="2026년 6월 수수료를 조회해줘",
            message_id="thread-1:user",
        )
        history = await store.get_recent(
            "EMP001",
            "conversation-1",
            "PERFORMANCE_FEE",
            10,
        )

        self.assertEqual(
            [
                {
                    "role": "user",
                    "content": "2026년 6월 수수료를 조회해줘",
                }
            ],
            history,
        )

    async def test_history_isolated_by_conversation_employee_and_agent(self):
        store = InMemoryChatHistoryStore(project_code="acqsc")
        await store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-1",
            agent_code="PERFORMANCE_FEE",
            role="user",
            content="내 수수료",
            message_id="thread-1:user",
        )

        self.assertEqual(
            [],
            await store.get_recent(
                "EMP001",
                "conversation-2",
                "PERFORMANCE_FEE",
                10,
            ),
        )
        self.assertEqual(
            [],
            await store.get_recent(
                "EMP002",
                "conversation-1",
                "PERFORMANCE_FEE",
                10,
            ),
        )
        self.assertEqual(
            [],
            await store.get_recent(
                "EMP001",
                "conversation-1",
                "RP",
                10,
            ),
        )

    async def test_duplicate_message_id_is_stored_once(self):
        store = InMemoryChatHistoryStore(project_code="acqsc")
        arguments = {
            "employee_id": "EMP001",
            "conversation_id": "conversation-1",
            "agent_code": "RP",
            "role": "user",
            "content": "RP 문의",
            "message_id": "thread-1:user",
        }

        first = await store.append_message(**arguments)
        second = await store.append_message(**arguments)
        history = await store.get_recent(
            "EMP001",
            "conversation-1",
            "RP",
            10,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(1, len(history))

    async def test_list_and_delete_conversation_across_agents(self):
        """한 conversation의 에이전트별 건수를 집계하고 함께 삭제한다."""

        store = InMemoryChatHistoryStore(project_code="acqsc")
        for agent_code, message_id in (
            ("RP", "thread-rp:user"),
            ("TABLET", "thread-tablet:user"),
        ):
            await store.append_message(
                employee_id="EMP001",
                conversation_id="conversation-delete",
                agent_code=agent_code,
                role="user",
                content=f"{agent_code} 질문",
                message_id=message_id,
            )
        await store.append_message(
            employee_id="EMP002",
            conversation_id="conversation-delete",
            agent_code="RP",
            role="user",
            content="다른 사원 질문",
            message_id="other-thread:user",
        )

        conversations = await store.list_conversations()
        target = next(
            item
            for item in conversations
            if item["employee_id"] == "EMP001"
        )
        deleted = await store.delete_conversation(
            "EMP001",
            "conversation-delete",
        )
        remaining = await store.list_conversations()

        self.assertEqual(2, target["message_count"])
        self.assertEqual({"RP": 1, "TABLET": 1}, target["agent_counts"])
        self.assertEqual(2, deleted)
        self.assertEqual(1, len(remaining))
        self.assertEqual("EMP002", remaining[0]["employee_id"])
