"""서브에이전트의 다중 시나리오 분류 결과 모델."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SubagentScenarioMatch(BaseModel):
    """한 질문에서 선택된 하나의 시나리오·세부 시나리오 결과."""

    model_config = ConfigDict(extra="forbid")

    scenario_code: str
    scenario_name: str
    detail_scenario_code: str
    detail_scenario_name: str
    # 일반 조회값은 문자열, RAG keywords는 문자열 배열을 사용한다.
    parameters: dict[str, Any]


class SubagentClassificationStatus(StrEnum):
    """세부 시나리오의 실제 지원 여부."""

    MATCHED = "MATCHED"
    UNSUPPORTED = "UNSUPPORTED"


class SubagentResult(BaseModel):
    """한 서브에이전트 안에서 선택된 하나 이상의 시나리오 결과."""

    model_config = ConfigDict(extra="forbid")

    agent_code: str
    prompt_version: str
    status: SubagentClassificationStatus = SubagentClassificationStatus.MATCHED
    scenario_code: str | None = None
    scenario_name: str | None = None
    detail_scenario_code: str | None = None
    detail_scenario_name: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    # 다중 시나리오의 정식 계약이다. 기존 단일 필드는 첫 번째 결과를 나타내므로
    # 기존 프론트와 API 소비자도 그대로 사용할 수 있다.
    matches: list[SubagentScenarioMatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def synchronize_primary_match(self) -> "SubagentResult":
        """정상 매칭과 미지원 결과의 필드 조합을 일관되게 유지한다."""

        if self.status == SubagentClassificationStatus.UNSUPPORTED:
            if self.matches:
                raise ValueError("UNSUPPORTED 결과의 matches는 비어 있어야 합니다.")
            if any(
                value is not None
                for value in (
                    self.scenario_code,
                    self.scenario_name,
                    self.detail_scenario_code,
                    self.detail_scenario_name,
                )
            ):
                raise ValueError("UNSUPPORTED 결과에는 시나리오 필드를 넣을 수 없습니다.")
            self.parameters = {}
            return self

        if not self.matches:
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    self.scenario_code,
                    self.scenario_name,
                    self.detail_scenario_code,
                    self.detail_scenario_name,
                )
            ):
                raise ValueError("MATCHED 결과에는 하나 이상의 세부 시나리오가 필요합니다.")
            self.matches = [
                SubagentScenarioMatch(
                    scenario_code=str(self.scenario_code),
                    scenario_name=str(self.scenario_name),
                    detail_scenario_code=str(self.detail_scenario_code),
                    detail_scenario_name=str(self.detail_scenario_name),
                    parameters=self.parameters,
                )
            ]
        primary = self.matches[0]
        self.scenario_code = primary.scenario_code
        self.scenario_name = primary.scenario_name
        self.detail_scenario_code = primary.detail_scenario_code
        self.detail_scenario_name = primary.detail_scenario_name
        self.parameters = primary.parameters
        return self
