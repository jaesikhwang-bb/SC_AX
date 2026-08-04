"""신규 프론트 입력 계약과 SSE 최종 답변 흐름을 검증한다."""

import json
import unittest

from fastapi.testclient import TestClient

from app.api import create_app
from app.domain import ClassificationType, IntentClassification
from app.history import EmptyChatHistoryStore, InMemoryChatHistoryStore
from app.hitl_store import InMemoryHitlStateStore
from app.subagents.models import SubagentResult
from app.subagents.router import EmptySubagentRouter
from tests.test_api import make_test_settings


def parse_sse(raw: str) -> list[dict]:
    """테스트 클라이언트가 모은 SSE 문자열을 event envelope 배열로 바꾼다."""

    events = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[5:].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


class StaticClassifier:
    def __init__(self, classification_type, agent_code, refined_query):
        self._result = IntentClassification(
            refined_query=refined_query,
            classification_type=classification_type,
            agent_code=agent_code,
        )

    async def classify(self, message, history, frontend_agent_code=None):
        return self._result


class StaticRouter(EmptySubagentRouter):
    def __init__(self, result: SubagentResult):
        self._result = result

    def supports(self, agent_code: str) -> bool:
        return agent_code == self._result.agent_code

    def registered_codes(self) -> tuple[str, ...]:
        return (self._result.agent_code,)

    async def classify(self, *, agent_code: str, query: str):
        return self._result


