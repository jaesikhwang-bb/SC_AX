"""서브에이전트가 공통으로 사용하는 결과 모델."""

from pydantic import BaseModel, ConfigDict


class SubagentResult(BaseModel):
    """마스터 분류 이후 서브에이전트가 반환하는 공통 시나리오 결과."""

    model_config = ConfigDict(extra="forbid")

    agent_code: str
    prompt_version: str
    scenario_code: str
    scenario_name: str
    detail_scenario_code: str
    detail_scenario_name: str
    # 각 서브에이전트 manifest가 정의한 파라미터를 동일한 응답 필드로 제공한다.
    parameters: dict[str, str | None]
