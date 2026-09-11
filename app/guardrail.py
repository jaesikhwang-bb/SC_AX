"""Bastion Guardian AI 가드레일 호출과 문장 단위 출력 정규화."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

import httpx

from app.config import Settings
from app.observability import logger, timed


GuardrailAction = Literal["PASS", "MASK", "BLOCK"]
GuardrailProcessType = Literal["INPUT", "OUTPUT"]
GuardrailRole = Literal["user", "assistant", "system"]

# OUTPUT MASK 결과에서 복원이 허용된 유일한 토큰 형식이다. 다른 PII 토큰을
# 실수로 원복하지 않도록 PERSON_NAME_숫자 형태만 명시적으로 허용한다.
_PERSON_NAME_MASK_WORD = re.compile(r"PERSON_NAME_\d+", re.IGNORECASE)


@dataclass(frozen=True)
class GuardrailContext:
    """고객사 API의 option/additionalData 추적 문맥."""

    trace_id: str
    session_id: str
    user_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    additional_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardrailDecision:
    """가드레일 판정과 실제로 사용해야 하는 처리 결과."""

    action: GuardrailAction
    processed_content: str | None
    applied: bool
    reason: str | None = None
    # 가드레일 검사 후 OUTPUT 이름 예외 정책으로 복원한 토큰 발생 횟수다.
    # 이름 원문 자체는 로그나 메타데이터에 넣지 않는다.
    restored_person_name_count: int = 0

    @property
    def allowed(self) -> bool:
        return self.action in {"PASS", "MASK"} and self.processed_content is not None


class GuardrailClient(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def process_text(
        self,
        content: str,
        *,
        role: GuardrailRole,
        process_type: GuardrailProcessType,
        context: GuardrailContext,
    ) -> GuardrailDecision: ...

    async def aclose(self) -> None: ...


class BastionGuardianClient:
    """API 키 유무에 따라 실제 검사 또는 명시적 PASS-through를 수행한다."""

    def __init__(
        self,
        *,
        base_url: str,
        endpoint_path: str,
        api_key: str | None,
        timeout_seconds: float,
        fail_open: bool,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key.strip() if api_key else None
        self._url = f"{base_url.rstrip('/')}/{endpoint_path.strip('/')}"
        self._fail_open = fail_open
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def enabled(self) -> bool:
        return self._api_key is not None

    @timed("Bastion Guardian 텍스트 검사")
    async def process_text(
        self,
        content: str,
        *,
        role: GuardrailRole,
        process_type: GuardrailProcessType,
        context: GuardrailContext,
    ) -> GuardrailDecision:
        normalized_role = str(role).strip().casefold()
        normalized_process = str(process_type).strip().upper()
        if normalized_role not in {"user", "assistant", "system"}:
            raise ValueError("가드레일 role은 user, assistant, system이어야 합니다.")
        if normalized_process not in {"INPUT", "OUTPUT"}:
            raise ValueError("가드레일 processType은 INPUT 또는 OUTPUT이어야 합니다.")

        # 고객사 규칙상 system은 검사에서 제외한다. 키가 아직 없으면 개발 단계의
        # 명시적 PASS 처리로 원문을 그대로 후속 단계에 넘긴다.
        if normalized_role == "system" or not self.enabled:
            return GuardrailDecision(
                action="PASS",
                processed_content=content,
                applied=False,
                reason="SYSTEM_EXCLUDED" if normalized_role == "system" else "NO_API_KEY",
            )

        payload = {
            "messages": [
                {
                    "role": normalized_role,
                    "content": [{"type": "text", "text": content}],
                }
            ],
            "processType": normalized_process,
            "additionalData": dict(context.additional_data),
            "option": {
                "trace_id": context.trace_id,
                "session_id": context.session_id,
                "user_id": context.user_id,
                "metadata": dict(context.metadata),
                "tags": list(context.tags),
            },
        }
        try:
            response = await self._http_client.post(
                self._url,
                headers={
                    "X-Starfort-Guard-Api-Key": self._api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()
            decision = parse_guardrail_response(response_payload, original=content)
            if (
                normalized_process == "OUTPUT"
                and decision.action == "MASK"
                and decision.processed_content is not None
            ):
                restored_content, restored_count = restore_output_person_names(
                    decision.processed_content,
                    response_payload,
                )
                decision = replace(
                    decision,
                    processed_content=restored_content,
                    restored_person_name_count=restored_count,
                )
            logger.info(
                "======== AI 가드레일 완료 | processType=%s | role=%s | "
                "action=%s | 원문길이=%d | 처리길이=%d | 이름복원개수=%d",
                normalized_process,
                normalized_role,
                decision.action,
                len(content),
                len(decision.processed_content or ""),
                decision.restored_person_name_count,
            )
            return decision
        except Exception as exc:
            logger.warning(
                "!!!!!!!! AI 가드레일 실패 | processType=%s | role=%s | "
                "정책=%s | 오류유형=%s",
                normalized_process,
                normalized_role,
                "fail-open" if self._fail_open else "fail-closed",
                type(exc).__name__,
            )
            if self._fail_open:
                return GuardrailDecision(
                    action="PASS",
                    processed_content=content,
                    applied=False,
                    reason="ERROR_FAIL_OPEN",
                )
            return GuardrailDecision(
                action="BLOCK",
                processed_content=None,
                applied=False,
                reason="ERROR_FAIL_CLOSED",
            )

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()


def parse_guardrail_response(payload: Any, *, original: str) -> GuardrailDecision:
    """PASS/MASK/BLOCK 및 공통 오류 봉투를 엄격하게 해석한다."""

    if not isinstance(payload, Mapping):
        raise ValueError("가드레일 응답이 JSON object가 아닙니다.")
    if payload.get("ok") is False or isinstance(payload.get("error"), Mapping):
        error = payload.get("error")
        code = error.get("code") if isinstance(error, Mapping) else "UNKNOWN"
        raise ValueError(f"가드레일 오류 응답: {code}")

    items = _result_items(payload)
    raw_action = payload.get("action")
    if raw_action is None and items:
        raw_action = items[0].get("action")
    action = str(raw_action or "").strip().upper()
    if action == "PASS":
        return GuardrailDecision("PASS", original, True)
    if action == "BLOCK":
        return GuardrailDecision("BLOCK", None, True)
    if action != "MASK":
        raise ValueError("가드레일 action은 PASS, MASK, BLOCK이어야 합니다.")

    for item in items:
        processed = item.get("processed_content")
        if isinstance(processed, str):
            return GuardrailDecision("MASK", processed, True)
    raise ValueError("MASK 응답에 processed_content가 없습니다.")


def restore_output_person_names(
    processed_content: str,
    payload: Mapping[str, Any],
) -> tuple[str, int]:
    """OUTPUT에서 이름 마스크만 ``matched_text``로 제한적으로 복원한다.

    복원 조건을 모두 만족한 항목만 사용한다.

    - 상위 정책이 PII이고 action이 MASK
    - detected item의 rule_name이 이름 규칙
    - mask_word가 ``PERSON_NAME_<숫자>`` 형식
    - 동일 mask_word가 서로 다른 원문을 가리키지 않음

    전화번호, 계좌번호, 주민번호 등 다른 mask_word는 수집하거나 변경하지 않는다.
    이 함수는 가드레일 호출을 대체하지 않으며, 검사 완료 후 OUTPUT 결과에만
    적용된다.
    """

    replacements: dict[str, str] = {}
    conflicts: set[str] = set()
    for result_item in _result_items(payload):
        policy_results = result_item.get("results")
        if not isinstance(policy_results, list):
            continue
        for policy_result in policy_results:
            if not isinstance(policy_result, Mapping):
                continue
            if str(policy_result.get("policy_type") or "").strip().upper() != "PII":
                continue
            if str(policy_result.get("action") or "").strip().upper() != "MASK":
                continue
            detected_items = policy_result.get("detected_items")
            if not isinstance(detected_items, list):
                continue
            for detected in detected_items:
                if not isinstance(detected, Mapping):
                    continue
                rule_name = str(detected.get("rule_name") or "").strip().casefold()
                if "이름" not in rule_name and "name" not in rule_name:
                    continue
                mask_word = str(detected.get("mask_word") or "").strip().strip("[]")
                if _PERSON_NAME_MASK_WORD.fullmatch(mask_word) is None:
                    continue
                matched_text = detected.get("matched_text")
                if not isinstance(matched_text, str) or not matched_text:
                    continue
                # 비정상 응답이 긴 본문이나 제어문자를 이름으로 주입하지 못하게
                # 사람 이름으로 충분한 짧은 문자열만 허용한다.
                if len(matched_text) > 100 or any(
                    character in matched_text for character in ("\r", "\n", "\x00")
                ):
                    continue
                normalized_mask = mask_word.upper()
                previous = replacements.get(normalized_mask)
                if previous is not None and previous != matched_text:
                    conflicts.add(normalized_mask)
                    continue
                replacements[normalized_mask] = matched_text

    for conflict in conflicts:
        replacements.pop(conflict, None)

    restored = processed_content
    restored_count = 0
    # 숫자 자릿수가 다른 토큰이 생겨도 긴 토큰부터 처리해 부분 일치를 막는다.
    for mask_word in sorted(replacements, key=len, reverse=True):
        token_pattern = re.compile(
            rf"(?<![A-Za-z0-9_])(?:\[{re.escape(mask_word)}\]|"
            rf"{re.escape(mask_word)})(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        restored, count = token_pattern.subn(
            lambda _match, value=replacements[mask_word]: value,
            restored,
        )
        restored_count += count
    return restored, restored_count


def split_period_sentences(buffer: str) -> tuple[list[str], str]:
    """온점(.)까지 완성된 문장과 아직 미완성인 꼬리를 분리한다.

    연속 온점은 한 경계로 묶으며 공백과 줄바꿈은 다음 문장에 남겨, 모든 조각을
    다시 합치면 원문과 동일하게 유지한다.
    """

    sentences: list[str] = []
    start = 0
    index = 0
    while index < len(buffer):
        if buffer[index] != ".":
            index += 1
            continue
        end = index + 1
        while end < len(buffer) and buffer[end] == ".":
            end += 1
        sentence = buffer[start:end]
        if sentence:
            sentences.append(sentence)
        start = end
        index = end
    return sentences, buffer[start:]


def _result_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("input_results", "output_results", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def create_guardrail_client(settings: Settings) -> GuardrailClient:
    client = BastionGuardianClient(
        base_url=settings.guardrail_base_url,
        endpoint_path=settings.guardrail_endpoint_path,
        api_key=settings.guardrail_api_key,
        timeout_seconds=settings.guardrail_timeout_seconds,
        fail_open=settings.guardrail_fail_open,
    )
    logger.info(
        "======== AI 가드레일 준비 | 상태=%s | endpoint=%s | 장애정책=%s",
        "활성" if client.enabled else "API키 없음/PASS-through",
        settings.guardrail_endpoint_path,
        "fail-open" if settings.guardrail_fail_open else "fail-closed",
    )
    return client
