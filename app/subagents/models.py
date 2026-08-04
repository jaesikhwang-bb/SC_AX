"""서브에이전트가 공통으로 사용하는 결과 모델."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SubagentScenarioMatch(BaseModel):
    """한 질문에서 선택된 하나의 시나리오·세부 시나리오 결과."""

    model_config = ConfigDict(extra="forbid")

    scenario_code: str
    scenario_name: str
    detail_scenario_code: str
    detail_scenario_name: str
    parameters: dict[str, str | None]


class SubagentResult(BaseModel):
    """한 서브에이전트 안에서 선택된 하나 이상의 시나리오 결과."""

    model_config = ConfigDict(extra="forbid")

    agent_code: str
    prompt_version: str
    scenario_code: str
    scenario_name: str
    detail_scenario_code: str
    detail_scenario_name: str
    parameters: dict[str, str | None]
    # 다중 시나리오의 정식 계약이다. 기존 단일 필드는 첫 번째 결과를 나타내므로
    # 기존 프론트와 API 소비자도 그대로 사용할 수 있다.
    matches: list[SubagentScenarioMatch] = Field(default_factory=list)

    @model_validator(mode="after")
    def synchronize_primary_match(self) -> "SubagentResult":
        """matches가 없으면 기존 단일 필드를 첫 매칭으로 자동 변환한다."""

        if not self.matches:
            self.matches = [
                SubagentScenarioMatch(
                    scenario_code=self.scenario_code,
                    scenario_name=self.scenario_name,
                    detail_scenario_code=self.detail_scenario_code,
                    detail_scenario_name=self.detail_scenario_name,
                    parameters=self.parameters,
                )
            ]
        return self
