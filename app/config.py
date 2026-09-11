"""환경변수에서 애플리케이션 설정을 읽는 모듈."""

import os
import re
from dataclasses import dataclass

from app.observability import logger, timed


# LLM 호출 상한 기본값. 응답이 끝나지 않는 구조화 출력을 끊기 위한 값이며
# 필드 기본값과 환경변수 기본값이 어긋나지 않도록 한 곳에서만 정의한다.
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
# 서브에이전트는 세부 시나리오 수만큼 matches 배열을 반환할 수 있고 사고 과정
# 토큰도 함께 소비한다. 정상 최대 출력의 두 배 이상으로 잡아, 이 값을 넘기면
# 유효한 응답이 아니라 반복 생성으로 보고 끊는다.
DEFAULT_LLM_MAX_TOKENS = 4096

# Langfuse의 비밀이 아닌 설정은 이 파일 한 곳에서 관리한다. 실제 배포 주소나
# 기동 인증 확인 정책이 다르면 아래 상수만 수정한다. Public/Secret Key는 절대로
# 코드에 적지 않고 컨테이너 Secret 환경변수로 넣는다.
DEFAULT_LANGFUSE_ENABLED = True
DEFAULT_LANGFUSE_HOST = "http://langfuse-web:3000"
DEFAULT_LANGFUSE_AUTH_CHECK_ON_STARTUP = False


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
    # Redis List로 저장하는 대화 이력의 유효시간이다. 메시지가 새로 저장될
    # 때마다 해당 사원·session·agent_code 이력 키의 TTL을 다시 갱신한다.
    redis_history_ttl_seconds: int = 3600
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_max_tokens: int = DEFAULT_LLM_MAX_TOKENS
    # 현재는 최초 오류를 그대로 확인하기 위해 0으로 사용한다. 운영 정책이
    # 확정된 뒤 환경변수만 올리면 LangChain 내부 재시도를 활성화할 수 있다.
    llm_max_retries: int = 0
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
    # MCP 서버는 환경별로 달라질 수 있으므로 코드 기본값을 두지 않는다.
    # HTTP 백엔드에서는 Settings.from_env가 MCP_ID 필수 여부를 검증한다.
    mcp_id: int | None = None
    mcp_bearer_token: str | None = None
    mcp_timeout_seconds: float = 30.0
    # httpx 연결 재시도 횟수이다. 0이면 최초 연결 실패를 즉시 발생시킨다.
    mcp_max_retries: int = 0
    # 로컬 의도분류 품질 확인용 CSV이다. 운영 개인정보 정책에 맞춰 끌 수 있다.
    csv_trace_enabled: bool = False
    csv_trace_dir: str = "data/intent_traces"
    # Databricks 하이브리드 검색 결과를 GenOS reranker로 다시 정렬할지 결정한다.
    # 토큰은 소스 기본값으로 두지 않고 환경변수나 Secret으로만 주입한다.
    reranking_enabled: bool = False
    reranking_serving_id: int = 226
    reranking_model: str = "bge-reranker-v2-m3"
    reranking_bearer_token: str | None = None
    reranking_top_n: int = 5
    reranking_timeout_seconds: float = 30.0
    # GenOS serving의 /v1 뒤에 붙는 경로다. 실제 배포 규격이 다르면 환경변수만
    # 바꿀 수 있도록 분리한다.
    reranking_endpoint_path: str = "rerank"
    guardrail_base_url: str = "https://bastionguardian-api.startfort.io"
    guardrail_endpoint_path: str = "/v1/huard/api"
    guardrail_api_key: str | None = None
    guardrail_timeout_seconds: float = 15.0
    guardrail_fail_open: bool = False
    # 개발 화면에서 고정 오류 문구 옆에 오류 코드·내용을 표시할지 결정한다.
    # 운영에서는 반드시 false로 두고 상세 원인은 서버 로그에서만 확인한다.
    response_error_details_enabled: bool = True
    # Langfuse의 설정값은 observability.py가 환경변수를 다시 읽지 않도록 Settings를
    # 통해 명시적으로 전달한다. 두 프로젝트 키는 컨테이너 환경변수에서만 받는다.
    langfuse_enabled: bool = DEFAULT_LANGFUSE_ENABLED
    langfuse_host: str = DEFAULT_LANGFUSE_HOST
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_auth_check_on_startup: bool = DEFAULT_LANGFUSE_AUTH_CHECK_ON_STARTUP
    # 채팅 요청의 실제 사번으로 직원 기본정보를 먼저 조회해 request_context.user를
    # 보강한다. 도구·인자·응답 필드가 운영 MCP마다 다르므로 환경설정으로 분리한다.
    employee_lookup_enabled: bool = True
    employee_lookup_tool_name: str = "test_tool"
    employee_lookup_employee_id_argument: str = "employee_id"
    employee_lookup_name_fields: tuple[str, ...] = (
        "employee_name",
        "name",
        "emp_name",
        "empName",
        "emplNm",
        "user_name",
    )

    @classmethod
    @timed("환경설정 불러오기")
    def from_env(cls) -> "Settings":
        """환경변수와 비밀이 아닌 기본값으로 설정 객체를 만든다."""

        project_code = os.getenv("PROJECT_CODE", "acqsc").strip().casefold()
        mcp_backend = os.getenv("MCP_BACKEND", "http").casefold()
        raw_mcp_id = os.getenv("MCP_ID", "").strip()
        if mcp_backend == "http" and not raw_mcp_id:
            raise ValueError("MCP_BACKEND=http인 경우 환경변수 MCP_ID가 필요합니다.")
        mcp_id = int(raw_mcp_id) if raw_mcp_id else None
        # 프로젝트 코드는 모든 Redis 키의 첫 번째 구간에 사용하므로
        # 구분자인 콜론이나 공백이 들어가지 않도록 제한한다.
        if not re.fullmatch(r"[a-z0-9_-]+", project_code):
            raise ValueError(
                "PROJECT_CODE는 영문 소문자, 숫자, 밑줄, 하이픈만 사용할 수 있습니다."
            )

        settings = cls(
            genos_url=os.getenv("GENOS_URL", "https://genos.genon.ai").rstrip("/"),
            genos_serving_id=int(os.getenv("GENOS_SERVING_ID", "850")),
            genos_model=os.getenv("GENOS_MODEL", "qwen/qwen3.7-flash"),
            genos_bearer_token=os.getenv("GENOS_BEARER_TOKEN","ffabf121d76e4b59aefb524b83cce180"),
            prompt_version=os.getenv("INTENT_PROMPT_VERSION"),
            history_backend=os.getenv("CHAT_HISTORY_BACKEND", "memory").casefold(),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            redis_history_key_prefix=os.getenv(
                "REDIS_HISTORY_KEY_PREFIX", "chat:history"
            ),
            history_limit=int(os.getenv("CHAT_HISTORY_LIMIT", "10")),
            redis_dedupe_ttl_seconds=int(
                os.getenv("REDIS_DEDUPE_TTL_SECONDS", "86400")
            ),
            redis_history_ttl_seconds=_positive_int_env(
                "REDIS_HISTORY_TTL_SECONDS", 3600
            ),
            llm_timeout_seconds=_positive_float_env(
                "LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS
            ),
            llm_max_tokens=_positive_int_env("LLM_MAX_TOKENS", DEFAULT_LLM_MAX_TOKENS),
            llm_max_retries=_nonnegative_int_env("LLM_MAX_RETRIES", 0),
            project_code=project_code,
            hitl_state_backend=os.getenv("HITL_STATE_BACKEND", "memory").casefold(),
            redis_hitl_key_prefix=os.getenv("REDIS_HITL_KEY_PREFIX", "hitl:state"),
            redis_hitl_ttl_seconds=int(os.getenv("REDIS_HITL_TTL_SECONDS", "3600")),
            mcp_backend=mcp_backend,
            mcp_id=mcp_id,
            mcp_bearer_token=os.getenv("MCP_BEARER_TOKEN"),
            mcp_timeout_seconds=float(os.getenv("MCP_TIMEOUT_SECONDS", "30")),
            mcp_max_retries=_nonnegative_int_env("MCP_MAX_RETRIES", 0),
            csv_trace_enabled=os.getenv("CSV_TRACE_ENABLED", "false").strip().casefold()
            in {"1", "true", "yes", "on"},
            csv_trace_dir=os.getenv("CSV_TRACE_DIR", "data/intent_traces").strip(),
            reranking_enabled=_bool_env("RERANKING_ENABLED", False),
            reranking_serving_id=int(os.getenv("RERANKING_SERVING_ID", "226")),
            reranking_model=os.getenv("RERANKING_MODEL", "bge-reranker-v2-m3").strip(),
            reranking_bearer_token=os.getenv("RERANKING_BEARER_TOKEN"),
            reranking_top_n=_positive_int_env("RERANKING_TOP_N", 5),
            reranking_timeout_seconds=float(
                os.getenv("RERANKING_TIMEOUT_SECONDS", "30")
            ),
            reranking_endpoint_path=os.getenv("RERANKING_ENDPOINT_PATH", "rerank")
            .strip()
            .strip("/"),
            guardrail_base_url=os.getenv(
                "AI_GUARDRAIL_BASE_URL",
                "https://bastionguardian-api.startfort.io",
            ).rstrip("/"),
            guardrail_endpoint_path=(
                "/"
                + os.getenv("AI_GUARDRAIL_ENDPOINT_PATH", "/v1/huard/api")
                .strip()
                .strip("/")
            ),
            guardrail_api_key=os.getenv("AI_GUARDRAIL_API_KEY", "").strip() or None,
            guardrail_timeout_seconds=_positive_float_env(
                "AI_GUARDRAIL_TIMEOUT_SECONDS", 15.0
            ),
            guardrail_fail_open=_bool_env("AI_GUARDRAIL_FAIL_OPEN", False),
            response_error_details_enabled=_bool_env(
                "RESPONSE_ERROR_DETAILS_ENABLED", True
            ),
            # Langfuse의 비밀이 아닌 값은 .env나 컨테이너 환경변수 대신 이
            # 파일 상단 상수에서 관리한다. 운영 변경 시 상수만 수정하면 된다.
            langfuse_enabled=DEFAULT_LANGFUSE_ENABLED,
            langfuse_host=DEFAULT_LANGFUSE_HOST,
            langfuse_public_key=(
                os.getenv("LANGFUSE_PUBLIC_KEY", "").strip() or None
            ),
            langfuse_secret_key=(
                os.getenv("LANGFUSE_SECRET_KEY", "").strip() or None
            ),
            langfuse_auth_check_on_startup=(
                DEFAULT_LANGFUSE_AUTH_CHECK_ON_STARTUP
            ),
            employee_lookup_enabled=_bool_env("EMPLOYEE_LOOKUP_ENABLED", True),
            employee_lookup_tool_name=(
                os.getenv("EMPLOYEE_LOOKUP_MCP_TOOL_NAME", "test_tool").strip()
                or "test_tool"
            ),
            employee_lookup_employee_id_argument=(
                os.getenv("EMPLOYEE_LOOKUP_EMPLOYEE_ID_ARGUMENT", "employee_id").strip()
                or "employee_id"
            ),
            employee_lookup_name_fields=tuple(
                field.strip()
                for field in os.getenv(
                    "EMPLOYEE_LOOKUP_NAME_FIELDS",
                    "employee_name,name,emp_name,empName,emplNm,user_name",
                ).split(",")
                if field.strip()
            ),
        )
        logger.info(
            "======== 환경설정 완료 | 프로젝트=%s | serving_id=%d | 모델=%s | "
            "프롬프트버전=%s | 이력저장소=%s | 이력개수=%d | "
            "대화이력_TTL초=%d | "
            "HITL저장소=%s | HITL_TTL초=%d | MCP=%s | Reranking=%s | "
            "Reranking모델=%s | "
            "LLM자동재시도=%d회 | MCP자동재시도=%d회 | "
            "오류상세노출=%s | Langfuse설정=%s | Langfuse호스트설정=%s | "
            "Langfuse프로젝트키설정=%s | Checkpointer=사용안함",
            settings.project_code,
            settings.genos_serving_id,
            settings.genos_model,
            settings.prompt_version or "active.yaml",
            settings.history_backend,
            settings.history_limit,
            settings.redis_history_ttl_seconds,
            settings.hitl_state_backend,
            settings.redis_hitl_ttl_seconds,
            settings.mcp_backend,
            settings.reranking_enabled,
            settings.reranking_model,
            settings.llm_max_retries,
            settings.mcp_max_retries,
            settings.response_error_details_enabled,
            settings.langfuse_enabled,
            bool(settings.langfuse_host),
            bool(settings.langfuse_public_key and settings.langfuse_secret_key),
        )
        return settings

    @property
    def llm_client_options(self) -> dict[str, object]:
        """ChatOpenAI 생성자에 그대로 전달할 공통 호출 상한 옵션."""

        # None을 그대로 넘기면 라이브러리 기본값을 사용한다. 값이 있으면
        # 마스터 분류기와 서브에이전트가 같은 상한을 공유한다.
        return {
            "timeout": self.llm_timeout_seconds,
            "max_retries": self.llm_max_retries,
            "max_tokens": self.llm_max_tokens,
        }

    @property
    def genos_openai_base_url(self) -> str:
        """LangChain ChatOpenAI에 전달할 GenOS /v1 기본 URL을 반환한다."""

        return f"{self.genos_url}/api/gateway/rep/serving/{self.genos_serving_id}/v1"

    @property
    def genos_mcp_url(self) -> str:
        """guide.ipynb와 동일한 GenOS Gateway MCP 엔드포인트를 반환한다."""

        if self.mcp_id is None:
            raise ValueError("MCP 호출에는 환경변수 MCP_ID가 필요합니다.")
        return f"{self.genos_url}/api/gateway/mcp/{self.mcp_id}/mcp"

    @property
    def genos_reranking_url(self) -> str:
        """GenOS reranker serving의 OpenAI 호환 기본 경로를 반환한다."""

        base = (
            f"{self.genos_url}/api/gateway/rep/serving/{self.reranking_serving_id}/v1"
        )
        return f"{base}/{self.reranking_endpoint_path}"


def _nonnegative_int_env(name: str, default: int) -> int:
    """재시도 설정을 0 이상의 정수로 검증해 잘못된 배포값을 즉시 알린다."""

    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name}는 0 이상의 정수여야 합니다: {value}")
    return value


def _positive_int_env(name: str, default: int) -> int:
    """1 이상의 정수 설정을 검증한다."""

    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name}는 1 이상의 정수여야 합니다: {value}")
    return value


def _positive_float_env(name: str, default: float) -> float:
    """0보다 큰 실수 설정을 검증한다."""

    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name}는 0보다 커야 합니다: {value}")
    return value


def _bool_env(name: str, default: bool) -> bool:
    """환경변수의 대표적인 참/거짓 문자열을 엄격하게 변환한다."""

    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}은 true 또는 false 형식이어야 합니다: {raw}")
