"""LLM, LangGraph, FastAPI가 함께 사용하는 Pydantic 도메인 모델."""

from enum import Enum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClassificationType(StrEnum):
    """마스터 프롬프트가 반환할 수 있는 최상위 분류 유형."""

    AGENT = "AGENT"
    # 현재 질문만으로 대상 또는 조건을 확정할 수 있어 같은 에이전트 범위의
    # 대화이력이 필요함을 뜻한다. 최종 응답 유형이 아니라 2차 분류로 보내는
    # LangGraph 내부 라우팅 유형이다.
    CONTEXT_REQUIRED = "CONTEXT_REQUIRED"
    EMPTY_QUERY = "EMPTY_QUERY"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class QueryRefinementStatus(StrEnum):
    """질문 보정기가 원문의 문맥을 확정했는지 나타낸다."""

    COMPLETE = "COMPLETE"
    CONTEXT_REQUIRED = "CONTEXT_REQUIRED"
    UNRESOLVED = "UNRESOLVED"


class QueryRefinement(BaseModel):
    """의도분류 전에 실행되는 의미 보존 질문 보정 결과."""

    model_config = ConfigDict(extra="forbid")

    refined_query: str
    status: QueryRefinementStatus
    used_history: bool
    inherit_active_period: bool = Field(
        description=(
            "현재 질문이 같은 업무의 기간별 실제 값·누락·반영 내역 조회이고 새 기간이 "
            "없으면 true. 문장이 완결돼도 활성 조회 월을 유지한다. 설명·기준·예외·"
            "기간 무관 질문이나 의미 미확정은 false."
        )
    )

    @model_validator(mode="after")
    def validate_refinement(self) -> "QueryRefinement":
        if not self.refined_query.strip():
            raise ValueError("refined_query는 비어 있을 수 없습니다.")
        if self.status != QueryRefinementStatus.COMPLETE and self.used_history:
            raise ValueError("문맥을 확정하지 못한 결과는 used_history=true일 수 없습니다.")
        return self


class IntentClassification(BaseModel):
    """그래프와 API가 사용하는 공급자 독립적인 의도분류 결과."""

    # 예상하지 못한 필드를 허용하지 않아 스키마 변경을 명시적으로 관리한다.
    model_config = ConfigDict(extra="forbid")

    refined_query: str
    classification_type: ClassificationType
    agent_code: str | None

    @model_validator(mode="after")
    def validate_result(self) -> "IntentClassification":
        """분류 유형과 에이전트 코드의 조합이 유효한지 확인한다."""

        if not self.refined_query.strip():
            raise ValueError("refined_query는 비어 있을 수 없습니다.")
        if self.classification_type == ClassificationType.AGENT:
            if self.agent_code is None:
                raise ValueError("AGENT 분류에는 agent_code가 필요합니다.")
        elif self.agent_code is not None:
            raise ValueError("예외 분류의 agent_code는 null이어야 합니다.")
        return self


def create_structured_output_model(
    agent_codes: tuple[str, ...],
) -> type[BaseModel]:
    """manifest의 에이전트 코드로 엄격한 LLM JSON Schema를 생성한다.

    Pydantic 클래스를 실행 시점에 만들기 때문에 새 프롬프트 버전에 에이전트를
    추가하더라도 Python Enum을 별도로 수정할 필요가 없다.
    """

    # HTTP 계약과 Redis 키가 대문자를 사용하므로 Enum 이름과 값도 통일한다.
    agent_code_enum = Enum(
        "AgentCode",
        {code.upper(): code.upper() for code in agent_codes},
        type=str,
    )

    class StructuredIntentOutput(BaseModel):
        """ChatOpenAI Structured Output으로 요청할 정확한 JSON 구조."""

        model_config = ConfigDict(extra="forbid")

        refined_query: str = Field(
            description=("대화 문맥과 질문의 오타를 반영해 보정한 최종 사용자 질문")
        )
        classification_type: Literal[
            ClassificationType.AGENT,
            ClassificationType.EMPTY_QUERY,
            ClassificationType.OUT_OF_SCOPE,
        ] = Field(
            description=(
                "정상 에이전트 분류, 최종적으로 의미를 확정할 수 없는 질문, "
                "또는 업무 범위 밖 예외"
            )
        )
        agent_code: agent_code_enum | None = Field(
            description=("AGENT 분류이면 선택한 에이전트 코드, 예외 분류이면 null")
        )

        @model_validator(mode="after")
        def validate_agent_code(self):
            """LLM 구조화 응답 내부의 필드 조합을 검증한다."""

            if not self.refined_query.strip():
                raise ValueError("refined_query는 비어 있을 수 없습니다.")
            if self.classification_type == ClassificationType.AGENT:
                if self.agent_code is None:
                    raise ValueError("AGENT 분류에는 agent_code가 필요합니다.")
            elif self.agent_code is not None:
                raise ValueError("예외 분류의 agent_code는 null이어야 합니다.")
            return self

    # 동적 클래스여도 공급자에게 전달되는 스키마 제목은 항상 동일하게 유지한다.
    StructuredIntentOutput.__name__ = "IntentClassificationOutput"
    return StructuredIntentOutput
