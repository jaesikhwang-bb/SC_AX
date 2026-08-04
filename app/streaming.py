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
        yield text[start:start + chunk_size]


def build_action_event(
    thread_id: str,
    interrupt: dict[str, Any] | None,
) -> dict[str, Any]:
    """기존 interrupt 필드를 프론트 action/humanInput 계약으로 변환한다."""

    source = interrupt or {}
    inputs = []
    for field in source.get("fields", []):
        if not isinstance(field, dict):
            continue
        inputs.append(
            {
                "code": field.get("name"),
                "label": field.get("label"),
                "type": field.get("type", "text"),
                "required": bool(field.get("required", False)),
                "expectedValue": field.get("expected_value"),
            }
        )
    return {
        "type": source.get("type"),
        "thread_id": thread_id,
        "message": source.get("message"),
        "inputs": inputs,
        "context": source.get("context", {}),
        "errors": source.get("errors", {}),
    }
