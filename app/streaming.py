"""프론트 SSE 계약의 이벤트 직렬화와 공통 변환 도구."""

import json
from typing import Any, Iterator


def encode_sse(event: str, data: Any) -> str:
    """한 이벤트를 한 줄 JSON과 SSE 종료 개행으로 직렬화한다."""

    payload = json.dumps(
        {"event": event, "data": data},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return f"data: {payload}\n\n"


def split_text(text: str, chunk_size: int = 16) -> Iterator[str]:
    """고정 답변도 실제 token 이벤트 흐름을 시험할 수 있도록 나눈다."""

    if chunk_size < 1:
        raise ValueError("chunk_size는 1 이상이어야 합니다.")
    for start in range(0, len(text), chunk_size):
        yield text[start : start + chunk_size]


def build_action_event(
    thread_id: str,
    interrupt: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """내부 interrupt를 순서형 프론트 action 배열로 변환한다.

    action 입력칸 한 개를 배열 원소 한 개로 보낸다. 따라서 팩스번호처럼 같은
    code를 공유하는 입력칸도 ``order``가 다르면 프론트에서 각각 렌더링할 수
    있다. 프론트는 각 원소의 code/input/order를 humanInput으로 되돌려 보낸다.
    """

    source = interrupt or {}
    action_code = source.get("action_code") or source.get("type")
    context = source.get("context")
    action_context = context if isinstance(context, dict) else {}
    actions: list[dict[str, Any]] = []
    fields = [
        field for field in source.get("fields", []) if isinstance(field, dict)
    ]
    # 동일한 action code로 여러 입력칸을 노출하는 경우 order 순서대로 보낸다.
    # order가 없는 기존 입력은 정의된 순서를 유지하며 뒤쪽에 배치한다.
    fields.sort(
        key=lambda field: (
            field.get("order") is None,
            int(field.get("order") or 0),
        )
    )
    for field in fields:
        item = {
            "code": action_code,
            "thread_id": thread_id,
            "message": source.get("message"),
            # humanInput[].code는 입력 필드의 name을 사용한다. 현재 SWITCH_AGENT와
            # INPUT_FAX는 action code와 input code가 같지만 두 계약을 명시적으로
            # 분리해 두면 향후 한 action에 서로 다른 입력 code도 지원할 수 있다.
            "inputCode": field.get("name"),
            "label": field.get("label"),
            "type": field.get("type", "text"),
            "required": bool(field.get("required", False)),
        }
        # 프론트는 SWITCH_AGENT 확인창을 표시할 때 마스터가 분류한 이동 대상
        # 에이전트 코드를 함께 알아야 한다. 내부 context 전체를 노출하지 않고
        # 허용된 classified_agent_code 하나만 외부 계약명으로 변환한다.
        if action_code == "SWITCH_AGENT":
            switch_agent_code = str(
                action_context.get("classified_agent_code") or ""
            ).strip().upper()
            if switch_agent_code:
                item["switch_agent_code"] = switch_agent_code
        # 일반 message 입력을 기다리는 안내 action이다. 주소처럼 선택값은 이미
        # Redis에 저장됐고 프론트는 별도 input을 렌더링하지 않은 채 채팅창을
        # 활성화해 같은 thread_id로 다음 message를 보내면 된다.
        if action_context.get("await_message_input") is True:
            item["inputMode"] = "message"
        optional_fields = {
            "expectedValue": field.get("expected_value"),
            "pattern": field.get("pattern"),
            "minLength": field.get("min_length"),
            "maxLength": field.get("max_length"),
            "allowedValues": field.get("allowed_values"),
            "order": field.get("order"),
        }
        item.update(
            {
                key: value
                for key, value in optional_fields.items()
                if value is not None
            }
        )
        if field.get("sensitive"):
            item["sensitive"] = True
        errors = source.get("errors")
        if isinstance(errors, dict) and errors:
            error_key = (
                f"{field.get('name')}:{field.get('order')}"
                if field.get("order") is not None
                else str(field.get("name") or "")
            )
            error = errors.get(error_key) or errors.get(field.get("name"))
            if error:
                item["error"] = error
        actions.append(item)
    # 새 프론트 필드가 실제로 필요해질 때만 이 whitelist에 명시적으로 추가한다.
    # interrupt.context, MCP 원본, handler 내부 상태는 운영 action에 노출하지 않는다.
    return actions
