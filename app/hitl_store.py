"""일반 Redis 명령으로 HITL 대기 상태를 저장하고 복원하는 모듈."""

from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, model_validator
from redis.exceptions import RedisError

from app.config import Settings
from app.observability import log_failure_diagnostic, logger, timed


def _project_scope(value: str | None, fallback: str) -> str:
    """요청 서비스 별칭을 Redis namespace로 사용하고 빈 값만 기본값으로 보완한다."""

    normalized = str(value or "").strip()
    return normalized or fallback


class HitlStateEntry(BaseModel):
    """Redis String에 JSON으로 저장되는 한 건의 HITL 대기 상태."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    project_code: str
    thread_id: str
    hitl_type: str
    # 특정 Edge부터 새 그래프를 실행하는 데 필요한 최소 상태만 저장한다.
    graph_state: dict[str, Any]
    # 프론트 입력창 구성과 오류 표시에 필요한 공통 응답 계약이다.
    interrupt: dict[str, Any]
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def validate_routing_type(self) -> "HitlStateEntry":
        """Redis 라우팅 유형과 프론트 입력 요청 유형의 일치를 확인한다."""

        if self.interrupt.get("type") != self.hitl_type:
            raise ValueError(
                "HITL 상태의 hitl_type과 interrupt.type이 일치하지 않습니다."
            )
        return self


class HitlStateStoreUnavailableError(RuntimeError):
    """Redis 연결 문제로 HITL 상태를 안전하게 처리할 수 없을 때 발생한다."""


class HitlStateNotFoundError(LookupError):
    """thread_id에 해당하는 HITL 상태가 없거나 TTL로 만료됐을 때 발생한다."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(f"HITL 상태를 찾을 수 없습니다: {thread_id}")


class HitlStateStore(Protocol):
    """그래프가 저장소 구현과 무관하게 사용할 HITL 상태 인터페이스."""

    @property
    def enabled(self) -> bool:
        """HITL 상태 저장과 프론트 재입력 흐름을 사용할지 반환한다."""

        ...

    async def save(
        self,
        *,
        thread_id: str,
        hitl_type: str,
        graph_state: dict[str, Any],
        interrupt: dict[str, Any],
        project_code: str | None = None,
    ) -> None: ...

    async def get(
        self,
        thread_id: str,
        project_code: str | None = None,
    ) -> HitlStateEntry | None: ...

    async def delete(
        self,
        thread_id: str,
        project_code: str | None = None,
    ) -> None: ...

    async def aclose(self) -> None: ...


class InMemoryHitlStateStore:
    """외부 Redis 없이 그래프와 API 계약을 검증하기 위한 테스트 저장소."""

    def __init__(self, project_code: str = "acqsc") -> None:
        self._project_code = project_code.casefold()
        self._states: dict[tuple[str, str], HitlStateEntry] = {}

    @property
    def enabled(self) -> bool:
        """테스트에서 실제 HITL 흐름을 검증하므로 활성 상태를 반환한다."""

        return True

    async def save(
        self,
        *,
        thread_id: str,
        hitl_type: str,
        graph_state: dict[str, Any],
        interrupt: dict[str, Any],
        project_code: str | None = None,
    ) -> None:
        scope = _project_scope(project_code, self._project_code)
        key = (scope, thread_id)
        now = datetime.now(UTC).isoformat()
        previous = self._states.get(key)
        self._states[key] = HitlStateEntry(
            project_code=scope,
            thread_id=thread_id,
            hitl_type=hitl_type,
            graph_state=graph_state,
            interrupt=interrupt,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )

    async def get(
        self,
        thread_id: str,
        project_code: str | None = None,
    ) -> HitlStateEntry | None:
        scope = _project_scope(project_code, self._project_code)
        return self._states.get((scope, thread_id))

    async def delete(
        self,
        thread_id: str,
        project_code: str | None = None,
    ) -> None:
        scope = _project_scope(project_code, self._project_code)
        self._states.pop((scope, thread_id), None)

    async def aclose(self) -> None:
        return None


