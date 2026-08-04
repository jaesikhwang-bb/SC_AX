"""외부 네트워크 없이 주입한 의존성으로 HTTP 계약을 검증한다."""

import unittest
from dataclasses import replace

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.domain import ClassificationType, IntentClassification
from app.history import EmptyChatHistoryStore, InMemoryChatHistoryStore
from app.hitl_store import (
    HitlStateStoreUnavailableError,
    InMemoryHitlStateStore,
)
from app.subagents.router import EmptySubagentRouter
from app.subagents.models import SubagentResult, SubagentScenarioMatch


class FakeClassifier:
    async def classify(self, message, history, frontend_agent_code=None):
        return IntentClassification(
            refined_query=message,
            classification_type=ClassificationType.AGENT,
            agent_code="RP",
        )


class FakePerformanceFeeClassifier:
    """마스터가 PERFORMANCE_FEE를 선택하는 API 테스트 분류기."""

    async def classify(self, message, history, frontend_agent_code=None):
        return IntentClassification(
            refined_query=message,
            classification_type=ClassificationType.AGENT,
            agent_code="PERFORMANCE_FEE",
        )


class HistoryAwareClassifier:
    """두 번째 호출에서 이전 보정 질문을 사용했는지 보여 주는 분류기."""

    def __init__(self) -> None:
        self.received_histories: list[list[dict[str, str]]] = []

    async def classify(self, message, history, frontend_agent_code=None):
        self.received_histories.append(list(history))
        refined_query = (
            "2026년 6월 세금과 실제 지급액을 조회해줘"
            if history
            else "2026년 6월 수수료 내역을 조회해줘"
        )
        return IntentClassification(
            refined_query=refined_query,
            classification_type=ClassificationType.AGENT,
            agent_code="PERFORMANCE_FEE",
        )


class FakePerformanceFeeRouter(EmptySubagentRouter):
    def supports(self, agent_code: str) -> bool:
        return agent_code == "PERFORMANCE_FEE"

    def registered_codes(self) -> tuple[str, ...]:
        return ("PERFORMANCE_FEE",)

    async def classify(self, *, agent_code: str, query: str):
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


