"""세부 시나리오별 수동 추천질문을 프론트 출력 형식으로 변환한다."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from app.observability import logger
from app.subagents.models import SubagentResult
from app.subagents.prompt_loader import ScenarioPromptBundle, SubagentPromptLoader


@dataclass(frozen=True)
class RecommendedQuestionDefinition:
    """manifest에 고정 등록된 추천질문 한 건과 추적용 시나리오 정보."""

    agent_code: str
    prompt_version: str
    scenario_code: str
    detail_scenario_code: str
    question: str
    order: int

    def to_event_item(self) -> dict[str, Any]:
        """프론트에 전달할 camelCase 추천질문 객체를 반환한다."""

        item: dict[str, Any] = {
            "id": (f"{self.agent_code}:{self.detail_scenario_code}:{self.order}"),
            "question": self.question,
            "interactionType": "prompt",
            "agentCode": self.agent_code,
            "promptVersion": self.prompt_version,
            "scenarioCode": self.scenario_code,
            "detailScenarioCode": self.detail_scenario_code,
        }
        return item


class RecommendedQuestionRegistry:
    """활성 manifest의 모든 세부 시나리오와 추천질문을 시작 시 검증한다.

    추천질문은 선택사항이며 개수의 상한도 두지 않는다. 필드가 없거나 빈
    배열이면 해당 detail은 추천질문을 보내지 않는다. 유효한 질문만 정규화하며
    YAML 배열 순서가 프론트 표시 순서다.
    """

    def __init__(
        self,
        definitions: Mapping[
            tuple[str, str],
            tuple[RecommendedQuestionDefinition, ...],
        ],
    ) -> None:
        self._definitions = MappingProxyType(dict(definitions))

    @classmethod
    def from_bundles(
        cls,
        bundles: Mapping[str, ScenarioPromptBundle],
    ) -> "RecommendedQuestionRegistry":
        definitions: dict[
            tuple[str, str],
            tuple[RecommendedQuestionDefinition, ...],
        ] = {}

        for bundle in bundles.values():
            agent_code = bundle.agent_code.upper()
            for scenario in bundle.manifest["scenarios"]:
                scenario_code = str(scenario["code"])
                for detail in scenario["details"]:
                    detail_code = str(detail["code"])
                    raw_questions = detail.get("recommended_questions")
                    location = f"{agent_code}.{scenario_code}.{detail_code}"
                    key = (agent_code, detail_code)
                    if key in definitions:
                        raise ValueError(
                            "같은 에이전트 안에 중복 세부 시나리오 코드가 있습니다: "
                            f"{location}"
                        )

                    if raw_questions is None:
                        raw_questions = []
                    elif isinstance(raw_questions, str):
                        # 운영 중 실수로 단일 문자열을 넣어도 서버 시작을 막지
                        # 않고 질문 한 개짜리 배열로 정규화한다.
                        raw_questions = [raw_questions]
                    elif not isinstance(raw_questions, list):
                        logger.warning(
                            "!!!!!!!! 추천질문 설정 무시 | 위치=%s | 타입=%s | "
                            "이유=문자열 또는 배열 아님",
                            location,
                            type(raw_questions).__name__,
                        )
                        raw_questions = []

                    questions: list[str] = []
                    for index, raw_question in enumerate(raw_questions, start=1):
                        if isinstance(raw_question, Mapping):
                            raw_question = raw_question.get("question")
                        if (
                            not isinstance(raw_question, str)
                            or not raw_question.strip()
                        ):
                            logger.warning(
                                "!!!!!!!! 추천질문 항목 무시 | 위치=%s[%d] | "
                                "타입=%s | 이유=비어 있지 않은 문자열 아님",
                                location,
                                index,
                                type(raw_question).__name__,
                            )
                            continue
                        normalized_question = raw_question.strip()
                        if normalized_question in questions:
                            logger.warning(
                                "!!!!!!!! 중복 추천질문 무시 | 위치=%s[%d]",
                                location,
                                index,
                            )
                            continue
                        questions.append(normalized_question)

                    definitions[key] = tuple(
                        RecommendedQuestionDefinition(
                            agent_code=agent_code,
                            prompt_version=bundle.version,
                            scenario_code=scenario_code,
                            detail_scenario_code=detail_code,
                            question=question,
                            order=index,
                        )
                        for index, question in enumerate(questions, start=1)
                    )

        return cls(definitions)

    def for_subagent(
        self,
        subagent: SubagentResult | None,
    ) -> list[dict[str, Any]]:
        """선택된 세부 시나리오 순서대로 모든 수동 추천질문을 반환한다."""

        if subagent is None:
            return []

        items: list[dict[str, Any]] = []
        agent_code = subagent.agent_code.upper()
        for match in subagent.matches:
            key = (agent_code, match.detail_scenario_code)
            for definition in self._definitions.get(key, ()):
                items.append(definition.to_event_item())
        return items


def create_recommended_question_registry() -> RecommendedQuestionRegistry:
    """활성 서브에이전트 manifest에서 추천질문 registry를 생성한다."""

    return RecommendedQuestionRegistry.from_bundles(SubagentPromptLoader().load_all())
