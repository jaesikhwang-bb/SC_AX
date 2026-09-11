"""표준 로그와 Langfuse 4.14.0 관측을 함께 제공하는 공통 도구.

GenOS 트레이싱 매뉴얼의 권장 계층대로 요청 1건을 trace, ``@timed`` 함수를
span, LLM 호출을 generation으로 기록한다. 요청 식별자는 ContextVar로 연결하고
Langfuse가 없거나 장애가 나면 관측만 no-op 처리한다. 프롬프트·최종 답변·RAG
전체 문서는 기록하지 않으며, 질문과 MCP는 가드레일 또는 로컬 비식별화를
통과한 짧은 FLOW 미리보기만 stdout에 남긴다.
"""

import hashlib
import importlib.metadata
import inspect
import json
import logging
import re
import sys
import threading
import time
import traceback
from collections.abc import Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, AsyncIterator, Callable, Iterator, ParamSpec, TypeVar

from langchain_core.callbacks import BaseCallbackHandler


P = ParamSpec("P")
R = TypeVar("R")

_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s %(message)s | "
    "추적정보=request_id=%(request_id)s, session_id=%(session_id)s, "
    "thread_id=%(thread_id)s, user_id=%(user_id)s | "
    "호출코드=%(module)s.%(funcName)s:%(lineno)d"
)

# 로그는 Bastion Guardian API를 동기적으로 매번 호출할 수 없다. 그렇게 하면
# 가드레일 호출 자체의 로그가 다시 가드레일을 호출하는 재귀와 요청 지연이 생긴다.
# 대신 모든 LogRecord가 반드시 통과하는 로컬 비식별화 경계를 두고, 사용자 질문과
# 최종 답변 본문은 app/api.py의 비동기 Bastion Guardian에서 별도로 검사한다.
_LOG_MAX_TEXT_LENGTH = 800
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "api_key",
        "apikey",
        "bearer_token",
        "password",
        "secret",
        "secret_key",
        "token",
    }
)
_PROTECTED_CONTENT_KEYS = frozenset(
    {
        "answer",
        "arguments",
        "content",
        "documents",
        "formattedresult",
        "formatted_result",
        "graph_state",
        "interrupt",
        "message",
        "parameters",
        "payload",
        "prompt",
        "query",
        "rawresult",
        "raw_result",
        "refined_query",
        "result",
    }
)
_CONTENT_FIELD_LABELS = (
    "사용자 질문",
    "질문",
    "원본질문",
    "보정질문",
    "전체답변",
    "고정답변",
    "정제답변",
    "답변",
    "사용자답변",
    "프롬프트",
    "검색문",
    "문서내용",
    "저장값",
    "신규상태",
    "복원상태",
    "정제결과",
    "전처리결과",
    "원본결과",
    "입력요약",
    "파라미터",
    "payload",
    "arguments",
)
_CONTENT_FIELD_PATTERN = re.compile(
    "(?P<prefix>(?:^|\\|\\s*)"
    + "(?:"
    + "|".join(re.escape(label) for label in _CONTENT_FIELD_LABELS)
    + ")=)(?P<value>.*?)(?=\\s*\\|\\s*|$)",
    flags=re.MULTILINE,
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(authorization|access[_-]?token|api[_-]?key|bearer[_-]?token|"
    r"password|secret(?:[_-]?key)?)\s*[:=]\s*([^\s,;|}\]]+)",
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9._%+-])"
)
_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:01[016789]|02|0[3-6][1-5])[- .]?\d{3,4}[- .]?\d{4}(?!\d)"
)
_RRN_PATTERN = re.compile(r"(?<!\d)\d{6}[- ]?[1-8]\d{6}(?!\d)")
_CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")
_EMPLOYEE_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])[SKU]\d{6,7}(?![A-Za-z0-9])", re.I)

# 애플리케이션 전체에서 같은 이름의 로거를 사용한다.
logger = logging.getLogger("master_agent")

# FastAPI의 비동기 요청은 같은 프로세스 안에서 동시에 실행될 수 있다. 전역
# 변수에 요청 ID를 넣으면 서로 다른 사용자의 로그가 섞이므로 ContextVar로 현재
# 비동기 실행 문맥에만 추적 식별자를 보관한다.
_LOG_CONTEXT: ContextVar[dict[str, str]] = ContextVar(
    "master_agent_log_context",
    default={},
)

# 개발 목업이 명시적으로 요청한 경우에만 현재 비동기 요청 문맥의 함수 실행
# 정보를 SSE로 전달한다. 일반 운영 요청에서는 값이 None이므로 기존 로그 외에
# 어떠한 내부 코드 경로도 응답에 노출하지 않는다.
_DEVELOPER_TRACE_SINK: ContextVar[
    Callable[[dict[str, Any]], None] | None
] = ContextVar("master_agent_developer_trace_sink", default=None)

# 첨부된 GenOS 트레이싱 매뉴얼은 Python SDK v4의 정확한 버전 4.14.0을
# 전제로 한다. SDK가 없거나 버전·키가 맞지 않으면 업무 요청은 그대로 처리하고
# Langfuse 전송만 no-op으로 바뀐다.
_LANGFUSE_REQUIRED_VERSION = "4.14.0"
_LANGFUSE_ACTIVE_REQUEST: ContextVar[bool] = ContextVar(
    "master_agent_langfuse_active_request",
    default=False,
)
_LANGFUSE_TRACE_FINALIZED: ContextVar[bool] = ContextVar(
    "master_agent_langfuse_trace_finalized",
    default=False,
)
_LANGFUSE_CLIENT: Any | None = None
_LANGFUSE_PROPAGATE_ATTRIBUTES: Callable[..., Any] | None = None
_LANGFUSE_ENABLED = False
_LANGFUSE_REASON = "초기화 전"
_LANGFUSE_PROJECT_CODE = "acqsc"
_LANGFUSE_LOCK = threading.Lock()
_LANGFUSE_SKIPPED_CHECKPOINT_CODES = frozenset(
    {
        # 문장별 OUTPUT 검사는 SSE 토큰 수만큼 반복되므로 Langfuse 관측을
        # 과도하게 만들지 않는다. 최종 조립 단계에서 전체 결과를 한 번 기록한다.
        "OUTPUT_SENTENCE_APPROVED",
        "OUTPUT_FINAL_TAIL_APPROVED",
    }
)


