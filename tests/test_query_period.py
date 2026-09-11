import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.classifier import GenOSIntentClassifier
from app.csv_trace import EmptyTraceRecorder
from app.domain import ClassificationType, QueryRefinement, QueryRefinementStatus
from app.graph import MasterIntentGraph
from app.history import InMemoryChatHistoryStore
from app.mcp.scenarios.helpers import month_value
from app.query_period import active_month, month_parameter
from app.subagents.prompt_loader import SubagentPromptLoader
from app.subagents.router import ScenarioSubagent, _create_output_model


def classifier_for(message, inherit=True):
    classifier = GenOSIntentClassifier.__new__(GenOSIntentClassifier)
    classifier._refinement_chain = AsyncMock()
    classifier._refinement_chain.ainvoke.return_value = QueryRefinement(
        refined_query=message, status=QueryRefinementStatus.COMPLETE,
        used_history=False, inherit_active_period=inherit,
    )
    classifier._classification_chain = AsyncMock()
    classifier._classification_chain.ainvoke.return_value = SimpleNamespace(
        classification_type=ClassificationType.AGENT,
        agent_code=SimpleNamespace(value="PERFORMANCE_FEE"),
    )
    return classifier


class PeriodTests(unittest.IsolatedAsyncioTestCase):
    def test_latest_user_month_only_and_ambiguous_period_barrier(self):
        history = [{"role": "user", "content": "7월 실적 알려줘"}]
        self.assertEqual(active_month(history + [
            {"role": "assistant", "content": "9월 기본값 안내"},
            {"role": "user", "content": "점수반영 안된거 있는거같은데?"},
        ]), "7월")
        for text, expected in (
            ("9월로 조회해줘", "9월"), ("2025년 7월 조회", "2025년 7월"),
            ("202607 실적", "2026년 7월"), ("전체 기간 조회", None),
            ("이번 달 조회", None), ("7월과 9월 비교", None),
            ("2025년 내역", None), ("7월 15일 내역", None),
            ("7월부터 조회", None), ("최근 3개월", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(active_month(history + [{"role": "user", "content": text}]), expected)

    async def test_period_survives_saved_turns_and_changes_to_september(self):
        store = InMemoryChatHistoryStore()
        graph = MasterIntentGraph.__new__(MasterIntentGraph)
        graph._history_store = store
        graph._history_limit = 10
        graph._trace_recorder = EmptyTraceRecorder()
        await store.append_message(employee_id="e", session_id="s", agent_code="PERFORMANCE_FEE",
                                   role="user", content="7월 실적 알려줘", message_id="initial")
        turns = [("점수반영 안된거 있는거같은데?", "7월"),
                 ("누락 내역 보여줘", "7월"), ("수수료 내역 보여줘", "7월"),
                 ("9월로 조회해줘", "9월"), ("점수반영 안된 내역 보여줘", "9월")]
        for index, (message, expected) in enumerate(turns):
            history = await store.get_recent("e", "s", "PERFORMANCE_FEE", 10)
            classifier = classifier_for(message)
            result = await classifier.classify(message, history, "HISTORY_ALLOWED")
            self.assertIn(expected, result.refined_query)
            self.assertEqual(classifier._classification_chain.ainvoke.call_args.args[0]["message"],
                             result.refined_query)
            await graph._persist_user_message({
                "classification": result.model_dump(mode="json"),
                "employee_id": "e", "session_id": "s", "message_id": str(index),
            })
        self.assertEqual(active_month(await store.get_recent("e", "s", "PERFORMANCE_FEE", 10)), "9월")
        self.assertEqual(await store.get_recent("e", "s", "RP", 10), [])

    async def test_explicit_period_and_non_period_questions_are_not_overwritten(self):
        history = [{"role": "user", "content": "7월 실적 알려줘"}]
        for message, inherit in (("9월 실적", True), ("이번 달 실적", True),
                                 ("전체 기간 실적", True), ("수수료 계산 방법", False),
                                 ("내일 날씨는?", False)):
            result = await classifier_for(message, inherit).classify(message, history, "HISTORY_ALLOWED")
            self.assertEqual(result.refined_query, message)
        result = await classifier_for("누락 내역 보여줘").classify("누락 내역 보여줘", [], "CURRENT_ONLY")
        self.assertEqual(result.refined_query, "누락 내역 보여줘")

    async def test_invented_default_month_is_replaced_with_user_period(self):
        result = await classifier_for("9월 누락 내역 보여줘").classify(
            "누락 내역 보여줘", [{"role": "user", "content": "7월 실적"}], "HISTORY_ALLOWED"
        )
        self.assertEqual(result.refined_query, "7월 누락 내역 보여줘")

    async def test_explicit_month_beats_subagent_default_and_reaches_payload(self):
        bundle = SubagentPromptLoader().load_one(directory="performance-fee")
        scenario, detail = next((scenario, detail) for scenario in bundle.manifest["scenarios"]
                                for detail in scenario["details"] if detail["name"] == "환산 미반영 내역 조회")
        for value in (None, "202609"):
            agent = ScenarioSubagent.__new__(ScenarioSubagent)
            agent._bundle = bundle
            agent._output_model = _create_output_model(bundle)
            agent._detail_by_code = {detail["code"]: (scenario, detail)}
            agent._chain = AsyncMock()
            agent._chain.ainvoke.return_value = SimpleNamespace(matches=[SimpleNamespace(
                scenario_code=scenario["code"], detail_scenario_code=detail["code"],
                parameters=SimpleNamespace(model_dump=lambda: {"closing_year_month": value}),
            )])
            result = await agent.classify("7월 점수반영 안된거 있는거같은데?", today=date(2026, 9, 10))
            self.assertEqual(result.parameters["closing_year_month"], "202607")
            self.assertEqual(month_value(result.parameters, today=date(2026, 9, 10), default_month="CURRENT"),
                             "202607")

    def test_month_parameter_does_not_collapse_ranges_or_relative_dates(self):
        for query in ("7월과 9월", "7월 15일", "이번 달", "7월부터", "최근 3개월"):
            self.assertIsNone(month_parameter(query, date(2026, 9, 10)))
        self.assertEqual(month_parameter("2025년 7월 실적", date(2026, 9, 10)), "202507")
