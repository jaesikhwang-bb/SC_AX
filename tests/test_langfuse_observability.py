"""Langfuse 4.14.0 no-op 래퍼의 계층·비식별화·종료 계약을 검증한다."""

from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import observability


class _FakeObservation:
    def __init__(self, kind: str, values: dict) -> None:
        self.kind = kind
        self.values = values
        self.updates: list[dict] = []
        self.ended = False

    def update(self, **values) -> None:
        self.updates.append(values)

    def end(self) -> None:
        self.ended = True


class _FakeObservationContext:
    def __init__(self, observation: _FakeObservation) -> None:
        self.observation = observation

    def __enter__(self) -> _FakeObservation:
        return self.observation

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.observation.end()


class _FakeLangfuseClient:
    def __init__(self) -> None:
        self.current: list[_FakeObservation] = []
        self.manual: list[_FakeObservation] = []
        self.events: list[_FakeObservation] = []

    def start_as_current_observation(self, **values):
        observation = _FakeObservation("current", values)
        self.current.append(observation)
        return _FakeObservationContext(observation)

    def start_observation(self, **values):
        observation = _FakeObservation("manual", values)
        self.manual.append(observation)
        return observation

    def create_event(self, **values):
        observation = _FakeObservation("event", values)
        observation.end()
        self.events.append(observation)
        return observation

    def auth_check(self) -> bool:
        return True


@contextmanager
def _fake_propagate_attributes(**values):
    yield values


class LangfuseObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _FakeLangfuseClient()
        self.patchers = [
            patch.object(observability, "_LANGFUSE_CLIENT", self.client),
            patch.object(observability, "_LANGFUSE_ENABLED", True),
            patch.object(
                observability,
                "_LANGFUSE_PROPAGATE_ATTRIBUTES",
                _fake_propagate_attributes,
            ),
            patch.object(observability, "_LANGFUSE_PROJECT_CODE", "acqsc"),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()

    def test_requirement_pins_exact_sdk_version(self) -> None:
        requirements = Path("requirements.txt").read_text(encoding="utf-8")
        self.assertIn("langfuse==4.14.0", requirements.splitlines())

    def test_configure_langfuse_accepts_only_documented_sdk_version(self) -> None:
        fake_module = SimpleNamespace(
            Langfuse=lambda **kwargs: self.client,
            propagate_attributes=_fake_propagate_attributes,
        )
        with patch.dict(
            sys.modules,
            {"langfuse": fake_module},
        ), patch.object(
            observability.importlib.metadata,
            "version",
            return_value="4.14.0",
        ):
            status = observability.configure_langfuse(
                enabled=True,
                host="http://langfuse.internal:3000",
                public_key="pk-lf-test",
                secret_key="sk-lf-test",
                auth_check_on_startup=True,
                project_code="acqsc",
            )

        self.assertTrue(status["enabled"])
        self.assertEqual(status["requiredSdkVersion"], "4.14.0")

    def test_request_trace_contains_no_question_or_employee_plaintext(self) -> None:
        with observability.langfuse_request_trace(
            request_id="acqsc:req-1",
            session_id="session-1",
            thread_id="thread-1",
            user_id="K3003980",
            endpoint="acqsc",
            frontend_agent_code="RP",
            is_hitl_continuation=False,
            input_guardrail_action="MASK",
            input_length=27,
        ) as trace:
            observability.finish_langfuse_trace(
                trace,
                status="PASS",
                duration_seconds=1.23456,
                agent_code="RP",
                scenario_codes=["CITY_GAS_AUTOPAY_GUIDE"],
                answer_length=55,
            )

        self.assertEqual(len(self.client.current), 1)
        root = self.client.current[0]
        self.assertEqual(root.values["name"], "acqsc.chat")
        self.assertEqual(root.values["input"]["messageLength"], 27)
        serialized = repr((root.values, root.updates))
        self.assertNotIn("K3003980", serialized)
        self.assertNotIn("사용자 질문 원문", serialized)
        self.assertTrue(root.ended)

    def test_timed_function_becomes_child_stage_and_checkpoint_event(self) -> None:
        @observability.timed("테스트 업무 단계")
        def sample() -> str:
            observability.record_langfuse_checkpoint(
                {
                    "stageCode": "TEST_CHECKPOINT",
                    "stage": "테스트 분기",
                    "details": {"documents": ["비밀문서본문"]},
                }
            )
            return "ok"

        with observability.langfuse_request_trace(
            request_id="req-2",
            session_id="session-2",
            thread_id="thread-2",
            user_id="S123456",
            endpoint="acqsc",
            frontend_agent_code=None,
            is_hitl_continuation=False,
            input_guardrail_action="PASS",
            input_length=3,
        ):
            self.assertEqual(sample(), "ok")

        self.assertEqual(len(self.client.current), 2)
        stage = self.client.current[1]
        self.assertEqual(stage.values["name"], "stage.테스트 업무 단계")
        self.assertEqual(stage.updates[-1]["output"]["phase"], "COMPLETED")
        self.assertEqual(len(self.client.events), 1)
        event_text = repr(self.client.events[0].values)
        self.assertNotIn("비밀문서본문", event_text)
        self.assertIn("itemCount", event_text)

    def test_llm_callback_records_generation_usage_and_always_ends(self) -> None:
        callback = observability.create_llm_usage_callback(
            stage="master.intent_classification",
            model="test-model",
        )
        response = SimpleNamespace(
            llm_output={
                "token_usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                }
            },
            generations=[],
        )

        with observability.langfuse_request_trace(
            request_id="req-3",
            session_id="session-3",
            thread_id="thread-3",
            user_id="U123456",
            endpoint="acqsc",
            frontend_agent_code=None,
            is_hitl_continuation=False,
            input_guardrail_action="PASS",
            input_length=4,
        ):
            callback.on_chat_model_start({}, [[]], run_id="llm-run-1")
            callback.on_llm_end(response, run_id="llm-run-1")

        self.assertEqual(len(self.client.manual), 1)
        generation = self.client.manual[0]
        self.assertEqual(generation.values["as_type"], "generation")
        self.assertEqual(generation.values["model"], "test-model")
        self.assertEqual(
            generation.updates[-1]["usage_details"],
            {"input": 10, "output": 4, "total": 14},
        )
        self.assertTrue(generation.ended)


if __name__ == "__main__":
    unittest.main()
