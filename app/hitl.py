"""프론트 HITL 입력 요청과 검증에 공통으로 사용하는 도구."""

from typing import Any, Callable

from app.observability import logger


ValidationErrors = dict[str, str]
InputValidator = Callable[[Any], ValidationErrors]


def build_hitl_request(
    *,
    hitl_type: str,
    message: str,
    fields: list[dict[str, Any]],
    context: dict[str, Any],
    errors: ValidationErrors | None = None,
) -> dict[str, Any]:
    """프론트가 팝업 또는 입력 폼을 만들 수 있는 공통 JSON을 반환한다.

    이전 구현의 LangGraph ``interrupt()`` payload와 외부 API 형식을 동일하게
    유지한다. 이제 그래프는 중단하지 않고 이 값을 일반 Redis에 저장한 뒤 END로
    종료하며, 다음 HTTP 요청에서 저장 상태를 읽어 특정 Edge부터 새로 실행한다.
    """

    payload = {
        "type": hitl_type,
        "message": message,
        "fields": fields,
        "context": context,
        "errors": errors or {},
    }
    logger.info(
        "======== HITL 입력 요청 생성 | 유형=%s | 필드개수=%d | 오류개수=%d",
        hitl_type,
        len(fields),
        len(payload["errors"]),
    )
    return payload


def validate_ok_signal(value: Any) -> ValidationErrors:
    """현재 에이전트 변경 팝업의 OK 응답을 검증한다."""

    if not isinstance(value, dict):
        return {"signal": "응답은 JSON 객체여야 합니다."}
    if value.get("signal") != "OK":
        return {"signal": "OK 값을 입력해야 합니다."}
    return {}
