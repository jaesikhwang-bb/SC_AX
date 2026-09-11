"""프론트 HITL 입력 요청과 검증에 공통으로 사용하는 도구."""

import re
from typing import Any, Callable

from app.observability import logger


ValidationErrors = dict[str, str]
InputValidator = Callable[[Any], ValidationErrors]


# HITL 입력은 Redis에 저장된 상태가 아니라 프론트가 보낸 humanInput[].code를
# 기준으로 가드레일 적용 여부를 결정한다. 등록되지 않은 코드는 항상 검사하여
# 새 입력이 실수로 가드레일을 우회하지 못하게 한다.
_HITL_INPUT_GUARDRAIL_POLICIES: dict[str, bool] = {
    # 에이전트 변경 승인은 고정된 제어값(yes/no)이므로 자연어 가드레일 대상이 아니다.
    "SWITCH_AGENT": False,
}


def register_hitl_input_guardrail_policy(
    input_code: str,
    *,
    enabled: bool,
) -> None:
    """Python HITL 정의의 입력 코드별 가드레일 정책을 등록한다.

    동일한 input_code를 여러 시나리오가 공유할 수는 있지만 정책은 같아야 한다.
    서로 다른 정책이 필요하면 프론트 계약에서 구분되는 input_code를 사용해야 한다.
    """

    normalized_code = str(input_code).strip()
    if not normalized_code:
        raise ValueError("HITL input_code는 비어 있을 수 없습니다.")
    previous = _HITL_INPUT_GUARDRAIL_POLICIES.get(normalized_code)
    if previous is not None and previous is not bool(enabled):
        raise ValueError(
            "동일한 HITL input_code에 서로 다른 가드레일 정책을 등록할 수 "
            f"없습니다: input_code={normalized_code}, 기존={previous}, 신규={enabled}"
        )
    _HITL_INPUT_GUARDRAIL_POLICIES[normalized_code] = bool(enabled)


def is_hitl_input_guardrail_enabled(input_code: str) -> bool:
    """입력 코드의 정책을 반환한다. 미등록 코드는 안전하게 True이다."""

    return _HITL_INPUT_GUARDRAIL_POLICIES.get(str(input_code).strip(), True)


def build_hitl_request(
    *,
    hitl_type: str,
    action_code: str | None = None,
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
        # 내부 재진입 route는 type을 사용하고 프론트 업무 분기는 action_code를
        # 사용한다. 값이 없으면 기존 type을 그대로 외부 code로 쓴다.
        "action_code": action_code or hitl_type,
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


def validate_switch_agent(value: Any) -> tuple[str, ValidationErrors]:
    """에이전트 변경 action의 yes/no 응답을 정규화하고 검증한다."""

    if not isinstance(value, dict):
        return "", {"SWITCH_AGENT": "응답은 JSON 객체여야 합니다."}
    decision = str(value.get("SWITCH_AGENT") or "").strip().casefold()
    if decision not in {"yes", "no"}:
        return decision, {"SWITCH_AGENT": "yes 또는 no 값을 입력해 주세요."}
    return decision, {}


def validate_input_value(
    human_input: Any,
    *,
    input_code: str,
    expected_value: str | None = None,
    pattern: str | None = None,
    min_length: int | None = None,
    max_length: int | None = None,
    allowed_values: list[str] | None = None,
    validation_message: str | None = None,
) -> tuple[str, ValidationErrors]:
    """action 입력 한 건을 정규화하고 공통 문자열 규칙으로 검증한다."""

    if not isinstance(human_input, dict):
        return "", {input_code: "응답은 JSON 객체여야 합니다."}
    raw_value = human_input.get(input_code)
    normalized = str(raw_value).strip() if raw_value is not None else ""
    if not normalized:
        return "", {input_code: "필수 입력값을 입력해 주세요."}

    error = validation_message or "입력값 형식을 확인해 주세요."
    if expected_value is not None and normalized != expected_value:
        return normalized, {input_code: error}
    if allowed_values is not None and normalized not in allowed_values:
        return normalized, {input_code: error}
    if min_length is not None and len(normalized) < min_length:
        return normalized, {input_code: error}
    if max_length is not None and len(normalized) > max_length:
        return normalized, {input_code: error}
    if pattern is not None and re.fullmatch(pattern, normalized) is None:
        return normalized, {input_code: error}
    return normalized, {}