class _RequestContextFilter(logging.Filter):
    """모든 로그 레코드에 요청 추적용 공통 필드를 추가한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _LOG_CONTEXT.get()
        record.request_id = _safe_identifier(context.get("request_id", "-"))
        record.session_id = _safe_identifier(context.get("session_id", "-"))
        record.thread_id = _safe_identifier(context.get("thread_id", "-"))
        # 사번은 원문 대신 같은 사용자를 계속 추적할 수 있는 안정적인 해시를 쓴다.
        record.user_id = _stable_fingerprint(context.get("user_id", "-"))
        return True


class _LogGuardrailFilter(logging.Filter):
    """모든 로그 인자와 템플릿을 출력 전에 비식별화한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        # 포맷 문자열의 ``%s`` 자체를 먼저 바꾸면 인자 개수 불일치가 날 수
        # 있으므로 템플릿은 유지한다. 인자는 여기서 정제하고 완성 문자열은
        # Formatter의 최종 출력 경계에서 다시 정제한다.
        record.args = sanitize_log_value(record.args)
        # 필터 통과 여부를 테스트·추가 핸들러에서도 확인할 수 있게 표시한다.
        record.log_guardrail_applied = True
        return True


class _CompactFlowInfoFilter(logging.Filter):
    """기존의 상세 INFO 로그는 숨기고 핵심 FLOW 경계만 터미널에 남긴다.

    WARNING 이상과 테스트·외부 라이브러리 로그는 그대로 유지한다. 기존 상세
    로그 호출은 코드에 남겨 두되 정상 운영 INFO 출력에는 노출하지 않는다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != logger.name or record.levelno != logging.INFO:
            return True
        # 단위 테스트가 보안 필터의 실제 출력 결과를 검사할 때는 해당 레코드를
        # 숨기지 않는다. 운영 코드 경로의 상세 로그만 compact 대상으로 삼는다.
        normalized_path = str(getattr(record, "pathname", "")).replace("\\", "/")
        if "/tests/" in normalized_path:
            return True
        message = str(record.msg)
        if message.startswith("========"):
            return False
        return True


class _ReadableMultilineFormatter(logging.Formatter):
    """구분 필드와 복합 자료형을 터미널용 여러 줄 로그로 변환한다.

    기존 호출부는 ``제목 | 필드=값 | 필드=값`` 형식을 계속 사용한다. 포맷터가
    각 ``|`` 필드를 새 줄의 ``-`` 항목으로 바꾸므로 새로운 logger 호출을
    추가해도 별도의 줄바꿈 코드를 반복해서 작성할 필요가 없다.
    """

    def format(self, record: logging.LogRecord) -> str:
        message_template = str(record.msg)
        original_args = record.args
        try:
            record.args = _pretty_log_arguments(original_args)
            rendered = super().format(record)
        finally:
            # 같은 LogRecord를 다른 핸들러도 사용할 수 있으므로 원본 인자를
            # 반드시 복원해 포맷터 사이의 부작용을 막는다.
            record.args = original_args
        # 템플릿의 ``질문=...``, ``답변=...`` 같은 본문 필드는 값의 종류와
        # 관계없이 마지막 출력 경계에서 한 번 더 요약한다.
        rendered = sanitize_log_text(_protect_labeled_content(rendered))
        expanded = _expand_log_fields(rendered)
        # 함수/요청의 시작과 실패 진단은 앞에 빈 줄을 두어 연속된 터미널
        # 출력에서도 새로운 로그 블록의 경계를 즉시 식별할 수 있게 한다.
        if message_template.startswith(
            (
                "======== 단계 시작",
                "======== 요청 도착",
                "======== 애플리케이션 시작",
                "FLOW ",
                "!!!!!!!! 실패 진단",
                "!!!!!!!! 처리 중단 진단",
            )
        ):
            return f"\n{expanded}"
        return expanded

    def formatException(self, ei: Any) -> str:
        """스택 위치는 유지하고 예외 메시지 본문은 길이·지문으로 치환한다."""

        exc_type, exc, tb = ei
        frames = "".join(traceback.format_list(traceback.extract_tb(tb)))
        type_name = getattr(exc_type, "__name__", str(exc_type))
        return sanitize_log_text(
            "Traceback (most recent call last):\n"
            f"{frames}{type_name}: {_protected_text_summary(str(exc))}"
        )


def _stable_fingerprint(value: Any) -> str:
    """원문을 노출하지 않으면서 같은 값을 로그에서 연결할 식별자를 만든다."""

    normalized = str(value or "").strip()
    if not normalized or normalized == "-":
        return "-"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def _protected_text_summary(value: Any) -> str:
    """긴 본문 대신 길이와 단방향 지문만 남긴다."""

    text = str(value or "")
    return f"<보호됨 길이={len(text)} 지문={_stable_fingerprint(text)}>"


def _safe_identifier(value: Any) -> str:
    """추적 ID에서 비밀 패턴만 제거하고 일반 UUID/요청 ID는 유지한다."""

    return sanitize_log_text(str(value or "-"))


def sanitize_log_text(value: str) -> str:
    """stdout/trace에 기록하기 전 비밀정보와 대표 개인정보 패턴을 마스킹한다."""

    text = str(value)
    text = _BEARER_PATTERN.sub("Bearer ***", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=***", text)
    text = _EMAIL_PATTERN.sub("***@***", text)
    text = _PHONE_PATTERN.sub("***-****-****", text)
    text = _RRN_PATTERN.sub("******-*******", text)
    text = _CARD_PATTERN.sub("****-****-****-****", text)
    text = _EMPLOYEE_ID_PATTERN.sub("***EMPLOYEE***", text)
    if len(text) > _LOG_MAX_TEXT_LENGTH:
        text = f"{text[:_LOG_MAX_TEXT_LENGTH]}…<잘림 원문길이={len(text)}>"
    return text


def sanitize_log_value(value: Any, *, key: str | None = None) -> Any:
    """복합 로그 인자를 재귀적으로 정제한다.

    dict/list를 통째로 문자열화하지 않으며 비밀 키의 값은 자료형과 무관하게
    제거한다. 숫자와 bool은 포맷 문자열 ``%d``/``%.3f`` 호환을 위해 유지한다.
    """

    normalized_key = str(key or "").strip().casefold()
    if normalized_key in _SENSITIVE_KEYS:
        return "***"
    if normalized_key in _PROTECTED_CONTENT_KEYS:
        if isinstance(value, Mapping):
            return {
                "protected": True,
                "type": "object",
                "fieldCount": len(value),
            }
        if isinstance(value, (list, tuple, set)):
            return {
                "protected": True,
                "type": "array",
                "itemCount": len(value),
            }
        return _protected_text_summary(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_log_text(value)
    if isinstance(value, Mapping):
        return {
            sanitize_log_text(str(item_key)): sanitize_log_value(
                item_value,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, tuple):
        return tuple(sanitize_log_value(item) for item in value)
    if isinstance(value, list):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, set):
        return {sanitize_log_value(item) for item in value}
    return sanitize_log_text(str(value))


def compact_log_preview(value: Any, *, limit: int = 100) -> str:
    """MCP 등 복합 결과를 로컬 마스킹한 한 줄 미리보기로 만든다."""

    safe_value = sanitize_log_value(value)
    if isinstance(safe_value, str):
        rendered = safe_value
    else:
        try:
            rendered = json.dumps(safe_value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(safe_value)
    rendered = " ".join(sanitize_log_text(rendered).split())
    safe_limit = max(1, int(limit))
    return rendered[:safe_limit] + ("…" if len(rendered) > safe_limit else "")


def _protect_labeled_content(rendered: str) -> str:
    """질문·답변·문서·payload 계열 필드의 원문을 길이/지문으로 치환한다."""

    return _CONTENT_FIELD_PATTERN.sub(
        lambda match: match.group("prefix")
        + _protected_text_summary(match.group("value")),
        rendered,
    )


def _pretty_log_arguments(arguments: Any) -> Any:
    """logger의 dict/list/tuple 값을 들여쓰기된 문자열로 변환한다."""

    if isinstance(arguments, tuple):
        return tuple(_pretty_log_value(value) for value in arguments)
    if isinstance(arguments, dict):
        # ``logger.info("%(name)s", {"name": ...})`` 형식도 보존한다.
        return {key: _pretty_log_value(value) for key, value in arguments.items()}
    return arguments


def _pretty_log_value(value: Any) -> Any:
    """복합 객체만 보기 좋은 JSON으로 만들고 숫자·문자열은 그대로 둔다."""

    if not isinstance(value, (dict, list, tuple, set)):
        return value
    # 원본 MCP 결과·문서·상태 객체가 로그에 재출력되지 않도록 복합 객체는
    # 구조와 건수만 남긴다. 짧은 scalar 배열은 에이전트/시나리오 코드 추적에
    # 유용하므로 값 자체를 유지한다.
    if isinstance(value, dict):
        keys = [sanitize_log_text(str(key)) for key in value]
        return f"<object 필드수={len(value)} 키={keys[:20]}>"
    serializable = sorted(value, key=str) if isinstance(value, set) else value
    if all(item is None or isinstance(item, (str, bool, int, float)) for item in serializable):
        if len(serializable) <= 20:
            return json.dumps(serializable, ensure_ascii=False, default=str)
    return f"<array 항목수={len(serializable)}>"


def _expand_log_fields(rendered: str) -> str:
    """각 로그 줄의 파이프 구분 필드를 줄바꿈된 목록으로 펼친다."""

    output: list[str] = []
    for line in rendered.splitlines():
        fields = line.split(" | ")
        output.append(fields[0])
        output.extend(f"    - {field}" for field in fields[1:])
    return "\n".join(output)


def configure_logging(level: str = "INFO") -> None:
    """애플리케이션 시작 시 UTF-8 출력과 공통 로그 형식을 설정한다."""

    configured_level = getattr(logging, level.upper(), logging.INFO)

    # Windows PowerShell의 기본 코드페이지와 Python 출력 인코딩이 다르면 한글
    # 로그가 깨질 수 있다. 지원되는 스트림은 UTF-8로 명시해 그대로 출력한다.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=configured_level,
        format=_LOG_FORMAT,
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(configured_level)
    logger.setLevel(configured_level)
    logger.disabled = False

    # uvicorn이 먼저 로깅 핸들러를 만든 환경에서는 basicConfig가 기존 핸들러를
    # 교체하지 않는다. 현재 루트 핸들러에도 필터와 포맷을 명시적으로 적용해
    # 개발·운영 실행 방식과 관계없이 같은 추적 필드가 출력되도록 한다.
    formatter = _ReadableMultilineFormatter(_LOG_FORMAT)
    # 애플리케이션 로거 자체에도 필터를 연결해 향후 별도 handler를 추가해도
    # 정제되지 않은 LogRecord가 밖으로 나가지 않게 한다.
    if not any(isinstance(item, _LogGuardrailFilter) for item in logger.filters):
        logger.addFilter(_LogGuardrailFilter())
    if not any(isinstance(item, _RequestContextFilter) for item in logger.filters):
        logger.addFilter(_RequestContextFilter())
    if not any(isinstance(item, _CompactFlowInfoFilter) for item in logger.filters):
        logger.addFilter(_CompactFlowInfoFilter())
    for handler in root_logger.handlers:
        if not any(isinstance(item, _LogGuardrailFilter) for item in handler.filters):
            handler.addFilter(_LogGuardrailFilter())
        if not any(isinstance(item, _RequestContextFilter) for item in handler.filters):
            handler.addFilter(_RequestContextFilter())
        handler.setFormatter(formatter)


def configure_langfuse(
    *,
    enabled: bool,
    host: str,
    public_key: str | None,
    secret_key: str | None,
    auth_check_on_startup: bool,
    project_code: str,
) -> dict[str, Any]:
    """Settings가 전달한 값으로 Langfuse 4.14.0 클라이언트를 초기화한다.

    다음 조건을 모두 만족할 때만 활성화한다.

    - ``enabled``가 true
    - host, public key, secret key가 존재
    - 설치된 Python SDK가 정확히 4.14.0

    환경변수 해석은 ``app/config.py``만 담당한다. 이 모듈은 전달받은 설정으로
    SDK를 명시적으로 생성하므로 설정 출처가 분산되지 않는다.

    초기화 실패는 관측 기능만 비활성화하고 채팅·MCP·Redis 흐름에는 전파하지
    않는다. 키와 호스트 원문은 로그에 기록하지 않는다.
    """

    global _LANGFUSE_CLIENT
    global _LANGFUSE_ENABLED
    global _LANGFUSE_PROJECT_CODE
    global _LANGFUSE_PROPAGATE_ATTRIBUTES
    global _LANGFUSE_REASON

    normalized_host = str(host or "").strip().rstrip("/")
    normalized_public_key = str(public_key or "").strip()
    normalized_secret_key = str(secret_key or "").strip()

    with _LANGFUSE_LOCK:
        _LANGFUSE_CLIENT = None
        _LANGFUSE_PROPAGATE_ATTRIBUTES = None
        _LANGFUSE_ENABLED = False
        _LANGFUSE_PROJECT_CODE = str(project_code or "acqsc").strip() or "acqsc"

        if not enabled:
            _LANGFUSE_REASON = "환경설정으로 비활성"
            logger.info(
                "======== Langfuse 비활성 | SDK요구버전=%s | 사유=%s",
                _LANGFUSE_REQUIRED_VERSION,
                _LANGFUSE_REASON,
            )
            return langfuse_status()

        missing = [
            name
            for name, value in (
                ("langfuse_host", normalized_host),
                ("langfuse_public_key", normalized_public_key),
                ("langfuse_secret_key", normalized_secret_key),
            )
            if not value
        ]
        if missing:
            _LANGFUSE_REASON = f"필수 설정 누락: {','.join(missing)}"
            logger.warning(
                "!!!!!!!! Langfuse 비활성 | SDK요구버전=%s | 사유=%s | "
                "업무처리계속=예",
                _LANGFUSE_REQUIRED_VERSION,
                _LANGFUSE_REASON,
            )
            return langfuse_status()

        try:
            installed_version = importlib.metadata.version("langfuse")
            if installed_version != _LANGFUSE_REQUIRED_VERSION:
                _LANGFUSE_REASON = (
                    "SDK 버전 불일치 "
                    f"(설치={installed_version}, 요구={_LANGFUSE_REQUIRED_VERSION})"
                )
                logger.warning(
                    "!!!!!!!! Langfuse 비활성 | 사유=%s | 업무처리계속=예",
                    _LANGFUSE_REASON,
                )
                return langfuse_status()
            from langfuse import Langfuse, propagate_attributes

            # get_client()의 환경변수 자동 로딩에 의존하지 않고 Settings가 전달한
            # 값을 명시하여 config.py를 유일한 설정 진입점으로 유지한다.
            client = Langfuse(
                public_key=normalized_public_key,
                secret_key=normalized_secret_key,
                base_url=normalized_host,
            )
            # auth_check는 네트워크 호출이므로 기본적으로 수행하지 않는다. 배포
            # 검증 시에만 켜며 실패해도 애플리케이션 기동은 계속한다.
            if auth_check_on_startup and not bool(client.auth_check()):
                raise RuntimeError("Langfuse auth_check 결과가 false입니다.")
            _LANGFUSE_CLIENT = client
            _LANGFUSE_PROPAGATE_ATTRIBUTES = propagate_attributes
            _LANGFUSE_ENABLED = True
            _LANGFUSE_REASON = "활성"
            logger.info(
                "======== Langfuse 활성 | SDK버전=%s | 프로젝트=%s | "
                "호스트설정=확인 | 프로젝트키설정=확인 | 시작인증확인=%s | "
                "전송내용=단계·시간·토큰·상태·건수 | 원문전송=안함",
                installed_version,
                _LANGFUSE_PROJECT_CODE,
                auth_check_on_startup,
            )
        except Exception as exc:  # 관측 장애는 업무 흐름과 반드시 격리한다.
            _LANGFUSE_CLIENT = None
            _LANGFUSE_PROPAGATE_ATTRIBUTES = None
            _LANGFUSE_ENABLED = False
            # /health에 외부 URL이나 SDK 내부 오류 원문이 노출되지 않도록 유형만
            # 공개하고, 서버 로그에도 오류 내용은 보호 요약으로 남긴다.
            _LANGFUSE_REASON = f"초기화 실패({type(exc).__name__})"
            logger.warning(
                "!!!!!!!! Langfuse 초기화 실패 | SDK요구버전=%s | "
                "오류유형=%s | 오류=%s | Langfuse전송=비활성 | 업무처리계속=예",
                _LANGFUSE_REQUIRED_VERSION,
                type(exc).__name__,
                _protected_text_summary(str(exc)),
            )
        return langfuse_status()


def langfuse_status() -> dict[str, Any]:
    """키 원문을 제외한 현재 Langfuse 연결 상태를 반환한다."""

    return {
        "enabled": _LANGFUSE_ENABLED,
        "requiredSdkVersion": _LANGFUSE_REQUIRED_VERSION,
        "reason": sanitize_log_text(_LANGFUSE_REASON),
    }


def flush_langfuse() -> None:
    """FastAPI 종료 시 백그라운드 큐에 남은 관측을 전송한다.

    요청 처리 경로에서는 호출하지 않는다. Langfuse 장애나 flush 실패도 서버의
    정상 종료를 방해하지 않는다.
    """

    client = _LANGFUSE_CLIENT if _LANGFUSE_ENABLED else None
    if client is None:
        logger.info("======== Langfuse flush 생략 | 사유=%s", _LANGFUSE_REASON)
        return
    started = time.perf_counter()
    try:
        client.flush()
        logger.info(
            "======== Langfuse flush 완료 | 소요시간=%.3f초",
            _elapsed_seconds(started),
        )
    except Exception as exc:  # pragma: no cover - 외부 관측 서버 장애 경계
        logger.warning(
            "!!!!!!!! Langfuse flush 실패 | 오류유형=%s | 소요시간=%.3f초 | "
            "애플리케이션종료계속=예",
            type(exc).__name__,
            _elapsed_seconds(started),
        )


def _langfuse_metadata(value: Any) -> Any:
    """Langfuse로 보내는 모든 값을 로컬 로그 가드레일과 동일하게 정제한다."""

    return sanitize_log_value(value)


def _safe_langfuse_update(observation: Any | None, **values: Any) -> None:
    """관측 객체 업데이트 실패를 업무 흐름에서 격리한다."""

    if observation is None:
        return
    try:
        observation.update(
            **{key: _langfuse_metadata(value) for key, value in values.items()}
        )
    except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
        logger.warning(
            "!!!!!!!! Langfuse 관측 업데이트 실패 | 오류유형=%s | "
            "업무처리계속=예",
            type(exc).__name__,
        )


def finish_langfuse_trace(
    observation: Any | None,
    *,
    status: str,
    duration_seconds: float,
    agent_code: str | None = None,
    scenario_codes: Sequence[str] = (),
    answer_length: int = 0,
) -> None:
    """SSE 결과 원문 없이 trace의 최종 상태와 대표 지표만 기록한다."""

    _safe_langfuse_update(
        observation,
        output={
            "status": status,
            "agentCode": agent_code,
            "scenarioCodes": list(scenario_codes),
            "answerLength": max(0, int(answer_length)),
            "durationSeconds": round(max(0.0, duration_seconds), 3),
        },
        metadata={"finalStatus": status},
        level="ERROR" if status == "ERROR" else "DEFAULT",
    )
    if observation is not None:
        _LANGFUSE_TRACE_FINALIZED.set(True)


@contextmanager
def langfuse_request_trace(
    *,
    request_id: str,
    session_id: str,
    thread_id: str,
    user_id: str,
    endpoint: str,
    frontend_agent_code: str | None,
    is_hitl_continuation: bool,
    input_guardrail_action: str,
    input_length: int,
) -> Iterator[Any | None]:
    """SSE 파이프라인 코루틴 내부에 요청 1건의 루트 trace를 연다.

    실제 질문은 이미 INPUT 가드레일을 통과했더라도 Langfuse에 저장하지 않는다.
    대신 길이와 가드레일 결과만 남긴다. SDK 컨텍스트 시작·종료 실패는 삼키지만
    ``yield``로 실행되는 업무 코드 예외는 그대로 호출자에게 전달한다.
    """

    if not _LANGFUSE_ENABLED or _LANGFUSE_CLIENT is None:
        yield None
        return

    attributes_cm: Any | None = None
    observation_cm: Any | None = None
    observation: Any | None = None
    active_token: Any | None = None
    finalized_token: Any | None = None
    try:
        propagate = _LANGFUSE_PROPAGATE_ATTRIBUTES
        if propagate is None:
            raise RuntimeError("propagate_attributes가 초기화되지 않았습니다.")
        attributes_cm = propagate(
            session_id=str(session_id),
            user_id=_stable_fingerprint(user_id),
            trace_name=f"{_LANGFUSE_PROJECT_CODE}.chat",
            tags=[_LANGFUSE_PROJECT_CODE, "chat", "sse"],
        )
        attributes_cm.__enter__()
        observation_cm = _LANGFUSE_CLIENT.start_as_current_observation(
            name=f"{_LANGFUSE_PROJECT_CODE}.chat",
            as_type="span",
            input={
                "messageLength": max(0, int(input_length)),
                "guardrailAction": input_guardrail_action,
            },
            metadata=_langfuse_metadata(
                {
                    "requestId": request_id,
                    "threadId": thread_id,
                    "endpoint": endpoint,
                    "frontendAgentCode": frontend_agent_code,
                    "entryMode": (
                        "HITL_RESUME" if is_hitl_continuation else "NEW_CHAT"
                    ),
                    "source": "app/api.py:create_app.stream_chat.event_stream",
                }
            ),
        )
        observation = observation_cm.__enter__()
        active_token = _LANGFUSE_ACTIVE_REQUEST.set(True)
        finalized_token = _LANGFUSE_TRACE_FINALIZED.set(False)
        logger.info(
            "======== Langfuse trace 시작 | 이름=%s.chat | "
            "입력유형=%s | 질문길이=%d | 원문전송=안함",
            _LANGFUSE_PROJECT_CODE,
            "HITL재진입" if is_hitl_continuation else "신규질문",
            input_length,
        )
    except Exception as exc:
        logger.warning(
            "!!!!!!!! Langfuse trace 시작 실패 | 오류유형=%s | "
            "Langfuse전송=이번요청생략 | 업무처리계속=예",
            type(exc).__name__,
        )
        observation = None

    try:
        yield observation
    except BaseException as exc:
        if not _LANGFUSE_TRACE_FINALIZED.get():
            _safe_langfuse_update(
                observation,
                output={"status": "ERROR", "errorType": type(exc).__name__},
                level="ERROR",
                status_message=type(exc).__name__,
            )
        raise
    finally:
        if finalized_token is not None:
            _LANGFUSE_TRACE_FINALIZED.reset(finalized_token)
        if active_token is not None:
            _LANGFUSE_ACTIVE_REQUEST.reset(active_token)
        if observation_cm is not None:
            try:
                observation_cm.__exit__(None, None, None)
            except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
                logger.warning(
                    "!!!!!!!! Langfuse trace 종료 실패 | 오류유형=%s | "
                    "업무처리계속=예",
                    type(exc).__name__,
                )
        if attributes_cm is not None:
            try:
                attributes_cm.__exit__(None, None, None)
            except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
                logger.warning(
                    "!!!!!!!! Langfuse 속성 컨텍스트 종료 실패 | 오류유형=%s | "
                    "업무처리계속=예",
                    type(exc).__name__,
                )


@contextmanager
def _langfuse_stage_span(
    *,
    stage: str,
    func: Callable[..., Any],
) -> Iterator[Any | None]:
    """활성 요청 안에서 ``@timed`` 함수 하나를 자식 span으로 연다."""

    if not _LANGFUSE_ACTIVE_REQUEST.get() or _LANGFUSE_CLIENT is None:
        yield None
        return
    cm: Any | None = None
    observation: Any | None = None
    try:
        cm = _LANGFUSE_CLIENT.start_as_current_observation(
            name=f"stage.{stage}",
            as_type="span",
            metadata=_langfuse_metadata(
                {
                    "stage": stage,
                    "stageCode": f"PYTHON::{func.__module__}::{func.__qualname__}",
                    "source": _trace_source(func),
                }
            ),
        )
        observation = cm.__enter__()
    except Exception as exc:
        logger.warning(
            "!!!!!!!! Langfuse span 시작 실패 | 단계=%s | 오류유형=%s | "
            "업무처리계속=예",
            stage,
            type(exc).__name__,
        )
        cm = None
    try:
        yield observation
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
                logger.warning(
                    "!!!!!!!! Langfuse span 종료 실패 | 단계=%s | "
                    "오류유형=%s | 업무처리계속=예",
                    stage,
                    type(exc).__name__,
                )


def record_langfuse_checkpoint(payload: Mapping[str, Any]) -> None:
    """API 조립·분기 checkpoint를 원문 없이 Langfuse event로 남긴다."""

    if not _LANGFUSE_ACTIVE_REQUEST.get() or _LANGFUSE_CLIENT is None:
        return
    stage_code = str(payload.get("stageCode") or "CHECKPOINT")
    if stage_code in _LANGFUSE_SKIPPED_CHECKPOINT_CODES:
        return
    try:
        # SDK 4.14.0에서 event는 start_observation(as_type="event")가 아니라
        # create_event()로 생성하며 호출 즉시 종료된 관측으로 기록된다.
        _LANGFUSE_CLIENT.create_event(
            name=f"checkpoint.{stage_code}",
            metadata=_langfuse_metadata(dict(payload)),
        )
    except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
        logger.warning(
            "!!!!!!!! Langfuse checkpoint 기록 실패 | 단계코드=%s | "
            "오류유형=%s | 업무처리계속=예",
            stage_code,
            type(exc).__name__,
        )


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """현재 요청의 로그에 공통 추적 식별자를 자동으로 붙인다.

    호출부는 매 단계마다 request_id 등을 반복해서 전달하지 않아도 된다. 값은
    컨텍스트 종료 시 반드시 이전 상태로 복원되므로 동시 요청끼리 섞이지 않는다.
    """

    current = dict(_LOG_CONTEXT.get())
    current.update(
        {key: str(value) for key, value in values.items() if value is not None}
    )
    token = _LOG_CONTEXT.set(current)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


@contextmanager
def developer_trace_context(
    sink: Callable[[dict[str, Any]], None] | None,
) -> Iterator[None]:
    """현재 요청의 ``@timed`` 실행 정보를 받을 개발용 sink를 설정한다.

    sink는 네트워크 I/O를 하지 않고 ``asyncio.Queue.put_nowait``처럼 즉시
    반환해야 한다. 추적 기능의 오류가 업무 응답을 깨뜨리지 않도록 sink 예외는
    :func:`emit_developer_trace`에서 로그만 남기고 무시한다.
    """

    token = _DEVELOPER_TRACE_SINK.set(sink)
    try:
        yield
    finally:
        _DEVELOPER_TRACE_SINK.reset(token)


def emit_developer_trace(payload: dict[str, Any]) -> None:
    """활성 개발 추적 sink에 구조화된 단계 정보를 전달한다."""

    sink = _DEVELOPER_TRACE_SINK.get()
    if sink is None:
        return
    try:
        # 개발용 SSE trace도 서버 로그와 같은 보호 경계를 사용한다.
        guarded_payload = sanitize_log_value(dict(payload))
        sink(guarded_payload if isinstance(guarded_payload, dict) else {})
    except Exception as exc:  # pragma: no cover - 진단 기능은 업무 흐름을 막지 않는다.
        logger.warning(
            "!!!!!!!! 개발 추적 이벤트 생성 실패 | 오류유형=%s",
            type(exc).__name__,
        )


def error_code_for_exception(exc: BaseException) -> str:
    """예외 클래스명을 목업에서 검색하기 쉬운 안정적인 오류 코드로 바꾼다."""

    explicit_codes = {
        "HitlStateNotFoundError": "HITL_STATE_NOT_FOUND",
        "HitlStateStoreUnavailableError": "HITL_STATE_STORE_UNAVAILABLE",
        "RequestValidationError": "REQUEST_VALIDATION_ERROR",
        "HTTPStatusError": "UPSTREAM_HTTP_STATUS_ERROR",
        "ConnectError": "UPSTREAM_CONNECTION_ERROR",
        "ReadTimeout": "UPSTREAM_READ_TIMEOUT",
        "TimeoutException": "UPSTREAM_TIMEOUT",
        "JSONDecodeError": "INVALID_JSON_RESPONSE",
    }
    type_name = type(exc).__name__
    if type_name in explicit_codes:
        return explicit_codes[type_name]
    snake_name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", type_name)
    snake_name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake_name).upper()
    return snake_name or "UNEXPECTED_ERROR"


def _trace_source(func: Callable[..., Any]) -> dict[str, Any]:
    """데코레이트된 함수의 저장소 상대 파일·함수·시작 줄을 반환한다."""

    module = func.__module__
    file_path = f"{module.replace('.', '/')}.py"
    code = getattr(func, "__code__", None)
    return {
        "file": file_path,
        "function": func.__qualname__,
        "line": code.co_firstlineno if code is not None else None,
    }


def _emit_timed_trace(
    *,
    stage: str,
    phase: str,
    func: Callable[..., Any],
    started: float,
    exc: BaseException | None = None,
) -> None:
    """``@timed`` 공통 추적 봉투를 생성한다."""

    payload: dict[str, Any] = {
        "kind": "python_function",
        "stage": stage,
        "stageCode": f"PYTHON::{func.__module__}::{func.__qualname__}",
        "phase": phase,
        "source": _trace_source(func),
        "durationMs": round(_elapsed_seconds(started) * 1000, 3),
    }
    if exc is not None:
        root = exc
        visited: set[int] = set()
        while id(root) not in visited:
            visited.add(id(root))
            nested = root.__cause__ or root.__context__
            if nested is None:
                break
            root = nested
        payload["error"] = {
            "code": error_code_for_exception(exc),
            "type": type(exc).__name__,
            "message": _protected_text_summary(str(exc)),
            "rootType": type(root).__name__,
            "rootMessage": _protected_text_summary(str(root)),
        }
        payload["customizationHint"] = (
            f"{payload['source']['file']}의 {func.__qualname__} 함수와 "
            "동일 request_id의 서버 실패 진단 로그를 확인하세요."
        )
    emit_developer_trace(payload)


@dataclass(frozen=True)
class LlmTokenUsage:
    """LLM 호출 1건에서 수집한 표준 토큰 사용량."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class LlmUsageCallbackHandler(BaseCallbackHandler):
    """LangChain LLM 호출을 generation 단위로 시간·토큰만 기록한다.

    프롬프트와 응답 본문은 기록하지 않는다. 비스트리밍/스트리밍 모두
    ``on_llm_end``의 LLMResult에서 usage를 읽으며, GenOS가 usage를 제공하지
    않으면 명시적으로 ``미제공``으로 남긴다.
    """

    def __init__(self, *, stage: str, model: str) -> None:
        self._stage = str(stage).strip()
        self._model = str(model).strip()
        self._started: dict[str, float] = {}
        self._generations: dict[str, Any] = {}
        self._lock = threading.Lock()

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        del serialized, prompts, kwargs
        self._start_generation(run_id)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: Any,
        **kwargs: Any,
    ) -> None:
        del serialized, messages, kwargs
        self._start_generation(run_id)

    def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        try:
            started = self._pop_started(run_id)
            usage = _extract_llm_token_usage(response)
            duration = time.perf_counter() - started if started is not None else None
            self._finish_generation(
                run_id,
                usage=usage,
                duration_seconds=duration,
                status="SUCCESS",
            )
            _log_llm_generation(
                stage=self._stage,
                model=self._model,
                usage=usage,
                duration_seconds=duration,
                status="SUCCESS",
            )
        except Exception as exc:  # pragma: no cover - 계측은 업무를 중단하지 않는다.
            logger.warning(
                "!!!!!!!! LLM generation 계측 생략 | 단계=%s | 오류유형=%s",
                self._stage,
                type(exc).__name__,
            )

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        del kwargs
        try:
            started = self._pop_started(run_id)
            duration = time.perf_counter() - started if started is not None else None
            self._finish_generation(
                run_id,
                usage=LlmTokenUsage(),
                duration_seconds=duration,
                status=f"ERROR:{type(error).__name__}",
            )
            _log_llm_generation(
                stage=self._stage,
                model=self._model,
                usage=LlmTokenUsage(),
                duration_seconds=duration,
                status=f"ERROR:{type(error).__name__}",
            )
        except Exception as exc:  # pragma: no cover - 계측은 업무를 중단하지 않는다.
            logger.warning(
                "!!!!!!!! LLM generation 오류 계측 생략 | 단계=%s | 오류유형=%s",
                self._stage,
                type(exc).__name__,
            )

    def _pop_started(self, run_id: Any) -> float | None:
        with self._lock:
            return self._started.pop(str(run_id), None)

    def _start_generation(self, run_id: Any) -> None:
        """LLM 스트리밍과 안전하게 공존하는 비컨텍스트 generation을 연다."""

        run_key = str(run_id)
        with self._lock:
            # 일부 LangChain 버전은 동일 run에 두 start callback을 호출할 수
            # 있으므로 중복 generation 생성을 막는다.
            if run_key in self._started:
                return
            self._started[run_key] = time.perf_counter()
            if not _LANGFUSE_ACTIVE_REQUEST.get() or _LANGFUSE_CLIENT is None:
                return
            try:
                self._generations[run_key] = _LANGFUSE_CLIENT.start_observation(
                    name=f"llm.{self._stage}",
                    as_type="generation",
                    model=self._model,
                    metadata=_langfuse_metadata(
                        {
                            "stage": self._stage,
                            "runId": run_key,
                            "inputContent": "기록하지 않음",
                            "outputContent": "기록하지 않음",
                        }
                    ),
                )
            except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
                logger.warning(
                    "!!!!!!!! Langfuse generation 시작 실패 | 단계=%s | "
                    "오류유형=%s | LLM호출계속=예",
                    self._stage,
                    type(exc).__name__,
                )

    def _finish_generation(
        self,
        run_id: Any,
        *,
        usage: LlmTokenUsage,
        duration_seconds: float | None,
        status: str,
    ) -> None:
        """토큰·시간·상태를 기록하고 generation을 반드시 닫는다."""

        with self._lock:
            generation = self._generations.pop(str(run_id), None)
        if generation is None:
            return
        try:
            usage_details = {
                key: value
                for key, value in (
                    ("input", usage.input_tokens),
                    ("output", usage.output_tokens),
                    ("total", usage.total_tokens),
                )
                if value is not None
            }
            update: dict[str, Any] = {
                "output": {"status": status},
                "metadata": {
                    "durationSeconds": (
                        round(duration_seconds, 3)
                        if duration_seconds is not None
                        else None
                    ),
                    "contentRecorded": False,
                },
                "level": "ERROR" if status.startswith("ERROR") else "DEFAULT",
            }
            if usage_details:
                update["usage_details"] = usage_details
            generation.update(**update)
        except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
            logger.warning(
                "!!!!!!!! Langfuse generation 업데이트 실패 | 단계=%s | "
                "오류유형=%s | LLM결과처리계속=예",
                self._stage,
                type(exc).__name__,
            )
        finally:
            try:
                generation.end()
            except Exception as exc:  # pragma: no cover - 외부 SDK 장애 경계
                logger.warning(
                    "!!!!!!!! Langfuse generation 종료 실패 | 단계=%s | "
                    "오류유형=%s | LLM결과처리계속=예",
                    self._stage,
                    type(exc).__name__,
                )


