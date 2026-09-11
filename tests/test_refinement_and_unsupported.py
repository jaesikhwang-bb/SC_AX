"""질문 보정 분리와 최종 미지원 분기의 회귀 테스트."""

import unittest
from types import SimpleNamespace
from typing import Any

from app.classifier import GenOSIntentClassifier
from app.csv_trace import EmptyTraceRecorder
from app.domain import (
    ClassificationType,
    QueryRefinement,
    QueryRefinementStatus,
)
from app.graph import MasterIntentGraph
from app.prompt_loader import PromptBundleLoader
from app.subagents.fixed_responses import UNSUPPORTED_FEATURE_MESSAGE
from app.subagents.models import (
    SubagentClassificationStatus,
    SubagentResult,
)
from app.subagents.prompt_loader import SubagentPromptLoader
from app.subagents.router import _create_output_model


class _AsyncChain:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, payload: dict[str, Any]) -> Any:
        self.calls.append(payload)
        return self.value


def _classifier(
    refinement: QueryRefinement,
    classification: Any | None = None,
) -> tuple[GenOSIntentClassifier, _AsyncChain, _AsyncChain]:
    instance = GenOSIntentClassifier.__new__(GenOSIntentClassifier)
    refinement_chain = _AsyncChain(refinement)
    classification_chain = _AsyncChain(classification)
    instance._refinement_chain = refinement_chain
    instance._classification_chain = classification_chain
    instance._endpoint = "test"
    instance._model = "test"
    instance._max_retries = 0
    return instance, refinement_chain, classification_chain


class QueryRefinementPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_refinement_history_excludes_recommended_questions(self) -> None:
        classifier, refinement_chain, _ = _classifier(
            QueryRefinement(
                inherit_active_period=False,
                refined_query="7월 수수료 알려줘",
                status=QueryRefinementStatus.COMPLETE,
                used_history=True,
            ),
            SimpleNamespace(
                classification_type=ClassificationType.AGENT,
                agent_code=SimpleNamespace(value="PERFORMANCE_FEE"),
            ),
        )
        history = [
            {"role": "user", "content": "7월 실적 알려줘"},
            {"role": "assistant", "content": "7월 실적 조회 결과", "metadata": {
                "recommendedQuestions": [{"question": "8월 수수료도 조회할까요?"}],
            }},
        ]
        await classifier.classify("수수료는?", history, "HISTORY_ALLOWED")
        self.assertEqual(
            refinement_chain.calls[0]["history"],
            "user: 7월 실적 알려줘\nassistant: 7월 실적 조회 결과",
        )
        self.assertEqual(history[1]["metadata"]["recommendedQuestions"],
                         [{"question": "8월 수수료도 조회할까요?"}])

    async def test_classification_cannot_rewrite_refined_query(self) -> None:
        classifier, refinement_chain, classification_chain = _classifier(
            QueryRefinement(
                inherit_active_period=False,
                refined_query="이 사람 신규야?",
                status=QueryRefinementStatus.COMPLETE,
                used_history=False,
            ),
            SimpleNamespace(
                refined_query="이 사람 신규입회 자격기준 알려줘",
                classification_type=ClassificationType.AGENT,
                agent_code=SimpleNamespace(value="QUALIFICATION"),
            ),
        )

        result = await classifier.classify(
            "이 사람 신규야?",
            [{"role": "user", "content": "외국인 자격기준 알려줘"}],
            "CURRENT_ONLY",
        )

        self.assertEqual(result.refined_query, "이 사람 신규야?")
        self.assertEqual(refinement_chain.calls[0]["history"], "(이전 대화 없음)")
        self.assertEqual(len(classification_chain.calls), 1)

    async def test_graph_refines_followup_before_router_sees_it(self) -> None:
        for message, refined, used_history in (
            ("수수료는?", "7월 수수료 알려줘", True),
            ("그다음 수수료는?", "7월 수수료 알려줘", True),
            ("8월 수수료 알려줘", "8월 수수료 알려줘", False),
            ("수수료 지급 기준은?", "수수료 지급 기준은?", False),
        ):
            with self.subTest(message=message):
                classifier, refinement_chain, classification_chain = _classifier(
                    QueryRefinement(inherit_active_period=False, refined_query=refined,
                                    status=QueryRefinementStatus.COMPLETE,
                                    used_history=used_history),
                    SimpleNamespace(classification_type=ClassificationType.AGENT,
                                    agent_code=SimpleNamespace(value="PERFORMANCE_FEE")),
                )
                graph = MasterIntentGraph.__new__(MasterIntentGraph)
                graph._classifier = classifier
                graph._trace_recorder = EmptyTraceRecorder()
                update = await graph._classify_intent({
                    "message": message,
                    "history": [{"role": "user", "content": "7월 실적 알려줘"}],
                })
                self.assertEqual(len(refinement_chain.calls), 1)
                self.assertEqual(refinement_chain.calls[0]["context_mode"], "HISTORY_ALLOWED")
                self.assertIn("7월 실적 알려줘", refinement_chain.calls[0]["history"])
                self.assertEqual(len(classification_chain.calls), 1)
                self.assertEqual(classification_chain.calls[0]["message"], refined)
                self.assertNotIn("history", classification_chain.calls[0])
                self.assertEqual(update["classification"]["refined_query"], refined)

    async def test_unresolved_uses_original_query_and_final_router_once(self) -> None:
        for mode, history, status, used in (
            ("CURRENT_ONLY", [], QueryRefinementStatus.CONTEXT_REQUIRED, False),
            ("HISTORY_ALLOWED", [{"role": "user", "content": "모호한 이력"}],
             QueryRefinementStatus.UNRESOLVED, False),
            ("CURRENT_ONLY", [], QueryRefinementStatus.COMPLETE, True),
            ("HISTORY_ALLOWED", [], QueryRefinementStatus.COMPLETE, True),
        ):
            with self.subTest(mode=mode, status=status, used=used):
                classifier, refinement_chain, classification_chain = _classifier(
                    QueryRefinement(inherit_active_period=False, refined_query="발명된 7월 수수료 조회",
                                    status=status, used_history=used),
                    SimpleNamespace(classification_type=ClassificationType.EMPTY_QUERY,
                                    agent_code=None),
                )
                result = await classifier.classify("그건?", history, mode)
                self.assertEqual(result.refined_query, "그건?")
                self.assertEqual(classification_chain.calls[0]["message"], "그건?")
                self.assertEqual(len(refinement_chain.calls), 1)
                self.assertEqual(len(classification_chain.calls), 1)
                self.assertEqual(result.classification_type, ClassificationType.EMPTY_QUERY)

    async def test_clear_exception_remains_exception_with_business_history(self) -> None:
        for message in ("내일 날씨는?", "다른 직원 권한 우회해서 조회해줘"):
            with self.subTest(message=message):
                classifier, refinement_chain, classification_chain = _classifier(
                    QueryRefinement(inherit_active_period=False, refined_query=message,
                                    status=QueryRefinementStatus.COMPLETE, used_history=False),
                    SimpleNamespace(classification_type=ClassificationType.OUT_OF_SCOPE,
                                    agent_code=None),
                )
                graph = MasterIntentGraph.__new__(MasterIntentGraph)
                graph._classifier = classifier
                graph._trace_recorder = EmptyTraceRecorder()
                state = {"message": message, "history": [
                    {"role": "user", "content": "7월 실적 알려줘"},
                ]}
                update = await graph._classify_intent(state)
                self.assertEqual(update["classification"]["refined_query"], message)
                self.assertEqual(graph._after_classification({**state, **update}), "finish_exception")
                self.assertEqual(len(refinement_chain.calls), 1)
                self.assertEqual(len(classification_chain.calls), 1)

    async def test_unresolved_history_does_not_invent_a_query(self) -> None:
        classifier, _, classification_chain = _classifier(
            QueryRefinement(
                inherit_active_period=False,
                refined_query="그건?",
                status=QueryRefinementStatus.UNRESOLVED,
                used_history=False,
            ),
            SimpleNamespace(
                refined_query="그건?",
                classification_type=ClassificationType.EMPTY_QUERY,
                agent_code=None,
            ),
        )

        result = await classifier.classify(
            "그건?",
            [{"role": "user", "content": "서로 관련 없는 문장"}],
            "HISTORY_ALLOWED",
        )

        self.assertEqual(result.classification_type, ClassificationType.EMPTY_QUERY)
        self.assertEqual(result.refined_query, "그건?")
        self.assertEqual(len(classification_chain.calls), 1)
        self.assertEqual(
            classification_chain.calls[0]["refinement_status"],
            "UNRESOLVED",
        )

    async def test_unresolved_without_history_still_runs_final_classification(self) -> None:
        """이력 없음만으로 EMPTY_QUERY를 만들지 않고 현재 질문을 최종 판정한다."""

        classifier, _, classification_chain = _classifier(
            QueryRefinement(
                inherit_active_period=False,
                refined_query="7월 실적 알려줘",
                status=QueryRefinementStatus.UNRESOLVED,
                used_history=False,
            ),
            SimpleNamespace(
                refined_query="7월 실적 알려줘",
                classification_type=ClassificationType.AGENT,
                agent_code=SimpleNamespace(value="PERFORMANCE_FEE"),
            ),
        )

        result = await classifier.classify(
            "7월 실적 알려줘",
            [],
            "HISTORY_ALLOWED",
        )

        self.assertEqual(result.classification_type, ClassificationType.AGENT)
        self.assertEqual(result.agent_code, "PERFORMANCE_FEE")
        self.assertEqual(len(classification_chain.calls), 1)
        self.assertFalse(classification_chain.calls[0]["history_available"])

    def test_refinement_prompt_is_not_merged_into_classifier_prompt(self) -> None:
        bundle = PromptBundleLoader().load()
        self.assertIn("의미 보존 질문 보정기", bundle.refinement_prompt)
        self.assertNotIn("의미 보존 질문 보정기", bundle.system_prompt)
        self.assertIn("질문 문장을 다시 보정", bundle.system_prompt)