class FakeMultiScenarioRouter(FakePerformanceFeeRouter):
    """한 질문에서 같은 서브에이전트의 두 시나리오를 반환한다."""

    async def classify(self, *, agent_code: str, query: str):
        first = SubagentScenarioMatch(
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
        second = SubagentScenarioMatch(
            scenario_code="FEE_DETAILS",
            scenario_name="수수료 내역 조회",
            detail_scenario_code="FEE_TAX_NET_PAYMENT",
            detail_scenario_name="세금 및 실지급액 조회",
            parameters={
                "closing_year_month": "202606",
                "reference_date": None,
                "reference_year": None,
            },
        )
        return SubagentResult(
            agent_code=agent_code,
            prompt_version="v1",
            scenario_code=first.scenario_code,
            scenario_name=first.scenario_name,
            detail_scenario_code=first.detail_scenario_code,
            detail_scenario_name=first.detail_scenario_name,
            parameters=first.parameters,
            matches=[first, second],
        )


class FailingHitlStateStore(InMemoryHitlStateStore):
    """HITL Redis 장애에 대응하는 HTTP 계약을 검증하기 위한 저장소."""

    async def save(self, **kwargs) -> None:
        raise HitlStateStoreUnavailableError("테스트용 Redis 장애")


def make_test_settings() -> Settings:
    return Settings(
        genos_url="https://genos.genon.ai",
        genos_serving_id=850,
        genos_model="qwen/qwen3.7-flash",
        genos_bearer_token=None,
        prompt_version=None,
        history_backend="empty",
        redis_url="redis://localhost:6379/0",
        redis_history_key_prefix="chat:history",
        history_limit=10,
        redis_dedupe_ttl_seconds=86400,
        project_code="acqsc",
        redis_hitl_key_prefix="hitl:state",
        redis_hitl_ttl_seconds=3600,
        # HTTP 계약 단위 테스트만 외부 GenOS 호출을 대체한다.
        # 실제 .env와 목업 화면은 MCP_BACKEND=http을 사용한다.
        mcp_backend="mock",
    )


class ChatApiTest(unittest.TestCase):
    def setUp(self) -> None:
        # FastAPI lifespan을 실행해야 Checkpointer 없는 그래프가 생성된다.
        # 단위 테스트에서는 외부 Redis 대신 같은 인터페이스의 메모리 저장소를
        # 명시적으로 주입하며, 운영 코드는 일반 Redis 저장소를 사용한다.
        self.hitl_store = InMemoryHitlStateStore()
        self.client_context = TestClient(
            create_app(
                settings=make_test_settings(),
                classifier=FakeClassifier(),
                history_store=EmptyChatHistoryStore(),
                hitl_store=self.hitl_store,
                subagent_router=EmptySubagentRouter(),
            )
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def test_intent_tester_page_and_metadata(self) -> None:
        """HTML 테스트 화면과 manifest 기반 코드 목록을 제공해야 한다."""

        page = self.client.get("/tester")
        metadata = self.client.get("/v1/metadata")

        self.assertEqual(200, page.status_code)
        self.assertIn("1차 의도분류 테스트", page.text)
        self.assertIn('requestStream("v1/chat/stream"', page.text)
        self.assertEqual(200, metadata.status_code)
        self.assertEqual(6, len(metadata.json()["agent_codes"]))
        self.assertIn("RP", metadata.json()["agent_codes"])

    def test_chat_returns_only_classification_contract(self) -> None:
        response = self.client.post(
            "/v1/chat",
            json={
                "message": "RP 문의",
                "employee_id": "EMP001",
                "frontend_agent_code": "RP",
                "conversation_id": "chat-1",
            },
        )

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("PASS", body["status"])
        self.assertEqual("RP", body["classification"]["agent_code"])
        self.assertEqual(
            {
                "status",
                "thread_id",
                "classification",
                "subagent",
                "mcp",
                "mcp_results",
                "interrupt",
            },
            set(body),
        )
        self.assertIsNone(body["subagent"])

    def test_chat_without_frontend_agent_code_passes(self) -> None:
        """에이전트 미선택 요청은 코드 비교와 HITL 없이 바로 통과해야 한다."""

        response = self.client.post(
            "/v1/chat",
            json={
                "message": "RP 문의",
                "employee_id": "EMP001",
                "conversation_id": "chat-no-selection",
            },
        )

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("PASS", body["status"])
        self.assertEqual("RP", body["classification"]["agent_code"])
        self.assertIsNone(body["interrupt"])

    def test_empty_frontend_agent_code_is_treated_as_not_selected(self) -> None:
        """빈 문자열도 프론트 미선택과 동일하게 처리해야 한다."""

        response = self.client.post(
            "/v1/chat",
            json={
                "message": "RP 문의",
                "employee_id": "EMP001",
                "frontend_agent_code": "   ",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("PASS", response.json()["status"])

    def test_performance_fee_returns_subagent_result(self) -> None:
        """마스터 결과와 서브에이전트 결과가 한 응답에 포함돼야 한다."""

        with TestClient(
            create_app(
                settings=make_test_settings(),
                classifier=FakePerformanceFeeClassifier(),
                history_store=EmptyChatHistoryStore(),
                hitl_store=InMemoryHitlStateStore(),
                subagent_router=FakePerformanceFeeRouter(),
            )
        ) as client:
            response = client.post(
                "/v1/chat",
                json={
                    "message": "이번 달 실적을 알려줘",
                    "employee_id": "EMP001",
                    "frontend_agent_code": "PERFORMANCE_FEE",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("PERFORMANCE_FEE", response.json()["classification"]["agent_code"])
        self.assertEqual(
            "PERFORMANCE_SUMMARY_TOTAL",
            response.json()["subagent"]["detail_scenario_code"],
        )
        self.assertEqual(1, len(response.json()["subagent"]["matches"]))
        self.assertEqual("test_tool", response.json()["mcp"]["tool_name"])
        self.assertIn(
            "EMP001",
            response.json()["mcp"]["request_id"],
        )
        self.assertEqual(
            {"param1": "data1", "param2": "data2"},
            response.json()["mcp"]["result"],
        )
        self.assertEqual(1, len(response.json()["mcp_results"]))

    def test_one_subagent_returns_multiple_scenarios_and_mcp_results(
        self,
    ) -> None:
        """서브에이전트는 고정한 채 여러 시나리오와 MCP를 반환해야 한다."""

        with TestClient(
            create_app(
                settings=make_test_settings(),
                classifier=FakePerformanceFeeClassifier(),
                history_store=EmptyChatHistoryStore(),
                hitl_store=InMemoryHitlStateStore(),
                subagent_router=FakeMultiScenarioRouter(),
            )
        ) as client:
            response = client.post(
                "/v1/chat",
                json={
                    "message": "이번 달 실적과 세금 실지급액을 알려줘",
                    "employee_id": "EMP001",
                    "frontend_agent_code": "PERFORMANCE_FEE",
                },
            )

        body = response.json()
        self.assertEqual(200, response.status_code)
        self.assertEqual("PERFORMANCE_FEE", body["subagent"]["agent_code"])
        self.assertEqual(2, len(body["subagent"]["matches"]))
        self.assertEqual(
            ["PERFORMANCE_SUMMARY_TOTAL", "FEE_TAX_NET_PAYMENT"],
            [item["detail_scenario_code"] for item in body["subagent"]["matches"]],
        )
        self.assertEqual(2, len(body["mcp_results"]))
        self.assertEqual(
            2,
            len({item["request_id"] for item in body["mcp_results"]}),
        )
        # 기존 호환 필드는 첫 번째 결과를 유지한다.
        self.assertEqual(body["mcp_results"][0], body["mcp"])

    def test_same_conversation_id_supplies_previous_turn_to_classifier(
        self,
    ) -> None:
        """서로 다른 thread가 같은 conversation 문맥을 공유해야 한다."""

        classifier = HistoryAwareClassifier()
        with TestClient(
            create_app(
                settings=replace(
                    make_test_settings(),
                    history_backend="memory",
                ),
                classifier=classifier,
                history_store=InMemoryChatHistoryStore(),
                hitl_store=InMemoryHitlStateStore(),
                subagent_router=EmptySubagentRouter(),
            )
        ) as client:
            first = client.post(
                "/v1/chat",
                json={
                    "message": "2026년 6월 수수료 알려줘",
                    "employee_id": "EMP001",
                    "conversation_id": "multiturn-1",
                    "frontend_agent_code": "PERFORMANCE_FEE",
                },
            )
            second = client.post(
                "/v1/chat",
                json={
                    "message": "그중 세금과 실제 받은 돈은?",
                    "employee_id": "EMP001",
                    "conversation_id": "multiturn-1",
                    "frontend_agent_code": "PERFORMANCE_FEE",
                },
            )

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertNotEqual(
            first.json()["thread_id"],
            second.json()["thread_id"],
        )
        self.assertEqual([], classifier.received_histories[0])
        self.assertEqual(
            [
                {
                    "role": "user",
                    "content": "2026년 6월 수수료 내역을 조회해줘",
                }
            ],
            classifier.received_histories[1],
        )
        self.assertEqual(
            "2026년 6월 세금과 실제 지급액을 조회해줘",
            second.json()["classification"]["refined_query"],
        )

    def test_no_frontend_selection_still_uses_conversation_history(
        self,
    ) -> None:
        """에이전트 미선택 멀티턴도 최근 에이전트 이력을 전달해야 한다."""

        classifier = HistoryAwareClassifier()
        history_store = InMemoryChatHistoryStore()
        with TestClient(
            create_app(
                settings=replace(
                    make_test_settings(),
                    history_backend="memory",
                ),
                classifier=classifier,
                history_store=history_store,
                hitl_store=InMemoryHitlStateStore(),
                subagent_router=EmptySubagentRouter(),
            )
        ) as client:
            first = client.post(
                "/v1/chat",
                json={
                    "message": "2026년 6월 수수료 알려줘",
                    "employee_id": "EMP001",
                    "conversation_id": "multiturn-no-selection",
                },
            )
            second = client.post(
                "/v1/chat",
                json={
                    "message": "저번달은?",
                    "employee_id": "EMP001",
                    "conversation_id": "multiturn-no-selection",
                },
            )

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual([], classifier.received_histories[0])
        self.assertEqual(
            [
                {
                    "role": "user",
                    "content": "2026년 6월 수수료 내역을 조회해줘",
                }
            ],
            classifier.received_histories[1],
        )
        self.assertEqual("PASS", second.json()["status"])

    def test_tester_history_returns_only_requested_scope(self) -> None:
        """목업 이력 API는 요청한 사원·대화·에이전트 이력만 반환해야 한다."""

        history_store = InMemoryChatHistoryStore()
        with TestClient(
            create_app(
                settings=replace(
                    make_test_settings(),
                    history_backend="memory",
                ),
                classifier=FakeClassifier(),
                history_store=history_store,
                hitl_store=InMemoryHitlStateStore(),
                subagent_router=EmptySubagentRouter(),
            )
        ) as client:
            client.post(
                "/v1/chat",
                json={
                    "message": "RP 문의",
                    "employee_id": "EMP001",
                    "conversation_id": "history-view-1",
                    "frontend_agent_code": "RP",
                },
            )
            response = client.get(
                "/v1/tester/history",
                params={
                    "employee_id": "EMP001",
                    "conversation_id": "history-view-1",
                    "agent_code": "RP",
                },
            )
            other_agent = client.get(
                "/v1/tester/history",
                params={
                    "employee_id": "EMP001",
                    "conversation_id": "history-view-1",
                    "agent_code": "TABLET",
                },
            )

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("memory", body["backend"])
        self.assertEqual(1, body["count"])
        self.assertEqual(
            [{"role": "user", "content": "RP 문의"}],
            body["messages"],
        )
        self.assertEqual(0, other_agent.json()["count"])

    def test_tester_page_has_history_viewer(self) -> None:
        page = self.client.get("/tester")

        self.assertEqual(200, page.status_code)
        self.assertIn("대화내역 보기", page.text)
        self.assertIn("v1/tester/history?", page.text)
        self.assertIn("프로젝트 전체 대화 관리", page.text)
        self.assertIn("v1/tester/conversations", page.text)

    def test_tester_can_list_and_delete_project_conversation(self) -> None:
        """목업에서 프로젝트 대화 목록을 조회하고 선택 대화를 삭제한다."""

        history_store = InMemoryChatHistoryStore(project_code="acqsc")
        with TestClient(
            create_app(
                settings=replace(
                    make_test_settings(),
                    history_backend="memory",
                ),
                classifier=FakeClassifier(),
                history_store=history_store,
                hitl_store=InMemoryHitlStateStore(),
                subagent_router=EmptySubagentRouter(),
            )
        ) as client:
            client.post(
                "/v1/chat",
                json={
                    "message": "RP 문의",
                    "employee_id": "EMP001",
                    "conversation_id": "delete-from-tester",
                    "frontend_agent_code": "RP",
                },
            )
            before = client.get("/v1/tester/conversations")
            deleted = client.delete(
                "/v1/tester/conversations",
                params={
                    "employee_id": "EMP001",
                    "conversation_id": "delete-from-tester",
                },
            )
            after = client.get("/v1/tester/conversations")

        self.assertEqual(200, before.status_code)
        self.assertEqual("acqsc", before.json()["project_code"])
        self.assertEqual(1, before.json()["count"])
        self.assertEqual(
            "delete-from-tester",
            before.json()["conversations"][0]["conversation_id"],
        )
        self.assertEqual(1, deleted.json()["deleted_message_count"])
        self.assertEqual(0, after.json()["count"])

    def test_employee_id_is_required(self) -> None:
        response = self.client.post(
            "/v1/chat",
            json={
                "message": "RP 문의",
                "frontend_agent_code": "RP",
            },
        )
        self.assertEqual(422, response.status_code)

    def test_mismatch_and_resume(self) -> None:
        paused = self.client.post(
            "/v1/chat",
            json={
                "message": "RP 문의",
                "employee_id": "EMP001",
                "frontend_agent_code": "TABLET",
            },
        ).json()
        self.assertEqual("INPUT_REQUIRED", paused["status"])

        invalid = self.client.post(
            "/v1/chat",
            json={
                "thread_id": paused["thread_id"],
                "hitl_input": {"signal": "NO"},
            },
        )
        self.assertEqual(200, invalid.status_code)
        self.assertEqual("INPUT_REQUIRED", invalid.json()["status"])
        self.assertEqual(
            "OK 값을 입력해야 합니다.",
            invalid.json()["interrupt"]["errors"]["signal"],
        )

        resumed = self.client.post(
            "/v1/chat",
            json={
                "thread_id": paused["thread_id"],
                "hitl_input": {"signal": "OK"},
            },
        )
        self.assertEqual(200, resumed.status_code)
        self.assertEqual("PASS", resumed.json()["status"])

        # 승인 후 같은 thread_id를 다시 사용하면 이미 DEL됐으므로 404이다.
        duplicated = self.client.post(
            "/v1/chat",
            json={
                "thread_id": paused["thread_id"],
                "hitl_input": {"signal": "OK"},
            },
        )
        self.assertEqual(404, duplicated.status_code)
        self.assertEqual(
            "HITL_STATE_NOT_FOUND",
            duplicated.json()["detail"]["code"],
        )

    def test_unknown_thread_returns_hitl_state_not_found(self) -> None:
        """Redis에 상태가 없는 thread_id는 500 대신 404를 반환해야 한다."""

        response = self.client.post(
            "/v1/chat",
            json={
                "thread_id": "unknown-thread",
                "hitl_input": {"signal": "OK"},
            },
        )

        self.assertEqual(404, response.status_code)
        self.assertEqual(
            "HITL_STATE_NOT_FOUND",
            response.json()["detail"]["code"],
        )
        self.assertEqual(
            "START_NEW_CHAT",
            response.json()["detail"]["action"],
        )

    def test_hitl_redis_failure_returns_service_unavailable(self) -> None:
        """불일치 상태를 저장하지 못하면 재개 불가능한 응답을 주지 않아야 한다."""

        with TestClient(
            create_app(
                settings=make_test_settings(),
                classifier=FakeClassifier(),
                history_store=EmptyChatHistoryStore(),
                hitl_store=FailingHitlStateStore(),
                subagent_router=EmptySubagentRouter(),
            )
        ) as client:
            response = client.post(
                "/v1/chat",
                json={
                    "message": "RP 문의",
                    "employee_id": "EMP001",
                    "frontend_agent_code": "TABLET",
                },
            )

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            "HITL_STATE_STORE_UNAVAILABLE",
            response.json()["detail"]["code"],
        )
        self.assertEqual("RETRY", response.json()["detail"]["action"])

    def test_redis_free_development_mode_keeps_hitl_flow(self) -> None:
        """Redis가 없어도 불일치 결과와 후속 승인이 정상 동작해야 한다."""

        # make_test_settings의 기본 hitl_state_backend는 memory이다. hitl_store를
        # 일부러 주입하지 않아 create_app이 메모리 저장소를 선택하게 한다.
        with TestClient(
            create_app(
                # 연결될 수 없는 주소를 넣어 Redis 접근이 전혀 없음을 검증한다.
                settings=replace(
                    make_test_settings(),
                    redis_url="redis://127.0.0.1:1/0",
                ),
                classifier=FakeClassifier(),
                subagent_router=EmptySubagentRouter(),
            )
        ) as client:
            paused = client.post(
                "/v1/chat",
                json={
                    "message": "RP 문의",
                    "employee_id": "EMP001",
                    "frontend_agent_code": "TABLET",
                },
            )
            resumed = client.post(
                "/v1/chat",
                json={
                    "thread_id": paused.json()["thread_id"],
                    "hitl_input": {"signal": "OK"},
                },
            )

        self.assertEqual(200, paused.status_code)
        self.assertEqual("INPUT_REQUIRED", paused.json()["status"])
        self.assertEqual(
            "AGENT_CODE_MISMATCH",
            paused.json()["interrupt"]["type"],
        )
        self.assertEqual("RP", paused.json()["classification"]["agent_code"])
        self.assertEqual(200, resumed.status_code)
        self.assertEqual("PASS", resumed.json()["status"])

    def test_hitl_request_cannot_mix_new_chat_fields(self) -> None:
        """HITL 입력과 신규 질문을 섞으면 Redis 상태보다 먼저 거절해야 한다."""

        response = self.client.post(
            "/v1/chat",
            json={
                "thread_id": "waiting-thread",
                "hitl_input": {"signal": "OK"},
                "message": "동시에 보내면 안 되는 질문",
                "employee_id": "EMP001",
                "frontend_agent_code": "RP",
            },
        )

        self.assertEqual(422, response.status_code)

    def test_separate_resume_api_is_not_exposed(self) -> None:
        """HITL 처리는 /v1/chat 하나로만 제공한다."""

        response = self.client.post(
            "/v1/chat/unknown-thread/resume",
            json={"signal": "OK"},
        )

        self.assertEqual(404, response.status_code)
