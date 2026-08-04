"""그래프 라우팅, 일반 Redis 방식 HITL 재개, 이력 범위를 검증한다."""

import unittest

from app.domain import ClassificationType, IntentClassification
from app.graph import MasterIntentGraph
from app.history import EmptyChatHistoryStore
from app.hitl_store import HitlStateNotFoundError, InMemoryHitlStateStore


class FakeClassifier:
    """정해진 분류 결과를 반환하고 전달받은 LLM 입력을 기록한다."""

    def __init__(self, result: IntentClassification) -> None:
        self.result = result
        self.calls = 0
        self.received_history: list[dict[str, str]] = []
        self.received_frontend_agent_code: str | None = None

    async def classify(
        self,
        message,
        history,
        frontend_agent_code=None,
    ) -> IntentClassification:
        self.calls += 1
        self.received_history = history
        self.received_frontend_agent_code = frontend_agent_code
        return self.result


class RecordingHistoryStore(EmptyChatHistoryStore):
    """이력 조회와 저장 인자를 확인하기 위한 메모리 기록 저장소."""

    def __init__(self) -> None:
        self.loaded_employee_id: str | None = None
        self.loaded_agent_code: str | None = None
        self.saved: list[dict] = []

    async def get_recent(
        self,
        employee_id,
        conversation_id,
        agent_code,
        limit,
    ):
        self.loaded_employee_id = employee_id
        self.loaded_agent_code = agent_code
        return [{"role": "user", "content": "이전 RP 질문"}]

    async def get_recent_for_conversation(
        self,
        employee_id,
        conversation_id,
        limit,
    ):
        self.loaded_employee_id = employee_id
        self.loaded_agent_code = "RP"
        return "RP", [{"role": "user", "content": "이전 RP 질문"}]

    async def append_message(self, **kwargs):
        self.saved.append(kwargs)
        return True


def agent_result(code: str) -> IntentClassification:
    """테스트에 사용할 정상 에이전트 분류 결과를 만든다."""

    return IntentClassification(
        refined_query="보정된 질문",
        classification_type=ClassificationType.AGENT,
        agent_code=code,
    )


class MasterIntentGraphTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_thread_cannot_resume(self) -> None:
        """Redis에 HITL 상태가 없으면 업무 노드를 실행하지 않아야 한다."""

        graph = MasterIntentGraph(
            FakeClassifier(agent_result("RP")),
            RecordingHistoryStore(),
            InMemoryHitlStateStore(),
        )

        with self.assertRaises(HitlStateNotFoundError):
            await graph.resume(
                thread_id="unknown-thread",
                value={"signal": "OK"},
            )

    async def test_hitl_state_contains_project_scope_and_minimum_state(
        self,
    ) -> None:
        """저장 상태에 프로젝트 범위와 재개에 필요한 값이 포함되는지 검증한다."""

        hitl_store = InMemoryHitlStateStore(project_code="acqsc")
        graph = MasterIntentGraph(
            FakeClassifier(agent_result("RP")),
            RecordingHistoryStore(),
            hitl_store,
        )

        await graph.start(
            thread_id="project-scoped-thread",
            employee_id="EMP001",
            conversation_id="conversation-1",
            message="RP 문의",
            frontend_agent_code="TABLET",
        )
        entry = await hitl_store.get("project-scoped-thread")

        self.assertIsNotNone(entry)
        self.assertEqual("acqsc", entry.project_code)
        self.assertEqual("AGENT_CODE_MISMATCH", entry.hitl_type)
        self.assertEqual(
            "RP",
            entry.graph_state["classification"]["agent_code"],
        )
        self.assertNotIn("human_input", entry.graph_state)
        self.assertNotIn("entry_stage", entry.graph_state)

    async def test_matching_code_passes(self) -> None:
        classifier = FakeClassifier(agent_result("RP"))
        history_store = RecordingHistoryStore()
        hitl_store = InMemoryHitlStateStore()
        graph = MasterIntentGraph(
            classifier,
            history_store,
            hitl_store,
        )

        result = await graph.start(
            thread_id="matching",
            employee_id="EMP001",
            conversation_id="conversation-1",
            message="RP 문의",
            frontend_agent_code="RP",
        )

        self.assertEqual("PASS", result.status)
        self.assertIsNone(result.interrupt)
        self.assertIsNone(await hitl_store.get("matching"))
        self.assertEqual(1, classifier.calls)
        self.assertEqual("EMP001", history_store.loaded_employee_id)
        self.assertEqual("RP", history_store.loaded_agent_code)
        self.assertEqual(
            [{"role": "user", "content": "이전 RP 질문"}],
            classifier.received_history,
        )
        self.assertEqual("RP", classifier.received_frontend_agent_code)
        self.assertEqual("EMP001", history_store.saved[0]["employee_id"])
        self.assertEqual("RP", history_store.saved[0]["agent_code"])
        self.assertEqual("보정된 질문", history_store.saved[0]["content"])

    async def test_missing_frontend_code_uses_latest_agent_history(self) -> None:
        """프론트 미선택도 최근 에이전트 문맥을 사용하고 HITL은 생략한다."""

        classifier = FakeClassifier(agent_result("RP"))
        history_store = RecordingHistoryStore()
        hitl_store = InMemoryHitlStateStore()
        graph = MasterIntentGraph(
            classifier,
            history_store,
            hitl_store,
        )

        result = await graph.start(
            thread_id="no-frontend-selection",
            employee_id="EMP001",
            conversation_id="conversation-no-selection",
            message="알피 상품 알려줘",
            frontend_agent_code=None,
        )

        self.assertEqual("PASS", result.status)
        self.assertEqual("RP", result.classification.agent_code)
        self.assertIsNone(result.interrupt)
        # conversation에서 가장 최근인 에이전트 하나의 이력만 전달한다.
        self.assertEqual(
            [{"role": "user", "content": "이전 RP 질문"}],
            classifier.received_history,
        )
        self.assertEqual("RP", history_store.loaded_agent_code)
        # 저장할 때는 마스터가 확정한 agent_code를 사용한다.
        self.assertEqual("RP", history_store.saved[0]["agent_code"])
        self.assertEqual("보정된 질문", history_store.saved[0]["content"])
        self.assertIsNone(await hitl_store.get("no-frontend-selection"))

    async def test_mismatch_resumes_without_reclassifying(self) -> None:
        classifier = FakeClassifier(agent_result("RP"))
        history_store = RecordingHistoryStore()
        hitl_store = InMemoryHitlStateStore()
        graph = MasterIntentGraph(
            classifier,
            history_store,
            hitl_store,
        )

        paused = await graph.start(
            thread_id="mismatch",
            employee_id="EMP002",
            conversation_id="conversation-2",
            message="RP 문의",
            frontend_agent_code="TABLET",
        )
        stored_after_pause = await hitl_store.get("mismatch")

        invalid = await graph.resume(
            thread_id="mismatch",
            value={"signal": "NO"},
        )
        stored_after_invalid = await hitl_store.get("mismatch")

        resumed = await graph.resume(
            thread_id="mismatch",
            value={"signal": "OK"},
        )

        self.assertEqual("INPUT_REQUIRED", paused.status)
        self.assertEqual("AGENT_CODE_MISMATCH", paused.interrupt["type"])
        self.assertIsNotNone(stored_after_pause)
        self.assertEqual("INPUT_REQUIRED", invalid.status)
        self.assertEqual(
            "OK 값을 입력해야 합니다.",
            invalid.interrupt["errors"]["signal"],
        )
        self.assertIsNotNone(stored_after_invalid)
        self.assertEqual("PASS", resumed.status)
        self.assertEqual("RP", resumed.classification.agent_code)
        # 재개 요청은 저장된 분류 결과를 사용하므로 LLM은 최초 한 번만 호출된다.
        self.assertEqual(1, classifier.calls)
        # 승인 완료 후 사용된 HITL 상태는 Redis에서 제거된다.
        self.assertIsNone(await hitl_store.get("mismatch"))
        self.assertEqual("EMP002", history_store.loaded_employee_id)
        self.assertEqual("TABLET", history_store.loaded_agent_code)
        self.assertEqual("EMP002", history_store.saved[0]["employee_id"])
        self.assertEqual("RP", history_store.saved[0]["agent_code"])

    async def test_out_of_scope_does_not_persist(self) -> None:
        """OUT_OF_SCOPE 질문은 Redis 대화이력에 저장하지 않아야 한다."""

        classifier = FakeClassifier(
            IntentClassification(
                refined_query="오늘 날씨",
                classification_type=ClassificationType.OUT_OF_SCOPE,
                agent_code=None,
            )
        )
        history_store = RecordingHistoryStore()
        hitl_store = InMemoryHitlStateStore()
        graph = MasterIntentGraph(
            classifier,
            history_store,
            hitl_store,
        )

        result = await graph.start(
            thread_id="exception",
            employee_id="EMP003",
            conversation_id="conversation-3",
            message="오늘 날씨",
            frontend_agent_code="RP",
        )

        self.assertEqual("EXCEPTION", result.status)
        self.assertIsNone(result.interrupt)
        self.assertEqual([], history_store.saved)
        self.assertIsNone(await hitl_store.get("exception"))

    async def test_protected_data_exceptions_do_not_persist_or_hitl(
        self,
    ) -> None:
        """타 모집인·고객 상세 조회는 이력 저장과 HITL 없이 종료해야 한다."""

        cases = (
            (
                ClassificationType.OTHER_RECRUITER_DATA_REQUEST,
                "다른 모집인의 이번 달 실적을 보여줘",
            ),
            (
                ClassificationType.CUSTOMER_DETAIL_REQUEST,
                "내 고객의 상세 카드정보를 보여줘",
            ),
        )
        for index, (classification_type, message) in enumerate(cases):
            with self.subTest(classification_type=classification_type):
                classifier = FakeClassifier(
                    IntentClassification(
                        refined_query=message,
                        classification_type=classification_type,
                        agent_code=None,
                    )
                )
                history_store = RecordingHistoryStore()
                hitl_store = InMemoryHitlStateStore()
                graph = MasterIntentGraph(
                    classifier,
                    history_store,
                    hitl_store,
                )
                thread_id = f"protected-exception-{index}"

                result = await graph.start(
                    thread_id=thread_id,
                    employee_id="EMP003",
                    conversation_id="conversation-protected",
                    message=message,
                    frontend_agent_code="PERFORMANCE_FEE",
                )

                self.assertEqual("EXCEPTION", result.status)
                self.assertEqual(
                    classification_type,
                    result.classification.classification_type,
                )
                self.assertIsNone(result.classification.agent_code)
                self.assertIsNone(result.interrupt)
                self.assertIsNone(result.subagent)
                self.assertIsNone(result.mcp)
                self.assertEqual([], history_store.saved)
                self.assertIsNone(await hitl_store.get(thread_id))
