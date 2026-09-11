"""세부 시나리오별 프론트 action을 Python 함수로 구성하는 실행 계층.

활성 업무의 action code, 안내문, 입력 필드, 검증과 parameter 반영 규칙은
manifest/YAML에 두지 않는다. 각 ``app/mcp/scenarios/*.py`` 파일이 이 모듈의
정의를 등록하고 MCP handler 안에서 ``require()``를 호출한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.hitl import register_hitl_input_guardrail_policy, validate_input_value


ActionValueParser = Callable[[Any], tuple[str, str | None]]


@dataclass(frozen=True)
class ScenarioActionInput:
    """action 한 필드의 화면 계약과 서버 검증 규칙."""

    parameter_name: str
    input_code: str
    label: str
    input_type: str = "text"
    # 동일 input_code로 여러 입력칸을 만들 때 프론트 표시와 서버 복원 순서를
    # 구분한다. order가 없는 기존 단일 입력 action은 종전과 동일하게 동작한다.
    order: int | None = None
    expected_value: str | None = None
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    allowed_values: tuple[str, ...] | None = None
    validation_message: str | None = None
    sensitive: bool = False
    # False이면 이 필드의 humanInput 값은 INPUT 가드레일의 MASK/BLOCK 처리를
    # 거치지 않고 원문 그대로 MCP 파라미터 검증 단계로 전달된다.
    # MCP 조회키처럼 한 글자라도 바뀌면 안 되는 값에서만 명시적으로 끈다.
    guardrail_enabled: bool = True
    # 표준 문자열 검증만으로 부족한 업무는 함수 하나로 정규화와 오류 문구를
    # 동시에 결정한다. 반환값은 ``(저장할 문자열, 오류 또는 None)``이다.
    value_parser: ActionValueParser | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("parameter_name", self.parameter_name),
            ("input_code", self.input_code),
            ("label", self.label),
            ("input_type", self.input_type),
        ):
            if not str(value).strip():
                raise ValueError(f"ScenarioActionInput.{name}은 비어 있을 수 없습니다.")
        if self.pattern is not None:
            re.compile(self.pattern)
        if self.order is not None and self.order < 1:
            raise ValueError("action order는 1 이상이어야 합니다.")
        if self.min_length is not None and self.min_length < 1:
            raise ValueError("action min_length는 1 이상이어야 합니다.")
        if self.max_length is not None and self.max_length < 1:
            raise ValueError("action max_length는 1 이상이어야 합니다.")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("action min_length는 max_length보다 클 수 없습니다.")
        if self.allowed_values is not None:
            normalized = tuple(str(value).strip() for value in self.allowed_values)
            if any(not value for value in normalized):
                raise ValueError("action allowed_values에는 빈 값을 사용할 수 없습니다.")
            if len(normalized) != len(set(normalized)):
                raise ValueError("action allowed_values에는 중복 값을 사용할 수 없습니다.")
            object.__setattr__(self, "allowed_values", normalized)

    def validate(self, raw_value: Any) -> tuple[str, str | None]:
        """한 값을 업무 함수 또는 공통 문자열 규칙으로 검증한다."""

        if self.value_parser is not None:
            normalized, error = self.value_parser(raw_value)
            return str(normalized), str(error) if error else None
        normalized, errors = validate_input_value(
            {self.input_code: raw_value},
            input_code=self.input_code,
            expected_value=self.expected_value,
            pattern=self.pattern,
            min_length=self.min_length,
            max_length=self.max_length,
            allowed_values=(
                list(self.allowed_values)
                if self.allowed_values is not None
                else None
            ),
            validation_message=self.validation_message,
        )
        return normalized, errors.get(self.input_code)

    def frontend_field(self) -> dict[str, Any]:
        """내부 action 필드를 ``build_action_event`` 입력 형태로 만든다."""

        field = {
            "name": self.input_code,
            "label": self.label,
            "type": self.input_type,
            "required": True,
            "expected_value": self.expected_value,
            "pattern": self.pattern,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "allowed_values": (
                list(self.allowed_values)
                if self.allowed_values is not None
                else None
            ),
            "sensitive": self.sensitive,
        }
        if self.order is not None:
            field["order"] = self.order
        return field


@dataclass(frozen=True)
class ScenarioActionDefinition:
    """한 세부 시나리오에서 한 번에 프론트로 보낼 action 정의."""

    agent_code: str
    detail_scenario_code: str
    action_code: str
    message: str
    inputs: tuple[ScenarioActionInput, ...]
    # 이 action 입력으로 선행 조회 조건이 바뀌면 해당 step 체크포인트만 버린다.
    # 비어 있으면 action 뒤에서도 이전 MCP 결과를 그대로 재사용한다.
    invalidate_step_codes: tuple[str, ...] = ()
    # 값이 이 목록에 해당하면 MCP 재호출 없이 HITL을 종료한다. 페이지 조회의
    # "아니오"처럼 확인 창을 닫기만 하는 action에서 사용한다.
    complete_on_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("agent_code", self.agent_code),
            ("detail_scenario_code", self.detail_scenario_code),
            ("action_code", self.action_code),
            ("message", self.message),
        ):
            if not str(value).strip():
                raise ValueError(f"ScenarioActionDefinition.{name}은 비어 있을 수 없습니다.")
        if not self.inputs:
            raise ValueError("ScenarioActionDefinition.inputs는 한 개 이상이어야 합니다.")
        parameter_names = [item.parameter_name for item in self.inputs]
        input_keys = [(item.input_code, item.order) for item in self.inputs]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("한 action 안의 parameter_name은 중복될 수 없습니다.")
        if len(input_keys) != len(set(input_keys)):
            raise ValueError(
                "한 action 안의 input_code와 order 조합은 중복될 수 없습니다."
            )
        normalized_steps = tuple(
            str(step_code).strip() for step_code in self.invalidate_step_codes
        )
        if any(not step_code for step_code in normalized_steps):
            raise ValueError("invalidate_step_codes에는 빈 값을 사용할 수 없습니다.")
        if len(normalized_steps) != len(set(normalized_steps)):
            raise ValueError("invalidate_step_codes에는 중복 값을 사용할 수 없습니다.")
        object.__setattr__(self, "invalidate_step_codes", normalized_steps)
        object.__setattr__(
            self,
            "complete_on_values",
            tuple(str(value).strip().casefold() for value in self.complete_on_values),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.agent_code.strip().upper(),
            self.detail_scenario_code.strip(),
            self.action_code.strip(),
        )

    def require(self, parameters: Mapping[str, Any]) -> dict[str, str]:
        """모든 값이 유효하면 정규화해 반환하고 아니면 action을 요청한다."""

        normalized: dict[str, str] = {}
        errors: dict[str, str] = {}
        for definition in self.inputs:
            value, error = definition.validate(
                parameters.get(definition.parameter_name)
            )
            normalized[definition.parameter_name] = value
            if error:
                error_code = (
                    f"{definition.input_code}:{definition.order}"
                    if definition.order is not None
                    else definition.input_code
                )
                errors[error_code] = error
        if errors:
            raise ScenarioActionRequired(self, errors)
        return normalized

    def request(
        self,
        *,
        message: str | None = None,
        errors: Mapping[str, str] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """현재 parameter가 있어도 MCP 결과에 따라 action을 강제로 요청한다.

        NO_DATA·ERROR 응답 뒤 검색조건을 다시 받거나, 성공 결과를 확인한 뒤 다음
        선택을 받아야 할 때 handler가 호출한다. 동적 message/error는 interrupt에
        저장되며 외부에는 최소 action 필드만 전달된다.
        """

        normalized_message = str(message or self.message).strip()
        if not normalized_message:
            raise ValueError("동적 action message는 비어 있을 수 없습니다.")
        normalized_errors = {
            str(code): str(error)
            for code, error in dict(errors or {}).items()
            if str(code).strip() and str(error).strip()
        }
        raise ScenarioActionRequired(
            self,
            normalized_errors,
            message=normalized_message,
            context=context,
        )

    def validate_submission(
        self,
        human_input: Any,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """프론트 ``humanInput``을 parameter 갱신값과 오류로 변환한다."""

        if not isinstance(human_input, Mapping):
            return {}, {
                definition.input_code: "응답은 JSON 객체여야 합니다."
                for definition in self.inputs
            }
        normalized: dict[str, str] = {}
        errors: dict[str, str] = {}
        for definition in self.inputs:
            raw_value = human_input.get(definition.input_code)
            if definition.order is not None:
                raw_value = _ordered_input_value(raw_value, definition.order)
            value, error = definition.validate(raw_value)
            normalized[definition.parameter_name] = value
            if error:
                error_code = (
                    f"{definition.input_code}:{definition.order}"
                    if definition.order is not None
                    else definition.input_code
                )
                errors[error_code] = error
        return normalized, errors

    def frontend_fields(self) -> list[dict[str, Any]]:
        definitions = sorted(
            self.inputs,
            key=lambda item: (
                item.order is None,
                item.order if item.order is not None else 0,
            ),
        )
        return [definition.frontend_field() for definition in definitions]


def _ordered_input_value(raw_value: Any, expected_order: int) -> Any:
    """같은 code의 입력 배열에서 지정된 order의 실제 input만 반환한다."""

    if not isinstance(raw_value, list):
        return None
    for item in raw_value:
        if not isinstance(item, Mapping):
            continue
        try:
            item_order = int(item.get("order"))
        except (TypeError, ValueError):
            continue
        if item_order == expected_order:
            return item.get("input")
    return None


class ScenarioActionRequired(ValueError):
    """Python 시나리오 함수가 요청한 1~N개 action 입력."""

    def __init__(
        self,
        definition: ScenarioActionDefinition,
        errors: Mapping[str, str],
        *,
        message: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        first = definition.inputs[0]
        action_message = str(message or definition.message).strip()
        self.input_code = first.input_code
        self.parameter_name = first.parameter_name
        self.label = first.label
        self.message = action_message
        self.input_type = first.input_type
        self.expected_value = first.expected_value
        self.pattern = first.pattern
        self.min_length = first.min_length
        self.max_length = first.max_length
        self.allowed_values = (
            list(first.allowed_values) if first.allowed_values is not None else None
        )
        self.validation_message = first.validation_message
        self.sensitive = first.sensitive
        first_error_code = (
            f"{first.input_code}:{first.order}"
            if first.order is not None
            else first.input_code
        )
        self.initial_error = errors.get(first_error_code)
        self.definition = definition
        self.action_code = definition.action_code
        self.errors = dict(errors)
        # handler가 API/graph에 알려야 하는 최소 상태다. 주소를 일반 message로
        # 받는 것처럼 표준 humanInput 재개만으로 표현할 수 없는 경우에만 쓴다.
        self.context = dict(context or {})
        super().__init__(action_message)


_SCENARIO_ACTION_REGISTRY: dict[
    tuple[str, str, str],
    ScenarioActionDefinition,
] = {}


def register_scenario_action(
    definition: ScenarioActionDefinition,
) -> ScenarioActionDefinition:
    """세부 시나리오 action을 등록하고 handler에서 쓸 동일 객체를 반환한다."""

    if definition.key in _SCENARIO_ACTION_REGISTRY:
        raise ValueError(f"중복 세부 시나리오 action입니다: {definition.key}")
    for action_input in definition.inputs:
        register_hitl_input_guardrail_policy(
            action_input.input_code,
            enabled=action_input.guardrail_enabled,
        )
    _SCENARIO_ACTION_REGISTRY[definition.key] = definition
    return definition


def get_scenario_action(
    agent_code: str,
    detail_scenario_code: str,
    action_code: str,
) -> ScenarioActionDefinition | None:
    """Redis 재진입 시 직렬화된 code로 Python action 함수를 복원한다."""

    return _SCENARIO_ACTION_REGISTRY.get(
        (
            str(agent_code).strip().upper(),
            str(detail_scenario_code).strip(),
            str(action_code).strip(),
        )
    )


def redact_scenario_action_parameters(
    agent_code: str,
    detail_scenario_code: str,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """등록된 action의 민감 parameter를 로그·tester 출력에서 마스킹한다."""

    redacted = dict(parameters)
    prefix = (
        str(agent_code).strip().upper(),
        str(detail_scenario_code).strip(),
    )
    for key, definition in _SCENARIO_ACTION_REGISTRY.items():
        if key[:2] != prefix:
            continue
        for input_definition in definition.inputs:
            if (
                input_definition.sensitive
                and redacted.get(input_definition.parameter_name)
            ):
                redacted[input_definition.parameter_name] = "***MASKED***"
    return redacted
