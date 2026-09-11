"""사원·대화·에이전트별 멀티턴 이력을 관리하는 저장소."""

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError

from app.config import Settings
from app.observability import logger, timed


def _project_scope(value: str | None, fallback: str) -> str:
    """요청 서비스 별칭을 Redis namespace로 사용하고 빈 값만 기본값으로 보완한다."""

    normalized = str(value or "").strip()
    return normalized or fallback


class ChatHistoryEntry(BaseModel):
    """Redis List에 JSON으로 저장되는 한 건의 대화 메시지."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    project_code: str
    employee_id: str
    role: Literal["user", "assistant"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    agent_code: str
    created_at: str


class ChatHistoryStore(Protocol):
    """마스터와 향후 서브에이전트가 공통으로 사용할 이력 저장 인터페이스."""

    async def get_recent(
        self,
        employee_id: str,
        session_id: str,
        agent_code: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> list[dict[str, Any]]: ...

    async def get_recent_for_session(
        self,
        employee_id: str,
        session_id: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]: ...

    async def append_message(
        self,
        *,
        employee_id: str,
        session_id: str,
        agent_code: str,
        role: Literal["user", "assistant"],
        content: str,
        message_id: str,
        metadata: dict[str, Any] | None = None,
        project_code: str | None = None,
    ) -> bool: ...

    async def aclose(self) -> None: ...


class EmptyChatHistoryStore:
    """외부 Redis를 사용하지 않는 단위 테스트용 빈 저장소."""

    @timed("테스트용 빈 이력 조회")
    async def get_recent(
        self,
        employee_id: str,
        session_id: str,
        agent_code: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

    @timed("테스트용 최근 에이전트 이력 조회")
    async def get_recent_for_session(
        self,
        employee_id: str,
        session_id: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        return None, []

    @timed("테스트용 빈 이력 저장")
    async def append_message(
        self,
        *,
        employee_id: str,
        session_id: str,
        agent_code: str,
        role: Literal["user", "assistant"],
        content: str,
        message_id: str,
        metadata: dict[str, Any] | None = None,
        project_code: str | None = None,
    ) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class InMemoryChatHistoryStore:
    """Redis가 없는 개발 PC에서 멀티턴을 검증하는 프로세스 메모리 저장소.

    Redis 구현과 동일하게 employee_id, session_id, agent_code 조합으로
    이력을 완전히 분리한다. 서버 재시작 또는 여러 워커/Pod 사이에서는 공유되지
    않으므로 목업·로컬 테스트 전용이며 운영에서는 redis 백엔드를 사용해야 한다.
    """

    def __init__(self, project_code: str = "acqsc") -> None:
        self._project_code = project_code.casefold()
        self._messages: dict[
            tuple[str, str, str, str],
            list[ChatHistoryEntry],
        ] = {}
        self._message_ids: set[tuple[str, str, str, str]] = set()
        # 동시에 같은 요청이 들어왔을 때 중복 검사와 추가를 하나의 임계 구역으로
        # 처리한다. Redis 백엔드에서는 같은 역할을 Lua 스크립트가 수행한다.
        self._lock = asyncio.Lock()
        logger.info(
            "======== 메모리 대화이력 저장소 준비 | 프로젝트=%s | "
            "용도=로컬 멀티턴 테스트",
            self._project_code,
        )

    def _scope_key(
        self,
        employee_id: str,
        session_id: str,
        agent_code: str,
        project_code: str | None = None,
    ) -> tuple[str, str, str, str]:
        """Redis 키와 같은 논리 범위를 메모리 딕셔너리 키로 만든다."""

        return (
            _project_scope(project_code, self._project_code),
            employee_id,
            session_id,
            agent_code.upper(),
        )

    @timed("메모리 최근 대화 조회")
    async def get_recent(
        self,
        employee_id: str,
        session_id: str,
        agent_code: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> list[dict[str, Any]]:
        key = self._scope_key(
            employee_id,
            session_id,
            agent_code,
            project_code,
        )
        async with self._lock:
            entries = list(self._messages.get(key, []))[-limit:]
        history = [
            _history_entry_view(entry, include_metadata=include_metadata)
            for entry in entries
        ]
        logger.info(
            "======== 메모리 이력 반환 | 에이전트=%s | 개수=%d",
            agent_code.upper(),
            len(history),
        )
        return history

    @timed("메모리 session 최근 에이전트 이력 조회")
    async def get_recent_for_session(
        self,
        employee_id: str,
        session_id: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """프론트 미선택 시 가장 최근 에이전트 한 범위의 이력만 반환한다.

        session 안의 모든 에이전트 메시지를 섞지 않는다. 마지막 메시지의
        생성 시각이 가장 최신인 agent_code를 먼저 고른 뒤 해당 범위만 읽는다.
        """

        scope = _project_scope(project_code, self._project_code)
        async with self._lock:
            candidates = [
                (key[3], entries)
                for key, entries in self._messages.items()
                if (
                    key[0] == scope
                    and key[1] == employee_id
                    and key[2] == session_id
                    and entries
                )
            ]
            if not candidates:
                logger.info(
                    "======== 메모리 최근 에이전트 이력 없음",
                )
                return None, []

            agent_code, entries = max(
                candidates,
                key=lambda item: item[1][-1].created_at,
            )
            selected_entries = list(entries)[-limit:]

        history = [
            _history_entry_view(entry, include_metadata=include_metadata)
            for entry in selected_entries
        ]
        logger.info(
            "======== 메모리 최근 에이전트 이력 반환 | 선택에이전트=%s | 개수=%d",
            agent_code,
            len(history),
        )
        return agent_code, history

    @timed("메모리 대화 메시지 저장")
    async def append_message(
        self,
        *,
        employee_id: str,
        session_id: str,
        agent_code: str,
        role: Literal["user", "assistant"],
        content: str,
        message_id: str,
        metadata: dict[str, Any] | None = None,
        project_code: str | None = None,
    ) -> bool:
        normalized_code = agent_code.upper()
        scope = _project_scope(project_code, self._project_code)
        scope_key = self._scope_key(
            employee_id,
            session_id,
            normalized_code,
            scope,
        )
        dedupe_key = (scope, employee_id, session_id, message_id)
        entry = ChatHistoryEntry(
            message_id=message_id,
            project_code=scope,
            employee_id=employee_id,
            role=role,
            content=content,
            metadata=dict(metadata or {}),
            agent_code=normalized_code,
            created_at=datetime.now(UTC).isoformat(),
        )
        async with self._lock:
            if dedupe_key in self._message_ids:
                inserted = False
            else:
                self._message_ids.add(dedupe_key)
                self._messages.setdefault(scope_key, []).append(entry)
                inserted = True
        logger.info(
            "======== 메모리 이력 저장 결과 | 에이전트=%s | 역할=%s | "
            "본문길이=%d | 메타데이터필드수=%d | 신규저장=%s",
            normalized_code,
            role,
            len(content),
            len(metadata or {}),
            inserted,
        )
        return inserted

    async def aclose(self) -> None:
        """외부 연결이 없으므로 종료할 자원이 없다."""

        return None


class RedisChatHistoryStore:
    """각 범위의 메시지를 별도 Redis List에 저장한다.

    키 형식:
        {project_code}:{prefix}:{employee_id}:{session_id}:{agent_code}

    키 자체를 사원, 대화, 에이전트로 분리하기 때문에 다른 사원이나 다른
    에이전트의 데이터를 읽은 뒤 애플리케이션에서 버리는 방식이 아니다.

    ``/chat`` 경로는 사용자 본문을 INPUT, 답변·테이블을 OUTPUT 가드레일로
    처리한 뒤 이 저장소를 호출한다. 저장소 로그에는 본문 대신 역할·길이·건수만
    남긴다.
    """

    # 메시지 저장, 대화 이력 TTL 갱신, 중복 방지 키 생성을 하나의 Lua
    # 스크립트로 실행한다.
    # Redis 내부에서 원자적으로 실행되므로 동시에 같은 요청이 들어와도 한 번만
    # List에 추가된다.
    _APPEND_SCRIPT = """
    if redis.call('EXISTS', KEYS[2]) == 1 then
        return 0
    end
    redis.call('RPUSH', KEYS[1], ARGV[1])
    redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    return 1
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str,
        dedupe_ttl_seconds: int,
        project_code: str = "acqsc",
        history_ttl_seconds: int = 3600,
    ) -> None:
        if history_ttl_seconds <= 0:
            raise ValueError("REDIS_HISTORY_TTL_SECONDS는 1 이상이어야 합니다.")

        # redis.asyncio를 사용해 FastAPI 이벤트 루프를 막지 않는다.
        from redis.asyncio import from_url

        self._client = from_url(redis_url, decode_responses=True)
        self._key_prefix = key_prefix.rstrip(":")
        self._dedupe_ttl_seconds = dedupe_ttl_seconds
        self._history_ttl_seconds = history_ttl_seconds
        self._project_code = project_code.casefold()
        logger.info(
            "======== Redis 저장소 준비 | 프로젝트=%s | URL=환경변수사용 | "
            "키접두사=%s | 대화이력_TTL초=%d | 중복방지_TTL초=%d",
            self._project_code,
            self._key_prefix,
            self._history_ttl_seconds,
            self._dedupe_ttl_seconds,
        )

    def _history_key(
        self,
        employee_id: str,
        session_id: str,
        agent_code: str,
        project_code: str | None = None,
    ) -> str:
        """대화 List에 사용할 범위별 Redis 키를 만든다."""

        scope = _project_scope(project_code, self._project_code)
        return (
            f"{scope}:{self._key_prefix}:"
            f"{employee_id}:{session_id}:"
            f"{agent_code.upper()}"
        )

    def _dedupe_key(
        self,
        employee_id: str,
        session_id: str,
        message_id: str,
        project_code: str | None = None,
    ) -> str:
        """동일 사원·대화 안에서만 적용되는 중복 방지 키를 만든다."""

        scope = _project_scope(project_code, self._project_code)
        return (
            f"{scope}:{self._key_prefix}:dedupe:{employee_id}:"
            f"{session_id}:{message_id}"
        )

    def _scope_from_history_key(
        self,
        key: str,
        project_code: str | None = None,
    ) -> tuple[str, str, str] | None:
        """프로젝트 이력 키에서 사원·session·에이전트를 안전하게 복원한다."""

        scope = _project_scope(project_code, self._project_code)
        base = f"{scope}:{self._key_prefix}:"
        if not key.startswith(base):
            return None
        suffix = key[len(base) :]
        if suffix.startswith("dedupe:"):
            return None
        parts = suffix.split(":")
        if len(parts) < 3:
            return None
        # employee_id와 agent_code에는 콜론을 허용하지 않는다. 가운데 구간을
        # 다시 결합하므로 session_id 자체에 콜론이 있어도 복원할 수 있다.
        return parts[0], ":".join(parts[1:-1]), parts[-1].upper()

    @timed("Redis 최근 대화 조회")
    async def get_recent(
        self,
        employee_id: str,
        session_id: str,
        agent_code: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> list[dict[str, Any]]:
        """같은 범위에 속한 최근 대화만 오래된 순서부터 반환한다."""

        scope = _project_scope(project_code, self._project_code)
        key = self._history_key(
            employee_id,
            session_id,
            agent_code,
            scope,
        )
        logger.info("======== Redis 대화이력 조회 | 최대개수=%d", limit)
        try:
            values = await self._client.lrange(key, -limit, -1)
        except (RedisError, OSError, TimeoutError) as exc:
            # Redis 장애가 의도분류 자체를 막지 않도록 빈 이력을 반환한다.
            # redis-py의 연결 풀은 다음 요청에서 다시 연결을 시도하므로 서버가
            # 복구되면 애플리케이션 재시작 없이 정상 조회로 돌아온다.
            logger.info(
                "======== Redis 조회 실패 | 대화이력 없이 계속 진행 | "
                "에이전트=%s | 오류유형=%s",
                agent_code.upper(),
                type(exc).__name__,
            )
            return []

        history: list[dict[str, Any]] = []
        for value in values:
            entry = ChatHistoryEntry.model_validate_json(value)

            # Redis에 데이터를 수동으로 잘못 넣은 경우까지 대비해 저장된 메타
            # 정보가 요청 범위와 같은지 한 번 더 확인한다.
            if (
                entry.project_code != scope
                or entry.employee_id != employee_id
                or entry.agent_code.upper() != agent_code.upper()
            ):
                logger.info(
                    "======== Redis 이력 제외 | message_id=%s | 범위불일치",
                    entry.message_id,
                )
                continue
            history.append(
                _history_entry_view(
                    entry,
                    include_metadata=include_metadata,
                )
            )

        logger.info(
            "======== Redis 이력 반환 | 에이전트=%s | 개수=%d",
            agent_code.upper(),
            len(history),
        )
        return history

    @timed("Redis session 최근 에이전트 이력 조회")
    async def get_recent_for_session(
        self,
        employee_id: str,
        session_id: str,
        limit: int,
        include_metadata: bool = False,
        project_code: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """프론트 미선택 시 최근에 사용한 에이전트의 이력만 조회한다.

        같은 session에 여러 agent_code 키가 있어도 각 키의 마지막 메시지
        시각을 비교하여 하나만 선택한다. 따라서 에이전트별 이력 격리는 유지된다.
        """

        scope = _project_scope(project_code, self._project_code)
        match = f"{scope}:{self._key_prefix}:{employee_id}:{session_id}:*"
        latest_agent_code: str | None = None
        latest_created_at = ""
        try:
            async for key in self._client.scan_iter(match=match):
                history_scope = self._scope_from_history_key(key, scope)
                if history_scope is None:
                    continue
                key_employee, key_session, key_agent = history_scope
                if key_employee != employee_id or key_session != session_id:
                    continue

                last_values = await self._client.lrange(key, -1, -1)
                if not last_values:
                    continue
                try:
                    entry = ChatHistoryEntry.model_validate_json(last_values[0])
                except ValueError:
                    logger.info(
                        "======== Redis 최근 에이전트 후보 제외 | "
                        "마지막 메시지 JSON 오류",
                    )
                    continue
                if (
                    entry.project_code != scope
                    or entry.employee_id != employee_id
                    or entry.agent_code.upper() != key_agent
                ):
                    continue
                if entry.created_at > latest_created_at:
                    latest_created_at = entry.created_at
                    latest_agent_code = key_agent
        except (RedisError, OSError, TimeoutError) as exc:
            logger.info(
                "======== Redis 최근 에이전트 조회 실패 | "
                "대화이력 없이 계속 진행 | 오류유형=%s",
                type(exc).__name__,
            )
            return None, []

        if latest_agent_code is None:
            logger.info("======== Redis 최근 에이전트 이력 없음")
            return None, []

        history = await self.get_recent(
            employee_id,
            session_id,
            latest_agent_code,
            limit,
            include_metadata=include_metadata,
            project_code=scope,
        )
        logger.info(
            "======== Redis 최근 에이전트 이력 선택 | 선택에이전트=%s | 개수=%d",
            latest_agent_code,
            len(history),
        )
        return latest_agent_code, history

    @timed("Redis 대화 메시지 저장")
    async def append_message(
        self,
        *,
        employee_id: str,
        session_id: str,
        agent_code: str,
        role: Literal["user", "assistant"],
        content: str,
        message_id: str,
        metadata: dict[str, Any] | None = None,
        project_code: str | None = None,
    ) -> bool:
        """메시지를 한 번만 저장하고 대화 이력의 슬라이딩 TTL을 갱신한다.

        TTL은 실제 신규 메시지가 RPUSH된 경우에만 갱신한다. 단순 조회나 동일한
        message_id의 중복 요청은 대화 이력의 수명을 연장하지 않는다.
        """

        normalized_code = agent_code.upper()
        scope = _project_scope(project_code, self._project_code)
        entry = ChatHistoryEntry(
            message_id=message_id,
            project_code=scope,
            employee_id=employee_id,
            role=role,
            content=content,
            metadata=dict(metadata or {}),
            agent_code=normalized_code,
            created_at=datetime.now(UTC).isoformat(),
        )
        history_key = self._history_key(
            employee_id,
            session_id,
            normalized_code,
            scope,
        )
        dedupe_key = self._dedupe_key(
            employee_id,
            session_id,
            message_id,
            scope,
        )
        logger.info(
            "======== Redis 대화 저장 상태 | 명령=EVAL/RPUSH | "
            "에이전트=%s | 역할=%s | 본문길이=%d | "
            "메타데이터필드수=%d | 대화이력_TTL초=%d | "
            "중복방지_TTL초=%d",
            normalized_code,
            role,
            len(content),
            len(metadata or {}),
            self._history_ttl_seconds,
            self._dedupe_ttl_seconds,
        )
        try:
            inserted = await self._client.eval(
                self._APPEND_SCRIPT,
                2,
                history_key,
                dedupe_key,
                entry.model_dump_json(),
                str(self._dedupe_ttl_seconds),
                str(self._history_ttl_seconds),
            )
        except (RedisError, OSError, TimeoutError) as exc:
            # 분류 결과는 이미 만들어졌으므로 Redis 저장 실패 때문에 사용자
            # 응답까지 실패시키지 않는다. False는 저장되지 않았음을 뜻한다.
            logger.info(
                "======== Redis 저장 실패 | 저장을 생략하고 계속 진행 | "
                "에이전트=%s | 오류유형=%s",
                normalized_code,
                type(exc).__name__,
            )
            return False

        logger.info(
            "======== Redis 저장 결과 | 에이전트=%s | 역할=%s | "
            "신규저장=%s | 대화이력_TTL초=%d",
            normalized_code,
            role,
            bool(inserted),
            self._history_ttl_seconds,
        )
        return bool(inserted)

    @timed("Redis 연결 종료")
    async def aclose(self) -> None:
        """애플리케이션 종료 시 Redis 연결을 닫는다."""

        try:
            await self._client.aclose()
            logger.info("======== Redis 연결 종료 완료")
        except (RedisError, OSError, TimeoutError) as exc:
            # 종료 과정의 Redis 오류는 이미 처리 중인 응답과 무관하므로 기록만
            # 남기고 애플리케이션 종료 흐름을 유지한다.
            logger.info(
                "======== Redis 연결 종료 오류 | 종료 흐름 계속 | 오류유형=%s",
                type(exc).__name__,
            )


def _history_entry_view(
    entry: ChatHistoryEntry,
    *,
    include_metadata: bool,
) -> dict[str, Any]:
    """LLM용 이력은 본문만, 화면 조회용 이력은 metadata까지 반환한다."""

    message: dict[str, Any] = {
        "role": entry.role,
        "content": entry.content,
    }
    if include_metadata:
        message.update(
            {
                "id": entry.message_id,
                "metadata": entry.metadata,
            }
        )
    return message


@timed("대화이력 저장소 생성")
def create_history_store(settings: Settings) -> ChatHistoryStore:
    """환경설정에 맞는 대화이력 저장소를 생성한다."""

    if settings.history_backend == "redis":
        return RedisChatHistoryStore(
            settings.redis_url,
            settings.redis_history_key_prefix,
            settings.redis_dedupe_ttl_seconds,
            project_code=settings.project_code,
            history_ttl_seconds=settings.redis_history_ttl_seconds,
        )
    if settings.history_backend == "memory":
        return InMemoryChatHistoryStore(settings.project_code)
    if settings.history_backend == "empty":
        return EmptyChatHistoryStore()
    raise ValueError(
        "CHAT_HISTORY_BACKEND는 redis, memory 또는 empty만 사용할 수 있습니다."
    )