def create_llm_usage_callback(*, stage: str, model: str) -> BaseCallbackHandler:
    """ChatOpenAI 생성자에 넣을 공통 토큰 계측 callback을 만든다."""

    return LlmUsageCallbackHandler(stage=stage, model=model)


def _extract_llm_token_usage(response: Any) -> LlmTokenUsage:
    """LangChain 버전별 LLMResult/AIMessage usage 위치를 공통 해석한다."""

    candidates: list[Mapping[str, Any]] = []
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, Mapping):
        for key in ("token_usage", "usage", "usage_metadata"):
            value = llm_output.get(key)
            if isinstance(value, Mapping):
                candidates.append(value)

    generations = getattr(response, "generations", None)
    if isinstance(generations, Sequence):
        for generation_group in generations:
            if not isinstance(generation_group, Sequence):
                continue
            for generation in generation_group:
                message = getattr(generation, "message", None)
                usage_metadata = getattr(message, "usage_metadata", None)
                if isinstance(usage_metadata, Mapping):
                    candidates.append(usage_metadata)
                response_metadata = getattr(message, "response_metadata", None)
                if isinstance(response_metadata, Mapping):
                    token_usage = response_metadata.get("token_usage")
                    if isinstance(token_usage, Mapping):
                        candidates.append(token_usage)

    for candidate in candidates:
        input_tokens = _optional_int(
            candidate.get("input_tokens", candidate.get("prompt_tokens", candidate.get("input")))
        )
        output_tokens = _optional_int(
            candidate.get(
                "output_tokens",
                candidate.get("completion_tokens", candidate.get("output")),
            )
        )
        total_tokens = _optional_int(
            candidate.get("total_tokens", candidate.get("total"))
        )
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        if any(value is not None for value in (input_tokens, output_tokens, total_tokens)):
            return LlmTokenUsage(input_tokens, output_tokens, total_tokens)
    return LlmTokenUsage()


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _log_llm_generation(
    *,
    stage: str,
    model: str,
    usage: LlmTokenUsage,
    duration_seconds: float | None,
    status: str,
) -> None:
    """문서의 generation 권장 필드를 본문 없이 표준 로그/trace로 기록한다."""

    usage_status = (
        "수집완료"
        if usage.total_tokens is not None
        or usage.input_tokens is not None
        or usage.output_tokens is not None
        else "GenOS응답미제공"
    )
    logger.info(
        "FLOW LLM 호출 완료 | 단계=%s | 상태=%s | 모델=%s | "
        "입력토큰=%s | 출력토큰=%s | 전체토큰=%s | 토큰상태=%s | "
        "소요시간=%s초 | 프롬프트로그=생략 | 응답본문로그=생략",
        stage,
        status,
        model,
        usage.input_tokens if usage.input_tokens is not None else "미제공",
        usage.output_tokens if usage.output_tokens is not None else "미제공",
        usage.total_tokens if usage.total_tokens is not None else "미제공",
        usage_status,
        f"{duration_seconds:.3f}" if duration_seconds is not None else "미측정",
    )
    emit_developer_trace(
        {
            "kind": "generation",
            "stage": stage,
            "phase": "COMPLETED" if status == "SUCCESS" else "FAILED",
            "model": model,
            "usage": {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "total": usage.total_tokens,
                "status": usage_status,
            },
            "durationSeconds": (
                round(duration_seconds, 3) if duration_seconds is not None else None
            ),
        }
    )


