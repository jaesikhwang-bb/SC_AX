"""Redis 모듈 없이 일반 String 명령으로 HITL 상태가 동작하는지 검증한다."""

import unittest
from uuid import uuid4

from redis.asyncio import from_url

from app.hitl_store import RedisHitlStateStore


class RedisHitlStateStoreIntegrationTest(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.redis_url = "redis://localhost:6379/0"
        self.key_prefix = f"test:hitl:{uuid4()}"
        self.client = from_url(self.redis_url, decode_responses=True)
        try:
            await self.client.ping()
        except Exception as exc:
            await self.client.aclose()
            self.skipTest(f"Local Redis is unavailable: {exc}")

        self.store = RedisHitlStateStore(
            redis_url=self.redis_url,
            key_prefix=self.key_prefix,
            ttl_seconds=60,
            project_code="acqsc",
        )

    async def asyncTearDown(self) -> None:
        # 다른 프로젝트나 실제 채팅 데이터는 건드리지 않고 테스트 키만 지운다.
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

    async def test_set_get_ttl_and_delete(self) -> None:
        thread_id = "thread-1"
        key = f"acqsc:{self.key_prefix}:{thread_id}"
        interrupt = {
            "type": "AGENT_CODE_MISMATCH",
            "message": "에이전트를 변경하시겠습니까?",
            "fields": [],
            "context": {
                "frontend_agent_code": "TABLET",
                "classified_agent_code": "RP",
            },
            "errors": {},
        }

        await self.store.save(
            thread_id=thread_id,
            hitl_type="AGENT_CODE_MISMATCH",
            graph_state={
                "thread_id": thread_id,
                "classification": {"agent_code": "RP"},
            },
            interrupt=interrupt,
        )
        entry = await self.store.get(thread_id)
        ttl = await self.client.ttl(key)

        self.assertIsNotNone(entry)
        self.assertEqual("acqsc", entry.project_code)
        self.assertEqual(thread_id, entry.thread_id)
        self.assertEqual("RP", entry.graph_state["classification"]["agent_code"])
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 60)

        await self.store.delete(thread_id)
        self.assertIsNone(await self.store.get(thread_id))
