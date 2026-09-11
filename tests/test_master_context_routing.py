"""완결 질문과 문맥 의존 질문의 마스터 분류 분기 테스트."""

import unittest
from typing import Any, Literal
from unittest.mock import AsyncMock

from app.csv_trace import EmptyTraceRecorder
from app.domain import ClassificationType, IntentClassification
from app.graph import MasterIntentGraph


class RecordingIntentClassifier:
    """호출 인자와 순서를 검증하기 위한 테스트용 마스터 분류기."""

    def __init__(self, *responses: IntentClassification) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def classify(
        self,
        message: str,
        history: list[dict[str, Any]],
        context_mode: Literal["CURRENT_ONLY", "HISTORY_ALLOWED"] = "CURRENT_ONLY",
    ) -> IntentClassification:
        self.calls.append(
            {
                "message": message,
                "history": list(history),
                "context_mode": context_mode,
            }
        )
        if not self._responses:
            raise AssertionError("예상하지 않은 추가 분류 호출입니다.")
        return self._responses.pop(0)


def _graph(classifier: RecordingIntentClassifier) -> MasterIntentGraph:
    """그래프 전체 컴파일 없이 분류 노드만 검증할 인스턴스를 만든다."""

    graph = MasterIntentGraph.__new__(MasterIntentGraph)
    graph._classifier = classifier
    graph._trace_recorder = EmptyTraceRecorder()
    return graph


class MasterContextRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_graph_finishes_exception_without_retry_or_mcp(self) -> None:
        for history in ([], [{"role": "user", "content": "7월 실적 알려줘"}]):
            with self.subTest(history=history):
                classifier = RecordingIntentClassifier(IntentClassification(
                    refined_query="내일 날씨는?",
                    classification_type=ClassificationType.OUT_OF_SCOPE, agent_code=None,
                ))
                history_store = AsyncMock()
                history_store.get_recent.return_value = history
                mcp = AsyncMock()
                graph = MasterIntentGraph(classifier, history_store, AsyncMock(), mcp_executor=mcp)
                result = await graph._graph.ainvoke({
                    "entry_stage": "NEW_CHAT", "message": "내일 날씨는?",
                    "employee_id": "employee", "session_id": "session",
                    "thread_id": "thread", "frontend_agent_code": "PERFORMANCE_FEE",
                })
                self.assertEqual(result["classification"]["classification_type"], "OUT_OF_SCOPE")
                self.assertEqual(len(classifier.calls), 1)
                mcp.execute.assert_not_called()
                history_store.append_message.assert_not_called()

    def test_unselected_agent_code_requires_switch_action(self) -> None:
        """None·빈 문자열은 자동 PASS하지 않고 분류된 화면 전환을 확인한다."""

        graph = _graph(RecordingIntentClassifier())
        classification = IntentClassification(
            refined_query="7월 실적 알려줘",
            classification_type=ClassificationType.AGENT,
            agent_code="PERFORMANCE_FEE",
        ).model_dump(mode="json")

        for frontend_agent_code in (None, "", "   "):
            with self.subTest(frontend_agent_code=frontend_agent_code):
                update = graph._verify_selection(
                    {
                        "message": "7월 실적 알려줘",
                        "classification": classification,
                        "frontend_agent_code": frontend_agent_code,
                    }
                )
                self.assertEqual(update["status"], "INPUT_REQUIRED")
                self.assertFalse(update["approved"])
                self.assertEqual(update["interrupt"]["action_code"], "SWITCH_AGENT")
                self.assertEqual(
                    update["interrupt"]["context"]["classified_agent_code"],
                    "PERFORMANCE_FEE",
                )

    async def test_complete_query_keeps_agent_selection_with_scoped_history(self) -> None:
        classifier = RecordingIntentClassifier(
            IntentClassification(
                refined_query="원천징수 내역을 알려줘.",
                classification_type=ClassificationType.AGENT,
                agent_code="PERFORMANCE_FEE",
            )
        )
        graph = _graph(classifier)
        state = {
            "message": "원천징수 내역 알려줘",
            "history": [
                {"role": "user", "content": "외국인 입회 자격기준 알려줘"},
                {"role": "assistant", "content": "자격기준 답변"},
            ],
            "frontend_agent_code": "QUALIFICATION",
        }

        update = await graph._classify_intent(state)
        classified_state = {**state, **update}

        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(classifier.calls[0]["context_mode"], "HISTORY_ALLOWED")
        self.assertEqual(classifier.calls[0]["history"], state["history"])
        self.assertEqual(
            graph._after_classification(classified_state),
            "verify_selection",
        )

        # 완결 질문은 이전 자격기준 이력에 끌려가지 않고 실제 업무로 분류되어
        # 기존 SWITCH_AGENT 확인 절차로 진입해야 한다.
        verified = graph._verify_selection(classified_state)
        self.assertEqual(verified["status"], "INPUT_REQUIRED")
        self.assertEqual(verified["interrupt"]["action_code"], "SWITCH_AGENT")
        self.assertEqual(
            verified["interrupt"]["context"]["classified_agent_code"],
            "PERFORMANCE_FEE",
        )

    async def test_followup_uses_history_before_any_classification(self) -> None:
        for message in ("그다음 수수료는?", "수수료는?"):
            with self.subTest(message=message):
                classifier = RecordingIntentClassifier(IntentClassification(
                    refined_query="7월 수수료 알려줘",
                    classification_type=ClassificationType.AGENT,
                    agent_code="PERFORMANCE_FEE",
                ))
                graph = _graph(classifier)
                history = [
                    {"role": "user", "content": "7월 실적 알려줘"},
                    {"role": "assistant", "content": "7월 실적 조회 결과"},
                ]
                state = {"message": message, "history": history}
                update = await graph._classify_intent(state)
                self.assertEqual(classifier.calls, [{
                    "message": message, "history": history,
                    "context_mode": "HISTORY_ALLOWED",
                }])
                self.assertEqual(update["classification"]["refined_query"],
                                 "7월 수수료 알려줘")
                self.assertEqual(graph._after_classification({**state, **update}),
                                 "verify_selection")

    async def test_exception_is_final_only_after_history_review(self) -> None:
        for kind in (ClassificationType.EMPTY_QUERY,
                     ClassificationType.OUT_OF_SCOPE,
                     ClassificationType.CONTEXT_REQUIRED):
            with self.subTest(kind=kind):
                classifier = RecordingIntentClassifier(IntentClassification(
                    refined_query="그건?", classification_type=kind, agent_code=None,
                ))
                graph = _graph(classifier)
                state = {"message": "그건?", "history": [
                    {"role": "user", "content": "관련 없는 질문"},
                ]}
                update = await graph._classify_intent(state)
                self.assertEqual(len(classifier.calls), 1)
                self.assertEqual(classifier.calls[0]["history"], state["history"])
                self.assertEqual(classifier.calls[0]["context_mode"], "HISTORY_ALLOWED")
                self.assertTrue(update["context_classified"])
                self.assertEqual(graph._after_classification({**state, **update}),
                                 "finish_exception")

    async def test_no_history_has_one_final_classification(self) -> None:
        for kind in (ClassificationType.EMPTY_QUERY, ClassificationType.OUT_OF_SCOPE,
                     ClassificationType.CONTEXT_REQUIRED, ClassificationType.AGENT):
            with self.subTest(kind=kind):
                classifier = RecordingIntentClassifier(IntentClassification(
                    refined_query="현재 질문", classification_type=kind,
                    agent_code="RP" if kind == ClassificationType.AGENT else None,
                ))
                graph = _graph(classifier)
                state = {"message": "현재 질문", "history": []}
                update = await graph._classify_intent(state)
                self.assertEqual(classifier.calls, [{
                    "message": "현재 질문", "history": [], "context_mode": "CURRENT_ONLY",
                }])
                self.assertEqual(graph._after_classification({**state, **update}),
                                 "verify_selection" if kind == ClassificationType.AGENT
                                 else "finish_exception")
                if kind == ClassificationType.CONTEXT_REQUIRED:
                    self.assertEqual(update["classification"]["classification_type"], "EMPTY_QUERY")

    async def test_history_scope_never_falls_back_to_another_agent(self) -> None:
        for agent in (None, "", "   ", "RP"):
            with self.subTest(agent=agent):
                graph = _graph(RecordingIntentClassifier())
                graph._history_store = AsyncMock()
                graph._history_store.get_recent.return_value = []
                graph._history_limit = 10
                update = await graph._load_history({
                    "employee_id": "employee", "session_id": "session",
                    "frontend_agent_code": agent,
                    "request_context": {"endpoint": "service"},
                })
                self.assertEqual(update["history"], [])
                graph._history_store.get_recent_for_session.assert_not_called()
                if agent == "RP":
                    graph._history_store.get_recent.assert_awaited_once_with(
                        "employee", "session", "RP", 10,
                        include_metadata=False, project_code="service",
                    )
                else:
                    graph._history_store.get_recent.assert_not_called()

    async def test_llm_failure_is_not_retried_as_business_classification(self) -> None:
        graph = _graph(RecordingIntentClassifier())
        graph._classifier.classify = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        with self.assertRaisesRegex(RuntimeError, "LLM unavailable"):
            await graph._classify_intent({"message": "수수료는?", "history": []})
        graph._classifier.classify.assert_awaited_once()

    async def test_click_text_and_yes_both_use_normal_classification(self) -> None:
        for message in ("원천징수 내역을 팩스로 보내줘", "네"):
            with self.subTest(message=message):
                classifier = RecordingIntentClassifier(IntentClassification(
                    refined_query=message, classification_type=ClassificationType.EMPTY_QUERY,
                    agent_code=None,
                ))
                graph = _graph(classifier)
                state = {"message": message, "history": [{
                    "role": "assistant", "content": "원천징수 내역입니다.",
                    "metadata": {"recommendedQuestions": [{
                        "id": "old-id", "question": "팩스로 전송해드릴까요?",
                        "affirmativeFollowup": {"message": "자동 실행하면 안 됨",
                            "agentCode": "PERFORMANCE_FEE",
                            "detailScenarioCode": "WITHHOLDING_TAX_FAX_SEND"},
                    }]},
                }]}
                update = await graph._classify_intent(state)
                self.assertEqual(len(classifier.calls), 1)
                self.assertEqual(classifier.calls[0]["message"], message)
                self.assertEqual(update["classification"]["refined_query"], message)
                self.assertEqual(graph._after_classification({**state, **update}), "finish_exception")

    async def test_unresolved_second_classification_does_not_loop(self) -> None:
        classifier = RecordingIntentClassifier(
            IntentClassification(
                refined_query="그중에는?",
                classification_type=ClassificationType.CONTEXT_REQUIRED,
                agent_code=None,
            )
        )
        graph = _graph(classifier)
        state = {
            "message": "그중에는?",
            "history": [{"role": "user", "content": "모호한 이전 질문"}],
        }

        update = await graph._classify_intent_with_history(state)

        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(classifier.calls[0]["context_mode"], "HISTORY_ALLOWED")
        self.assertEqual(
            update["classification"]["classification_type"],
            "EMPTY_QUERY",
        )


if __name__ == "__main__":
    unittest.main()
