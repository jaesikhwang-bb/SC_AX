"""GenOS·Redis 없이 SSE 고정답변/RAG/HITL 화면을 확인하는 개발 서버."""

import os
import uvicorn

from app.api import create_app
from app.config import Settings
from app.domain import ClassificationType, IntentClassification
from app.history import InMemoryChatHistoryStore
from app.hitl_store import InMemoryHitlStateStore
from app.subagents.models import SubagentResult
from app.subagents.router import EmptySubagentRouter


class MockMasterClassifier:
    """질문 표현으로 테스트할 응답 모드만 고르는 로컬 전용 분류기."""

    async def classify(self, message, history, frontend_agent_code=None):
        if "날씨" in message or "주식" in message:
            return IntentClassification(
                refined_query=message,
                classification_type=ClassificationType.OUT_OF_SCOPE,
                agent_code=None,
            )
        agent_code = "QUALIFICATION" if "자격" in message or "서류" in message else "PERFORMANCE_FEE"
        return IntentClassification(
            refined_query=message,
            classification_type=ClassificationType.AGENT,
            agent_code=agent_code,
        )


class MockScenarioRouter(EmptySubagentRouter):
    def supports(self, agent_code: str) -> bool:
        return agent_code in {"PERFORMANCE_FEE", "QUALIFICATION"}

    def registered_codes(self) -> tuple[str, ...]:
        return ("PERFORMANCE_FEE", "QUALIFICATION")

    async def classify(self, *, agent_code: str, query: str):
        if agent_code == "QUALIFICATION":
            return SubagentResult(
                agent_code=agent_code,
                prompt_version="mock-v1",
                scenario_code="PERSONAL_MEMBER_QUALIFICATION",
                scenario_name="개인회원 입회 자격기준",
                detail_scenario_code="NEW_MEMBER_QUALIFICATION",
                detail_scenario_name="신규회원 입회 자격기준",
                parameters={
                    "member_category": "NEW",
                    "restricted_request_type": None,
                },
            )
        return SubagentResult(
            agent_code=agent_code,
            prompt_version="mock-v1",
            scenario_code="PERFORMANCE_SUMMARY",
            scenario_name="실적 종합조회",
            detail_scenario_code="PERFORMANCE_SUMMARY_TOTAL",
            detail_scenario_name="실적 종합 조회",
            parameters={
                "closing_year_month": "202608",
                "reference_date": None,
                "reference_year": None,
            },
        )


settings = Settings(
    genos_url="https://genos.genon.ai",
    genos_serving_id=850,
    genos_model="mock-model",
    genos_bearer_token=None,
    prompt_version=None,
    history_backend="memory",
    redis_url="redis://localhost:6379/0",
    redis_history_key_prefix="chat:history",
    history_limit=10,
    redis_dedupe_ttl_seconds=3600,
    project_code="acqsc",
    hitl_state_backend="memory",
    mcp_backend="mock",
    csv_trace_enabled=False,
)

app = create_app(
    settings=settings,
    classifier=MockMasterClassifier(),
    history_store=InMemoryChatHistoryStore(project_code="acqsc"),
    hitl_store=InMemoryHitlStateStore(project_code="acqsc"),
    subagent_router=MockScenarioRouter(),
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.getenv("MOCK_STREAM_PORT", "8010")),
    )