def performance_result() -> SubagentResult:
    return SubagentResult(
        agent_code="PERFORMANCE_FEE",
        prompt_version="v1",
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


def qualification_result() -> SubagentResult:
    return SubagentResult(
        agent_code="QUALIFICATION",
        prompt_version="v1",
        scenario_code="PERSONAL_MEMBER_QUALIFICATION",
        scenario_name="개인회원 입회 자격기준",
        detail_scenario_code="NEW_MEMBER_QUALIFICATION",
        detail_scenario_name="신규회원 입회 자격기준",
        parameters={
            "member_category": "NEW",
            "restricted_request_type": None,
        },
    )


class StreamingChatApiTest(unittest.TestCase):
    def _client(self, classifier, router=None, history_store=None):
        return TestClient(
            create_app(
                settings=make_test_settings(),
                classifier=classifier,
                history_store=history_store or EmptyChatHistoryStore(),
                hitl_store=InMemoryHitlStateStore(),
                subagent_router=router or EmptySubagentRouter(),
            )
        )

    @staticmethod
    def _request(message: str, agent_code: str | None = None) -> dict:
        body = {
            "message": message,
            "session_id": "stream-session-001",
            "endpoint": "acqsc",
            "employee_id": "EMP001",
            "humanInput": [],
        }
        if agent_code is not None:
            body["agent_code"] = agent_code
        return body

    def test_performance_fee_streams_fixed_mcp_data_answer(self) -> None:
        classifier = StaticClassifier(
            ClassificationType.AGENT,
            "PERFORMANCE_FEE",
            "내 실적을 조회해줘",
        )
        history_store = InMemoryChatHistoryStore()
        with self._client(
            classifier,
            StaticRouter(performance_result()),
            history_store,
        ) as client:
            response = client.post(
                "/v1/chat/stream",
                json=self._request("내 실적을 조회해줘", "PERFORMANCE_FEE"),
            )
            history_response = client.get(
                "/v1/tester/history",
                params={
                    "employee_id": "EMP001",
                    "conversation_id": "stream-session-001",
                    "agent_code": "PERFORMANCE_FEE",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = parse_sse(response.text)
        names = [item["event"] for item in events]
        answer = "".join(
            item["data"] for item in events if item["event"] == "token"
        )
        self.assertEqual("request_id", names[0])
        self.assertIn("thread_id", names)
        self.assertIn("messages", names)
        self.assertNotIn("sourceDocuments", names)
        self.assertIn("테스트 고정답변입니다", answer)
        self.assertIn("test_tool", answer)
        self.assertEqual("end", names[-1])
        self.assertEqual(
            ["user", "assistant"],
            [item["role"] for item in history_response.json()["messages"]],
        )

    def test_qualification_streams_rag_sources_and_answer(self) -> None:
        classifier = StaticClassifier(
            ClassificationType.AGENT,
            "QUALIFICATION",
            "신규회원 입회 자격기준을 알려줘",
        )
        with self._client(classifier, StaticRouter(qualification_result())) as client:
            response = client.post(
                "/v1/chat/stream",
                json=self._request("신규회원 입회 자격기준을 알려줘", "QUALIFICATION"),
            )

        events = parse_sse(response.text)
        source_event = next(
            item for item in events if item["event"] == "sourceDocuments"
        )
        answer = "".join(
            item["data"] for item in events if item["event"] == "token"
        )
        self.assertEqual(1, len(source_event["data"]))
        self.assertEqual("test_tool", source_event["data"][0]["source"])
        # 테스트 설정은 LLM 토큰이 없으므로 문서 기반 대체 스트림을 사용한다.
        self.assertIn("테스트 RAG 답변입니다", answer)

    def test_exception_streams_fixed_answer_without_mcp(self) -> None:
        classifier = StaticClassifier(
            ClassificationType.OUT_OF_SCOPE,
            None,
            "오늘 날씨 알려줘",
        )
        with self._client(classifier) as client:
            response = client.post(
                "/v1/chat/stream",
                json=self._request("오늘 날씨 알려줘"),
            )

        events = parse_sse(response.text)
        answer = "".join(
            item["data"] for item in events if item["event"] == "token"
        )
        self.assertNotIn("sourceDocuments", [item["event"] for item in events])
        self.assertIn("카드 모집인 업무와 관련된 질문", answer)
        end = next(item for item in events if item["event"] == "end")
        self.assertEqual("EXCEPTION", end["data"]["status"])

    def test_agent_mismatch_streams_action_linked_to_human_input(self) -> None:
        classifier = StaticClassifier(
            ClassificationType.AGENT,
            "PERFORMANCE_FEE",
            "내 수수료를 조회해줘",
        )
        with self._client(classifier, StaticRouter(performance_result())) as client:
            first = client.post(
                "/v1/chat/stream",
                json=self._request("내 수수료를 조회해줘", "RP"),
            )
            first_events = parse_sse(first.text)
            action = next(item for item in first_events if item["event"] == "action")
            thread_id = action["data"]["thread_id"]
            self.assertEqual("signal", action["data"]["inputs"][0]["code"])

            wrong_owner = client.post(
                "/v1/chat/stream",
                json={
                    "session_id": "stream-session-001",
                    "thread_id": thread_id,
                    "endpoint": "acqsc",
                    "employee_id": "EMP999",
                    "humanInput": [{"code": "signal", "input": "OK"}],
                },
            )
            wrong_events = parse_sse(wrong_owner.text)
            self.assertEqual(
                "HITL_STATE_NOT_FOUND",
                next(
                    item for item in wrong_events if item["event"] == "error"
                )["data"]["code"],
            )

            resumed = client.post(
                "/v1/chat/stream",
                json={
                    "session_id": "stream-session-001",
                    "thread_id": thread_id,
                    "endpoint": "acqsc",
                    "employee_id": "EMP001",
                    "humanInput": [{"code": "signal", "input": "OK"}],
                },
            )

        resumed_events = parse_sse(resumed.text)
        self.assertNotIn("action", [item["event"] for item in resumed_events])
        self.assertEqual(
            "PASS",
            next(item for item in resumed_events if item["event"] == "end")["data"]["status"],
        )

    def test_stream_request_contract_rejects_invalid_modes(self) -> None:
        classifier = StaticClassifier(
            ClassificationType.AGENT,
            "PERFORMANCE_FEE",
            "질문",
        )
        with self._client(classifier) as client:
            missing_message = client.post(
                "/v1/chat/stream",
                json={
                    "session_id": "s1",
                    "endpoint": "acqsc",
                    "employee_id": "EMP001",
                    "humanInput": [],
                },
            )
            wrong_endpoint = client.post(
                "/v1/chat/stream",
                json=self._request("질문") | {"endpoint": "other"},
            )

        self.assertEqual(422, missing_message.status_code)
        self.assertEqual(422, wrong_endpoint.status_code)