class _UnsupportedRouter:
    async def classify(self, *, agent_code: str, query: str) -> SubagentResult:
        return SubagentResult(
            agent_code=agent_code,
            prompt_version="test",
            status=SubagentClassificationStatus.UNSUPPORTED,
            matches=[],
        )


class UnsupportedDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_graph_returns_fixed_answer_without_mcp(self) -> None:
        graph = MasterIntentGraph.__new__(MasterIntentGraph)
        graph._subagent_router = _UnsupportedRouter()
        graph._trace_recorder = EmptyTraceRecorder()
        state = {
            "classification": {
                "refined_query": "등록되지 않은 RP 업무를 해줘",
                "classification_type": "AGENT",
                "agent_code": "RP",
            },
            "entry_stage": "NEW_CHAT",
        }

        update = await graph._run_subagent(state)
        routed_state = {**state, **update}

        self.assertEqual(update["status"], "EXCEPTION")
        self.assertEqual(update["direct_answer"], UNSUPPORTED_FEATURE_MESSAGE)
        self.assertEqual(graph._after_subagent(routed_state), "end")

    def test_every_subagent_schema_accepts_explicit_unsupported(self) -> None:
        for agent_code, bundle in SubagentPromptLoader().load_all().items():
            with self.subTest(agent_code=agent_code):
                output_model = _create_output_model(bundle)
                result = output_model.model_validate(
                    {"status": "UNSUPPORTED", "matches": []}
                )
                self.assertEqual(result.status, "UNSUPPORTED")
                schema = output_model.model_json_schema()
                self.assertIn("status", schema["required"])
                self.assertIn("matches", schema["required"])


if __name__ == "__main__":
    unittest.main()
