"""PERFORMANCE_FEE 프롬프트, 파라미터 규칙, 그래프 연결을 검증한다."""

import unittest
from datetime import date

from app.domain import ClassificationType, IntentClassification
from app.graph import MasterIntentGraph
from app.history import EmptyChatHistoryStore
from app.hitl_store import InMemoryHitlStateStore
from app.subagents.models import SubagentResult
from app.subagents.prompt_loader import SubagentPromptLoader
from app.subagents.router import (
    ScenarioSubagent,
    _create_output_model,
    _normalize_parameters,
)


class FixedMasterClassifier:
    """마스터가 PERFORMANCE_FEE를 선택한 상황을 만드는 테스트 분류기."""

    async def classify(self, message, history, frontend_agent_code=None):
        return IntentClassification(
            refined_query=message,
            classification_type=ClassificationType.AGENT,
            agent_code="PERFORMANCE_FEE",
        )


class FixedSubagentRouter:
    """LangGraph 연결만 검증하기 위한 고정 서브에이전트 라우터."""

    def __init__(self) -> None:
        self.calls = 0
        self.received_queries: list[str] = []

    def supports(self, agent_code: str) -> bool:
        return agent_code == "PERFORMANCE_FEE"

    def registered_codes(self) -> tuple[str, ...]:
        return ("PERFORMANCE_FEE",)

    async def classify(self, *, agent_code: str, query: str):
        self.calls += 1
        self.received_queries.append(query)
        return SubagentResult(
            agent_code=agent_code,
            prompt_version="v1",
            scenario_code="PERFORMANCE_SUMMARY",
            scenario_name="실적 종합조회",
            detail_scenario_code="PERFORMANCE_SUMMARY_TOTAL",
            detail_scenario_name="실적 종합 조회",
            parameters={
                "closing_year_month": "202607",
                "reference_date": None,
                "reference_year": None,
            },
        )


class FakeStructuredChain:
    """GenOS 네트워크 호출 없이 구조화 응답을 반환하는 가짜 체인."""

    def __init__(self, value) -> None:
        self.value = value

    async def ainvoke(self, payload):
        return self.value


class FailingSubagentRouter(FixedSubagentRouter):
    """서브에이전트 오류가 마스터 API까지 전파되지 않는지 검증한다."""

    async def classify(self, *, agent_code: str, query: str):
        raise ValueError("테스트용 서브에이전트 오류")


class RefinedMasterClassifier:
    """원본과 다른 보정 질문을 반환해 서브에이전트 입력을 검증한다."""

    async def classify(self, message, history, frontend_agent_code=None):
        return IntentClassification(
            refined_query="2026년 7월 내 실적을 조회해줘",
            classification_type=ClassificationType.AGENT,
            agent_code="PERFORMANCE_FEE",
        )


class PerformanceFeeSubagentTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = SubagentPromptLoader().load_all()["PERFORMANCE_FEE"]
        cls.detail_by_code = {
            detail["code"]: detail
            for scenario in cls.bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }

    def test_manifest_contains_all_scenarios_and_details(self) -> None:
        """요청된 5개 시나리오와 11개 세부 시나리오가 모두 있어야 한다."""

        self.assertEqual(5, len(self.bundle.manifest["scenarios"]))
        self.assertEqual(11, len(self.detail_by_code))
        self.assertEqual(
            {
                "closing_year_month",
                "reference_date",
                "reference_year",
            },
            set(self.bundle.manifest["parameter_definitions"]),
        )

    def test_all_default_parameter_policies(self) -> None:
        """당월·전월·전년도 기본값을 모든 세부 시나리오에 적용한다."""

        expected = {
            "PERFORMANCE_SUMMARY_TOTAL": ("202607", None, None),
            "COMPOSITE_CONVERSION_SCORE": ("202607", None, None),
            "COMPOSITE_CONVERSION_EXCLUDED": ("202607", None, None),
            "UNREGISTERED_MEMBER_SUMMARY": ("202607", None, None),
            "UNREGISTERED_MEMBER_HANDOFF_DETAIL": ("202607", None, None),
            "DISPOSAL_FEE_SUMMARY": ("202606", None, None),
            "DISPOSAL_FEE_CUSTOMER_DETAIL": ("202606", None, None),
            "FEE_ITEM_DETAILS": ("202606", None, None),
            "FEE_TAX_NET_PAYMENT": ("202606", None, None),
            "FEE_12_MONTH_TREND": ("202607", None, None),
            "WITHHOLDING_TAX": (None, None, "2025"),
        }

        for detail_code, values in expected.items():
            with self.subTest(detail_code=detail_code):
                normalized = _normalize_parameters(
                    raw={
                        "closing_year_month": None,
                        "reference_date": None,
                        "reference_year": None,
                    },
                    manifest=self.bundle.manifest,
                    detail=self.detail_by_code[detail_code],
                    today=date(2026, 7, 30),
                )
                self.assertEqual(
                    {
                        "closing_year_month": values[0],
                        "reference_date": values[1],
                        "reference_year": values[2],
                    },
                    normalized,
                )

    def test_reference_date_clears_closing_month(self) -> None:
        normalized = _normalize_parameters(
            raw={
                "closing_year_month": "202606",
                "reference_date": "20260715",
                "reference_year": None,
            },
            manifest=self.bundle.manifest,
            detail=self.detail_by_code["PERFORMANCE_SUMMARY_TOTAL"],
            today=date(2026, 7, 30),
        )

        self.assertIsNone(normalized["closing_year_month"])
        self.assertEqual("20260715", normalized["reference_date"])

    async def test_structured_scenario_result(self) -> None:
        """동적 Pydantic 모델 결과가 공통 SubagentResult로 변환돼야 한다."""

        output_model = _create_output_model(self.bundle)
        structured = output_model.model_validate(
            {
                "scenario_code": "FEE_DETAILS",
                "detail_scenario_code": "WITHHOLDING_TAX",
                "parameters": {
                    "closing_year_month": None,
                    "reference_date": None,
                    "reference_year": None,
                },
            }
        )

        # ChatOpenAI 생성 없이 classify의 후처리 로직만 실제 객체에서 실행한다.
        agent = object.__new__(ScenarioSubagent)
        agent._bundle = self.bundle
        agent._output_model = output_model
        agent._scenario_by_code = {
            scenario["code"]: scenario
            for scenario in self.bundle.manifest["scenarios"]
        }
        agent._detail_by_code = {
            detail["code"]: (scenario, detail)
            for scenario in self.bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }
        agent._chain = FakeStructuredChain(structured)

        result = await agent.classify(
            "원천징수 내역을 보여줘",
            today=date(2026, 7, 30),
        )

        self.assertEqual("FEE_DETAILS", result.scenario_code)
        self.assertEqual("WITHHOLDING_TAX", result.detail_scenario_code)
        self.assertEqual("2025", result.parameters["reference_year"])

    async def test_mismatched_parent_scenario_is_automatically_corrected(
        self,
    ) -> None:
        """LLM의 최상위·세부 코드가 어긋나도 세부 코드 기준으로 보정한다."""

        output_model = _create_output_model(self.bundle)
        structured = output_model.model_validate(
            {
                # 첨부 Traceback과 같은 잘못된 부모·자식 조합을 의도적으로 만든다.
                "scenario_code": "PERFORMANCE_SUMMARY",
                "detail_scenario_code": "WITHHOLDING_TAX",
                "parameters": {
                    "closing_year_month": None,
                    "reference_date": None,
                    "reference_year": None,
                },
            }
        )
        agent = object.__new__(ScenarioSubagent)
        agent._bundle = self.bundle
        agent._output_model = output_model
        agent._scenario_by_code = {
            scenario["code"]: scenario
            for scenario in self.bundle.manifest["scenarios"]
        }
        agent._detail_by_code = {
            detail["code"]: (scenario, detail)
            for scenario in self.bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }
        agent._chain = FakeStructuredChain(structured)

        result = await agent.classify(
            "원천징수 내역을 보여줘",
            today=date(2026, 7, 30),
        )

        self.assertEqual("FEE_DETAILS", result.scenario_code)
        self.assertEqual("WITHHOLDING_TAX", result.detail_scenario_code)

    async def test_master_hitl_then_runs_performance_fee_subagent(self) -> None:
        """코드 불일치 승인 후 PERFORMANCE_FEE 서브에이전트를 실행해야 한다."""

        subagent_router = FixedSubagentRouter()
        graph = MasterIntentGraph(
            FixedMasterClassifier(),
            EmptyChatHistoryStore(),
            InMemoryHitlStateStore(),
            subagent_router=subagent_router,
        )

        paused = await graph.start(
            thread_id="performance-fee-hitl",
            employee_id="EMP001",
            conversation_id="conversation-1",
            message="이번 달 실적을 알려줘",
            frontend_agent_code="RP",
        )
        resumed = await graph.resume(
            thread_id="performance-fee-hitl",
            value={"signal": "OK"},
        )

        self.assertEqual("INPUT_REQUIRED", paused.status)
        self.assertIsNone(paused.subagent)
        self.assertEqual("PASS", resumed.status)
        self.assertEqual(1, subagent_router.calls)
        self.assertEqual(
            "PERFORMANCE_SUMMARY_TOTAL",
            resumed.subagent.detail_scenario_code,
        )

    async def test_subagent_receives_master_refined_query(self) -> None:
        """서브에이전트에는 원본이 아니라 마스터의 보정 질문을 전달해야 한다."""

        subagent_router = FixedSubagentRouter()
        graph = MasterIntentGraph(
            RefinedMasterClassifier(),
            EmptyChatHistoryStore(),
            InMemoryHitlStateStore(),
            subagent_router=subagent_router,
        )

        result = await graph.start(
            thread_id="refined-query-to-subagent",
            employee_id="EMP001",
            conversation_id="conversation-1",
            message="칠월 내실적",
            frontend_agent_code="PERFORMANCE_FEE",
        )

        self.assertEqual("PASS", result.status)
        self.assertEqual(
            ["2026년 7월 내 실적을 조회해줘"],
            subagent_router.received_queries,
        )

    async def test_subagent_failure_still_returns_master_result(self) -> None:
        """서브에이전트 내부 오류가 전체 그래프를 500으로 만들지 않아야 한다."""

        graph = MasterIntentGraph(
            FixedMasterClassifier(),
            EmptyChatHistoryStore(),
            InMemoryHitlStateStore(),
            subagent_router=FailingSubagentRouter(),
        )

        result = await graph.start(
            thread_id="subagent-failure",
            employee_id="EMP001",
            conversation_id="conversation-1",
            message="이번 달 실적을 알려줘",
            frontend_agent_code="PERFORMANCE_FEE",
        )

        self.assertEqual("PASS", result.status)
        self.assertEqual(
            "PERFORMANCE_FEE",
            result.classification.agent_code,
        )
        self.assertIsNone(result.subagent)
