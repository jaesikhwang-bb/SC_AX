"""환경변수에서 애플리케이션 설정을 읽는 모듈."""

import os
import re
from dataclasses import dataclass

from app.observability import logger, timed


@dataclass(frozen=True)
class Settings:
    """실행 중 변경되지 않는 애플리케이션 설정.

    보안 정보에는 소스 코드 기본값을 두지 않는다. GenOS 토큰은 반드시 프로세스
    환경변수 또는 배포 환경의 보안 저장소에서 주입해야 한다.
    """

    genos_url: str
    genos_serving_id: int
    genos_model: str
    genos_bearer_token: str | None
    prompt_version: str | None
    history_backend: str
    redis_url: str
    redis_history_key_prefix: str
    history_limit: int
    redis_dedupe_ttl_seconds: int
    project_code: str = "acqsc"
    # 개발 PC에서는 Redis 없이도 HITL 불일치 응답과 후속 승인을 테스트할 수
    # 있도록 memory를 사용한다. 운영에서는 redis로 변경한다.
    hitl_state_backend: str = "memory"
    # HITL 상태는 LangGraph Checkpointer가 아니라 일반 Redis String에
    # JSON으로 저장한다. 따라서 RedisJSON과 RediSearch 모듈이 필요 없다.
    redis_hitl_key_prefix: str = "hitl:state"
    redis_hitl_ttl_seconds: int = 3600
    # guide.ipynb와 동일하게 GenOS Gateway MCP를 HTTP로 호출한다.
    # mock은 외부 네트워크를 사용하지 않는 단위 테스트에서만 명시적으로 사용한다.
    mcp_backend: str = "http"
    mcp_id: int = 475
    mcp_bearer_token: str | None = None
    mcp_timeout_seconds: float = 30.0
    # 로컬 의도분류 품질 확인용 CSV이다. 운영 개인정보 정책에 맞춰 끌 수 있다.
    csv_trace_enabled: bool = False
    csv_trace_dir: str = "data/intent_traces"

    @classmethod
    @timed("환경설정 불러오기")
    def from_env(cls) -> "Settings":
        """환경변수와 비밀이 아닌 기본값으로 설정 객체를 만든다."""

        project_code = os.getenv("PROJECT_CODE", "acqsc").strip().casefold()
        # 프로젝트 코드는 모든 Redis 키의 첫 번째 구간에 사용하므로
        # 구분자인 콜론이나 공백이 들어가지 않도록 제한한다.
        if not re.fullmatch(r"[a-z0-9_-]+", project_code):
            raise ValueError(
                "PROJECT_CODE는 영문 소문자, 숫자, 밑줄, 하이픈만 "
                "사용할 수 있습니다."
            )

        settings = cls(
            genos_url=os.getenv(
                "GENOS_URL", "https://genos.genon.ai"
            ).rstrip("/"),
            genos_serving_id=int(os.getenv("GENOS_SERVING_ID", "850")),
            genos_model=os.getenv("GENOS_MODEL", "qwen/qwen3.7-flash"),
            genos_bearer_token=os.getenv("GENOS_BEARER_TOKEN"),
            prompt_version=os.getenv("INTENT_PROMPT_VERSION"),
            history_backend=os.getenv(
                "CHAT_HISTORY_BACKEND", "redis"
            ).casefold(),
            redis_url=os.getenv(
                "REDIS_URL", "redis://localhost:6379/0"
            ),
            redis_history_key_prefix=os.getenv(
                "REDIS_HISTORY_KEY_PREFIX", "chat:history"
            ),
            history_limit=int(os.getenv("CHAT_HISTORY_LIMIT", "10")),
            redis_dedupe_ttl_seconds=int(
                os.getenv("REDIS_DEDUPE_TTL_SECONDS", "86400")
            ),
            project_code=project_code,
            hitl_state_backend=os.getenv(
                "HITL_STATE_BACKEND", "redis"
            ).casefold(),
            redis_hitl_key_prefix=os.getenv(
                "REDIS_HITL_KEY_PREFIX", "hitl:state"
            ),
            redis_hitl_ttl_seconds=int(
                os.getenv("REDIS_HITL_TTL_SECONDS", "3600")
            ),
            mcp_backend=os.getenv("MCP_BACKEND", "http").casefold(),
            mcp_id=int(os.getenv("MCP_ID", "475")),
            mcp_bearer_token=os.getenv("MCP_BEARER_TOKEN"),
            mcp_timeout_seconds=float(
                os.getenv("MCP_TIMEOUT_SECONDS", "30")
            ),
            csv_trace_enabled=os.getenv(
                "CSV_TRACE_ENABLED", "true"
            ).strip().casefold() in {"1", "true", "yes", "on"},
            csv_trace_dir=os.getenv(
                "CSV_TRACE_DIR", "data/intent_traces"
            ).strip(),
        )
        logger.info(
            "======== 환경설정 완료 | 프로젝트=%s | serving_id=%d | 모델=%s | "
            "프롬프트버전=%s | 이력저장소=%s | 이력개수=%d | "
            "HITL저장소=%s | HITL_TTL초=%d | MCP=%s | "
            "Checkpointer=사용안함",
            settings.project_code,
            settings.genos_serving_id,
            settings.genos_model,
            settings.prompt_version or "active.yaml",
            settings.history_backend,
            settings.history_limit,
            settings.hitl_state_backend,
            settings.redis_hitl_ttl_seconds,
            settings.mcp_backend,
        )
        return settings

    @property
    def genos_openai_base_url(self) -> str:
        """LangChain ChatOpenAI에 전달할 GenOS /v1 기본 URL을 반환한다."""

        return (
            f"{self.genos_url}/api/gateway/rep/serving/"
            f"{self.genos_serving_id}/v1"
        )

    @property
    def genos_mcp_url(self) -> str:
        """guide.ipynb와 동일한 GenOS Gateway MCP 엔드포인트를 반환한다."""

        return f"{self.genos_url}/api/gateway/mcp/{self.mcp_id}/mcp"
