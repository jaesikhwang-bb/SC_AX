"""사용자용 안전 오류 답변과 개발용 오류 상세 표시를 구성한다."""

import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|authorization)\s*[=:]\s*)[^\s,;]+"),
)


def build_safe_error_answer(
    base_message: str,
    *,
    error_code: Any,
    error_detail: Any,
    include_details: bool,
) -> str:
    """고정 문구에 선택적으로 오류 코드와 내용을 붙인다.

    개발에서는 ``RESPONSE_ERROR_DETAILS_ENABLED=true``로 원인을 화면에서도
    확인하고, 운영에서는 false로 바꾸어 고정 문구만 노출한다. 상세를 켜더라도
    토큰처럼 보이는 값은 마스킹하고 지나치게 긴 내용은 잘라낸다.
    """

    message = str(base_message).strip()
    if not include_details:
        return message

    code = _compact(error_code, fallback="UNKNOWN_ERROR", limit=120)
    detail = _compact(error_detail, fallback="상세 내용 없음", limit=800)
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub(r"\1***", detail)
    return f"{message} (오류코드: {code}, 오류내용: {detail})"


def _compact(value: Any, *, fallback: str, limit: int) -> str:
    """오류 메타데이터를 한 줄의 제한된 문자열로 만든다."""

    text = " ".join(str(value or fallback).split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
