"""FastAPI 요청과 응답에 사용하는 Pydantic 모델."""

from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from app.domain import IntentClassification
from app.mcp.models import McpExecutionResult
from app.subagents.models import SubagentResult


class ChatRequest(BaseModel):
    """신규 질문과 HITL 입력을 하나의 채팅 API로 받는 요청.

    ``thread_id``가 없으면 신규 질문이며 message와 employee_id가 필요하다.
    frontend_agent_code는 사용자가 프론트에서 에이전트를 직접 선택했을 때만
    전달한다. ``thread_id``가 있으면 Redis에 대기 중인 HITL 요청에 대한
    응답이며 hitl_input만 필요하다. 두 형태를 섞어 보내면 잘못된 상태가
    만들어질 수 있으므로 모델 검증 단계에서 거절한다.
    """

    thread_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="HITL 상태를 이어 갈 때 이전 응답에서 받은 thread_id",
    )
    message: str | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
    )
    employee_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="사원번호 또는 사원을 고유하게 식별하는 값",
    )
    frontend_agent_code: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "프론트에서 선택한 에이전트 코드. 미선택이면 생략, null 또는 빈 문자열"
        ),
    )
    conversation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    hitl_input: JsonValue | None = Field(
        default=None,
        description=(
            "HITL 승인 또는 향후 MCP 파라미터 입력에 사용하는 JSON 값"
        ),
    )

    @field_validator("frontend_agent_code", mode="before")
    @classmethod
    def normalize_optional_frontend_agent_code(cls, value):
        """미선택을 나타내는 null·빈 문자열·공백 문자열을 모두 None으로 통일한다."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_request_mode(self) -> "ChatRequest":
        """thread_id 존재 여부에 따라 신규·재진입 요청 필드를 검증한다."""

        if self.thread_id is not None:
            if self.hitl_input is None:
                raise ValueError(
                    "thread_id가 있는 요청에는 hitl_input이 필요합니다."
                )
            supplied_new_fields = {
                "message": self.message,
                "employee_id": self.employee_id,
                "frontend_agent_code": self.frontend_agent_code,
                "conversation_id": self.conversation_id,
            }
            mixed_fields = [
                name
                for name, value in supplied_new_fields.items()
                if value is not None
            ]
            if mixed_fields:
                raise ValueError(
                    "HITL 재진입 요청에는 신규 질문 필드를 함께 보낼 수 없습니다: "
                    + ", ".join(mixed_fields)
                )
            return self

        missing = [
            name
            for name, value in {
                "message": self.message,
                "employee_id": self.employee_id,
            }.items()
            if value is None
        ]
        if missing:
            raise ValueError(
                "신규 채팅 요청에 필수 필드가 없습니다: "
                + ", ".join(missing)
            )
        if self.hitl_input is not None:
            raise ValueError(
                "신규 채팅 요청에는 hitl_input을 사용할 수 없습니다."
            )
        return self

    @property
    def is_hitl_continuation(self) -> bool:
        """Redis HITL 상태를 이어 가는 요청인지 반환한다."""

        return self.thread_id is not None


class ChatResponse(BaseModel):
    """마스터 분류와 등록된 시나리오 서브에이전트의 결과."""

    status: Literal["PASS", "INPUT_REQUIRED", "EXCEPTION"]
    thread_id: str
    classification: IntentClassification
    # 마스터 agent_code에 구현된 서브에이전트가 없거나 HITL 대기 중이면 null이다.
    subagent: SubagentResult | None = None
    # 세부 시나리오 manifest에 등록된 MCP 도구와 추적 ID, 조회 결과이다.
    mcp: McpExecutionResult | None = None
    # 기존 프론트 계약과의 호환성을 위해 필드명은 interrupt를 유지한다.
    # 실제 구현은 LangGraph interrupt()가 아니며, 에이전트 승인·MCP 파라미터
    # 입력·잘못된 값 재입력 등을 표현하는 Redis 기반 공통 입력 요청이다.
    interrupt: dict[str, JsonValue] | None = None