def log_failure_diagnostic(
    *,
    stage: str,
    code_location: str,
    exc: BaseException,
    likely_cause: str,
    corrective_action: str,
    retry_count: int = 0,
    context: Any | None = None,
) -> None:
    """실패 원인과 운영자가 확인할 수정 지점을 여러 줄과 스택으로 남긴다.

    호출자는 토큰·비밀번호를 제거한 ``context``만 전달해야 한다. 예외 체인의
    가장 안쪽 원인까지 함께 출력하므로 LangChain/httpx가 예외를 감싸더라도
    실제 연결·검증 오류를 찾기 쉽다. 이 함수는 예외를 삼키지 않으며 호출부가
    바로 ``raise``하여 최초 실패를 사용자 오류 이벤트까지 전달해야 한다.
    """

    root = exc
    visited: set[int] = set()
    while id(root) not in visited:
        visited.add(id(root))
        nested = root.__cause__ or root.__context__
        if nested is None:
            break
        root = nested

    logger.error(
        "!!!!!!!! 실패 진단 | 실패단계=%s | 코드위치=%s | "
        "예외유형=%s | 오류메시지=%s | 근본예외유형=%s | "
        "근본오류메시지=%s | 가능한원인=%s | 확인및수정=%s | "
        "자동재시도횟수=%d | 입력요약=%s",
        stage,
        code_location,
        type(exc).__name__,
        _protected_text_summary(str(exc)),
        type(root).__name__,
        _protected_text_summary(str(root)),
        likely_cause,
        corrective_action,
        retry_count,
        context if context is not None else "없음",
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _finish_langfuse_stage_span(
    observation: Any | None,
    *,
    phase: str,
    started: float,
    exc: BaseException | None = None,
) -> None:
    """단계 span에 본문 없이 종료 상태·시간·오류 유형만 기록한다."""

    duration = round(_elapsed_seconds(started), 3)
    output: dict[str, Any] = {
        "phase": phase,
        "durationSeconds": duration,
    }
    if exc is not None:
        output["errorType"] = type(exc).__name__
        output["errorCode"] = error_code_for_exception(exc)
    _safe_langfuse_update(
        observation,
        output=output,
        metadata={"durationSeconds": duration, "phase": phase},
        level="ERROR" if phase == "FAILED" else "DEFAULT",
        status_message=(type(exc).__name__ if phase == "FAILED" and exc else None),
    )


def timed(
    stage: str,
    *,
    expected_exceptions: tuple[type[BaseException], ...] = (),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """동기·비동기 함수의 시작, 완료, 중단, 실패와 소요시간을 기록한다.

    예외는 로그를 남긴 뒤 그대로 다시 발생시킨다. 따라서 이 데코레이터를
    적용해도 기존 비즈니스 동작이나 예외 처리 흐름은 바뀌지 않는다.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs):
                started = time.perf_counter()
                _emit_timed_trace(
                    stage=stage,
                    phase="STARTED",
                    func=func,
                    started=started,
                )
                logger.info(
                    "======== 단계 시작 | %s | 함수=%s",
                    stage,
                    func.__qualname__,
                )
                with _langfuse_stage_span(stage=stage, func=func) as span:
                    try:
                        result = await func(*args, **kwargs)
                    except expected_exceptions as exc:
                        _finish_langfuse_stage_span(
                            span,
                            phase="STOPPED",
                            started=started,
                            exc=exc,
                        )
                        _emit_timed_trace(
                            stage=stage,
                            phase="STOPPED",
                            func=func,
                            started=started,
                            exc=exc,
                        )
                        logger.info(
                            "======== 단계 중단 | %s | 함수=%s | 소요시간=%.3f초",
                            stage,
                            func.__qualname__,
                            _elapsed_seconds(started),
                        )
                        raise
                    except Exception as exc:
                        _finish_langfuse_stage_span(
                            span,
                            phase="FAILED",
                            started=started,
                            exc=exc,
                        )
                        _emit_timed_trace(
                            stage=stage,
                            phase="FAILED",
                            func=func,
                            started=started,
                            exc=exc,
                        )
                        logger.error(
                            "======== 단계 실패 | %s | 함수=%s | 코드위치=%s.%s | "
                            "예외유형=%s | 오류=%s | 자동재시도=없음 | "
                            "소요시간=%.3f초",
                            stage,
                            func.__qualname__,
                            func.__module__,
                            func.__qualname__,
                            type(exc).__name__,
                            _protected_text_summary(exc),
                            _elapsed_seconds(started),
                            exc_info=(type(exc), exc, exc.__traceback__),
                        )
                        raise
                    except BaseException as exc:
                        _finish_langfuse_stage_span(
                            span,
                            phase="STOPPED",
                            started=started,
                            exc=exc,
                        )
                        raise
                    _finish_langfuse_stage_span(
                        span,
                        phase="COMPLETED",
                        started=started,
                    )
                    _emit_timed_trace(
                        stage=stage,
                        phase="COMPLETED",
                        func=func,
                        started=started,
                    )
                    logger.info(
                        "======== 단계 완료 | %s | 함수=%s | 소요시간=%.3f초",
                        stage,
                        func.__qualname__,
                        _elapsed_seconds(started),
                    )
                    return result

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs):
            started = time.perf_counter()
            _emit_timed_trace(
                stage=stage,
                phase="STARTED",
                func=func,
                started=started,
            )
            logger.info(
                "======== 단계 시작 | %s | 함수=%s",
                stage,
                func.__qualname__,
            )
            with _langfuse_stage_span(stage=stage, func=func) as span:
                try:
                    result = func(*args, **kwargs)
                except expected_exceptions as exc:
                    _finish_langfuse_stage_span(
                        span,
                        phase="STOPPED",
                        started=started,
                        exc=exc,
                    )
                    _emit_timed_trace(
                        stage=stage,
                        phase="STOPPED",
                        func=func,
                        started=started,
                        exc=exc,
                    )
                    logger.info(
                        "======== 단계 중단 | %s | 함수=%s | 소요시간=%.3f초",
                        stage,
                        func.__qualname__,
                        _elapsed_seconds(started),
                    )
                    raise
                except Exception as exc:
                    _finish_langfuse_stage_span(
                        span,
                        phase="FAILED",
                        started=started,
                        exc=exc,
                    )
                    _emit_timed_trace(
                        stage=stage,
                        phase="FAILED",
                        func=func,
                        started=started,
                        exc=exc,
                    )
                    logger.error(
                        "======== 단계 실패 | %s | 함수=%s | 코드위치=%s.%s | "
                        "예외유형=%s | 오류=%s | 자동재시도=없음 | "
                        "소요시간=%.3f초",
                        stage,
                        func.__qualname__,
                        func.__module__,
                        func.__qualname__,
                        type(exc).__name__,
                        _protected_text_summary(exc),
                        _elapsed_seconds(started),
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                    raise
                except BaseException as exc:
                    _finish_langfuse_stage_span(
                        span,
                        phase="STOPPED",
                        started=started,
                        exc=exc,
                    )
                    raise
                _finish_langfuse_stage_span(
                    span,
                    phase="COMPLETED",
                    started=started,
                )
                _emit_timed_trace(
                    stage=stage,
                    phase="COMPLETED",
                    func=func,
                    started=started,
                )
                logger.info(
                    "======== 단계 완료 | %s | 함수=%s | 소요시간=%.3f초",
                    stage,
                    func.__qualname__,
                    _elapsed_seconds(started),
                )
                return result

        return sync_wrapper

    return decorator


@asynccontextmanager
async def async_timed_block(stage: str) -> AsyncIterator[None]:
    """LLM·MCP처럼 비동기로 실행되는 특정 코드 구간을 측정한다."""

    started = time.perf_counter()
    logger.info("======== 구간 시작 | %s", stage)
    try:
        yield
    except Exception:
        logger.exception(
            "======== 구간 실패 | %s | 소요시간=%.3f초",
            stage,
            _elapsed_seconds(started),
        )
        raise
    logger.info(
        "======== 구간 완료 | %s | 소요시간=%.3f초",
        stage,
        _elapsed_seconds(started),
    )


def _elapsed_seconds(started: float) -> float:
    """perf_counter 시작값을 기준으로 경과시간을 초 단위로 반환한다."""

    return time.perf_counter() - started
