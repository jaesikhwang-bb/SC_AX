"""WSL에서 Windows localhost로 노출된 Redis 연동을 검증한다."""

import unittest
from uuid import uuid4

from redis.asyncio import from_url

from app.history import RedisChatHistoryStore


class RedisChatHistoryStoreIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.redis_url = "redis://localhost:6379/0"
        self.key_prefix = f"test:master-agent:{uuid4()}"
        self.client = from_url(self.redis_url, decode_responses=True)
        try:
            await self.client.ping()
        except Exception as exc:
            await self.client.aclose()
            self.skipTest(f"Local Redis is unavailable: {exc}")
        self.store = RedisChatHistoryStore(
            self.redis_url,
            self.key_prefix,
            dedupe_ttl_seconds=60,
            project_code="acqsc",
        )

    async def asyncTearDown(self) -> None:
        # 다른 데이터는 건드리지 않고 이 테스트의 고유 접두사 키만 삭제한다.
        keys = [
            key
            async for key in self.client.scan_iter(
                match=f"acqsc:{self.key_prefix}:*"
            )
        ]
        if keys:
            await self.client.delete(*keys)
        await self.store.aclose()
        await self.client.aclose()

    async def test_filters_employee_agent_and_duplicates(self) -> None:
        inserted = await self.store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-1",
            agent_code="RP",
            role="user",
            content="RP 첫 질문",
            message_id="message-rp-1",
        )
        duplicate = await self.store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-1",
            agent_code="RP",
            role="user",
            content="중복되면 안 되는 질문",
            message_id="message-rp-1",
        )
        await self.store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-1",
            agent_code="TABLET",
            role="user",
            content="태블릿 질문",
            message_id="message-tablet-1",
        )
        await self.store.append_message(
            employee_id="EMP002",
            conversation_id="conversation-1",
            agent_code="RP",
            role="user",
            content="다른 사원의 RP 질문",
            # 다른 사원 범위에서는 같은 message_id도 별도 요청으로 인정한다.
            message_id="message-rp-1",
        )

        rp_history = await self.store.get_recent(
            "EMP001", "conversation-1", "RP", limit=10
        )
        tablet_history = await self.store.get_recent(
            "EMP001", "conversation-1", "TABLET", limit=10
        )
        other_employee_history = await self.store.get_recent(
            "EMP002", "conversation-1", "RP", limit=10
        )

        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(
            [{"role": "user", "content": "RP 첫 질문"}],
            rp_history,
        )
        self.assertEqual(
            [{"role": "user", "content": "태블릿 질문"}],
            tablet_history,
        )
        self.assertEqual(
            [{"role": "user", "content": "다른 사원의 RP 질문"}],
            other_employee_history,
        )

    async def test_list_and_delete_conversation(self) -> None:
        """Redis에서도 conversation 목록 집계와 범위 삭제가 동작해야 한다."""

        await self.store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-delete",
            agent_code="RP",
            role="user",
            content="삭제할 RP 질문",
            message_id="delete-rp-1",
        )
        await self.store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-delete",
            agent_code="TABLET",
            role="user",
            content="삭제할 태블릿 질문",
            message_id="delete-tablet-1",
        )

        conversations = await self.store.list_conversations()
        deleted = await self.store.delete_conversation(
            "EMP001",
            "conversation-delete",
        )
        remaining = await self.store.list_conversations()

        self.assertEqual(1, len(conversations))
        self.assertEqual(2, conversations[0]["message_count"])
        self.assertEqual(
            {"RP": 1, "TABLET": 1},
            conversations[0]["agent_counts"],
        )
        self.assertEqual(2, deleted)
        self.assertEqual([], remaining)
