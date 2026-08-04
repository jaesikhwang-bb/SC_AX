"""Redis 서버 장애 시 빈 이력으로 계속 진행하는 동작을 검증한다."""

import unittest

from redis.exceptions import ConnectionError

from app.history import RedisChatHistoryStore


class FailingRedisClient:
    """모든 Redis 작업에서 연결 오류를 발생시키는 테스트용 클라이언트."""

    async def lrange(self, *args):
        raise ConnectionError("테스트용 Redis 연결 실패")

    async def scan_iter(self, *args, **kwargs):
        raise ConnectionError("테스트용 Redis 연결 실패")
        yield  # pragma: no cover - 비동기 제너레이터 형태를 위한 도달 불가 코드

    async def eval(self, *args):
        raise ConnectionError("테스트용 Redis 연결 실패")

    async def aclose(self):
        raise ConnectionError("테스트용 Redis 연결 실패")


class RedisFallbackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 실제 Redis 연결을 만들지 않고 장애 클라이언트를 직접 주입한다.
        self.store = object.__new__(RedisChatHistoryStore)
        self.store._client = FailingRedisClient()
        self.store._key_prefix = "test:master-agent"
        self.store._project_code = "acqsc"
        self.store._dedupe_ttl_seconds = 60

    async def test_history_key_contains_project_code(self) -> None:
        """대화 이력 키의 첫 구간에 프로젝트 코드가 들어가는지 검증한다."""

        self.assertEqual(
            "acqsc:test:master-agent:EMP001:conversation-1:RP",
            self.store._history_key(
                "EMP001",
                "conversation-1",
                "RP",
            ),
        )

    async def test_read_failure_returns_empty_history(self) -> None:
        history = await self.store.get_recent(
            "EMP001",
            "conversation-1",
            "RP",
            limit=10,
        )
        self.assertEqual([], history)

    async def test_recent_agent_read_failure_returns_empty_history(self) -> None:
        agent_code, history = await self.store.get_recent_for_conversation(
            "EMP001",
            "conversation-1",
            limit=10,
        )
        self.assertIsNone(agent_code)
        self.assertEqual([], history)

    async def test_write_failure_returns_false(self) -> None:
        inserted = await self.store.append_message(
            employee_id="EMP001",
            conversation_id="conversation-1",
            agent_code="RP",
            role="user",
            content="저장되지 않아도 응답은 계속 진행된다.",
            message_id="message-1",
        )
        self.assertFalse(inserted)

    async def test_close_failure_does_not_raise(self) -> None:
        await self.store.aclose()