class RedisHitlStateStore:
    """Redis String의 SET/GET/DEL만 사용해 HITL 상태를 관리한다.

    RedisJSON과 RediSearch는 사용하지 않는다. 각 상태에는 TTL을 적용해 사용자가
    응답하지 않은 오래된 대기 상태가 Redis에 계속 쌓이지 않게 한다.

    HITL 상태는 재개에 필요한 내부 실행 데이터이므로 출력 로그에는 원문을 남기지
    않는다. ``guardrail_enabled=False``인 정확 조회키는 업무상 Redis 상태에 원문
    보관될 수 있으며, 이 값의 보호는 Redis ACL·TLS·TTL 같은 저장소 통제로 한다.
    """

    def __init__(
        self,
        redis_url: str,
        key_prefix: str,
        ttl_seconds: int,
        project_code: str = "acqsc",
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("REDIS_HITL_TTL_SECONDS는 1 이상이어야 합니다.")

        from redis.asyncio import from_url

        self._client = from_url(redis_url, decode_responses=True)
        self._key_prefix = key_prefix.strip(":")
        self._ttl_seconds = ttl_seconds
        self._project_code = project_code.casefold()
        logger.info(
            "======== Redis HITL 저장소 준비 | 프로젝트=%s | "
            "URL=환경변수사용 | "
            "키접두사=%s | TTL초=%d",
            self._project_code,
            self._key_prefix,
            self._ttl_seconds,
        )

    def _key(
        self,
        thread_id: str,
        project_code: str | None = None,
    ) -> str:
        """프로젝트별 HITL 상태 Redis 키를 만든다."""

        scope = _project_scope(project_code, self._project_code)
        return f"{scope}:{self._key_prefix}:{thread_id}"

    @property
    def enabled(self) -> bool:
        """일반 Redis HITL 저장과 재진입 기능이 활성화됐음을 반환한다."""

        return True

    @timed("Redis HITL 상태 저장")
    async def save(
        self,
        *,
        thread_id: str,
        hitl_type: str,
        graph_state: dict[str, Any],
        interrupt: dict[str, Any],
        project_code: str | None = None,
    ) -> None:
        """HITL 상태를 JSON 문자열로 저장하고 TTL을 갱신한다."""

        scope = _project_scope(project_code, self._project_code)
        key = self._key(thread_id, scope)
        now = datetime.now(UTC).isoformat()
        previous = await self.get(thread_id, project_code=scope)
        entry = HitlStateEntry(
            project_code=scope,
            thread_id=thread_id,
            hitl_type=hitl_type,
            graph_state=graph_state,
            interrupt=interrupt,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        payload = entry.model_dump_json()
        logger.info(
            "======== Redis HITL SET 준비 | 유형=%s | TTL초=%d | "
            "그래프상태필드수=%d | 사용자입력필드수=%d",
            hitl_type,
            self._ttl_seconds,
            len(graph_state),
            len(interrupt.get("input_fields", [])),
        )
        try:
            await self._client.set(
                key,
                payload,
                ex=self._ttl_seconds,
            )
        except (RedisError, OSError, TimeoutError) as exc:
            _log_redis_hitl_failure("SET", thread_id, key, exc)
            raise HitlStateStoreUnavailableError(
                "Redis에 HITL 상태를 저장할 수 없습니다."
            ) from exc

        logger.info(
            "======== Redis HITL 상태 저장 완료 | 유형=%s | TTL초=%d",
            hitl_type,
            self._ttl_seconds,
        )

    @timed("Redis HITL 상태 조회")
    async def get(
        self,
        thread_id: str,
        project_code: str | None = None,
    ) -> HitlStateEntry | None:
        """thread_id의 상태를 조회하며 존재하지 않으면 None을 반환한다."""

        scope = _project_scope(project_code, self._project_code)
        key = self._key(thread_id, scope)
        logger.info("======== Redis HITL GET 실행")
        try:
            value = await self._client.get(key)
        except (RedisError, OSError, TimeoutError) as exc:
            _log_redis_hitl_failure("GET", thread_id, key, exc)
            raise HitlStateStoreUnavailableError(
                "Redis에서 HITL 상태를 조회할 수 없습니다."
            ) from exc

        if value is None:
            logger.info("======== Redis HITL 상태 없음")
            return None

        try:
            entry = HitlStateEntry.model_validate_json(value)
        except Exception as exc:
            log_failure_diagnostic(
                stage="Redis HITL JSON 복원",
                code_location="app/hitl_store.py:RedisHitlStateStore.get",
                exc=exc,
                likely_cause="Redis HITL 값 손상 또는 현재 Pydantic 스키마와 불일치",
                corrective_action=(
                    "해당 Redis 키 값을 확인하고 이전 버전 상태라면 키를 삭제한 "
                    "뒤 새 질문으로 시작하세요."
                ),
                retry_count=0,
                context={"operation": "JSON_VALIDATE"},
            )
            raise HitlStateStoreUnavailableError(
                "Redis HITL 상태 데이터가 올바르지 않습니다."
            ) from exc

        # 키뿐 아니라 JSON 내부 프로젝트와 thread_id도 다시 확인한다.
        if entry.project_code != scope or entry.thread_id != thread_id:
            raise HitlStateStoreUnavailableError(
                "Redis HITL 상태의 식별 정보가 요청과 일치하지 않습니다."
            )

        logger.info(
            "======== Redis HITL 상태 조회 완료 | 유형=%s | "
            "그래프상태필드수=%d | 사용자입력필드수=%d",
            entry.hitl_type,
            len(entry.graph_state),
            len(entry.interrupt.get("input_fields", [])),
        )
        return entry

    @timed("Redis HITL 상태 삭제")
    async def delete(
        self,
        thread_id: str,
        project_code: str | None = None,
    ) -> None:
        """정상 처리된 HITL 상태를 Redis에서 삭제한다."""

        key = self._key(thread_id, project_code)
        try:
            deleted = await self._client.delete(key)
        except (RedisError, OSError, TimeoutError) as exc:
            _log_redis_hitl_failure("DEL", thread_id, key, exc)
            raise HitlStateStoreUnavailableError(
                "Redis HITL 상태를 삭제할 수 없습니다."
            ) from exc

        logger.info(
            "======== Redis HITL DEL 완료 | 삭제=%s",
            bool(deleted),
        )

    @timed("Redis HITL 연결 종료")
    async def aclose(self) -> None:
        """애플리케이션 종료 시 HITL Redis 연결을 닫는다."""

        try:
            await self._client.aclose()
            logger.info("======== Redis HITL 연결 종료 완료")
        except (RedisError, OSError, TimeoutError) as exc:
            logger.info(
                "======== Redis HITL 연결 종료 오류 | 종료 흐름 계속 | 오류유형=%s",
                type(exc).__name__,
            )


def _log_redis_hitl_failure(
    operation: str,
    thread_id: str,
    key: str,
    exc: BaseException,
) -> None:
    """HITL Redis 연결·명령 실패의 확인 위치를 공통 형식으로 기록한다."""

    log_failure_diagnostic(
        stage=f"Redis HITL {operation}",
        code_location="app/hitl_store.py:RedisHitlStateStore",
        exc=exc,
        likely_cause=(
            "Redis 서버 미기동, REDIS_URL 오류, 네트워크·인증 문제 또는 명령 시간 초과"
        ),
        corrective_action=(
            "redis-cli -u REDIS_URL PING으로 연결을 확인하고 .env의 REDIS_URL, "
            "Redis 포트·비밀번호·방화벽 및 해당 키 권한을 확인하세요."
        ),
        retry_count=0,
        context={"operation": operation},
    )


@timed("HITL 상태 저장소 생성")
def create_hitl_state_store(settings: Settings) -> HitlStateStore:
    """환경설정에 따라 Redis 또는 개발용 메모리 저장소를 생성한다."""

    if settings.hitl_state_backend == "memory":
        logger.info(
            "======== HITL 저장소 준비 | 종류=memory | Redis 없이 불일치·승인 흐름 실행"
        )
        return InMemoryHitlStateStore(settings.project_code)
    if settings.hitl_state_backend == "redis":
        return RedisHitlStateStore(
            redis_url=settings.redis_url,
            key_prefix=settings.redis_hitl_key_prefix,
            ttl_seconds=settings.redis_hitl_ttl_seconds,
            project_code=settings.project_code,
        )
    raise ValueError("HITL_STATE_BACKEND는 memory 또는 redis만 사용할 수 있습니다.")
