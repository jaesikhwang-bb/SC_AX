"""운영 로그의 비식별화 경계와 LLM usage 계측을 검증한다."""

import io
import logging
import unittest
from types import SimpleNamespace
from uuid import uuid4

from app.observability import (
    LlmUsageCallbackHandler,
    configure_logging,
    log_context,
    logger,
)


class ObservabilityGuardrailTests(unittest.TestCase):
    def test_every_application_log_is_sanitized_before_output(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            configure_logging("INFO")
            with log_context(
                request_id="request-1",
                session_id="session-1",
                thread_id="thread-1",
                user_id="K3003980",
            ):
                logger.info(
                    "======== 보호 테스트 | Authorization=Bearer raw-secret | "
                    "질문=%s | 연락처=%s",
                    "홍길동의 주민번호 900101-1234567",
                    "010-1234-5678",
                )
                # 비밀 필드가 포맷 인자인 경우에도 placeholder 개수를 유지해야 한다.
                logger.info("======== 토큰 보호 테스트 | Authorization=%s", "raw-token")
        finally:
            root.removeHandler(handler)

        output = stream.getvalue()
        self.assertNotIn("raw-secret", output)
        self.assertNotIn("raw-token", output)
        self.assertNotIn("K3003980", output)
        self.assertNotIn("홍길동", output)
        self.assertNotIn("900101-1234567", output)
        self.assertNotIn("010-1234-5678", output)
        self.assertIn("sha256:", output)
        self.assertIn("Authorization=***", output)

    def test_llm_callback_records_usage_without_prompt_or_answer(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            configure_logging("INFO")
            callback = LlmUsageCallbackHandler(stage="test.generation", model="qwen")
            run_id = uuid4()
            callback.on_llm_start({}, ["민감한 프롬프트"], run_id=run_id)
            callback.on_llm_end(
                SimpleNamespace(
                    llm_output={
                        "token_usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 7,
                            "total_tokens": 19,
                        }
                    },
                    generations=[],
                ),
                run_id=run_id,
            )
        finally:
            root.removeHandler(handler)

        output = stream.getvalue()
        self.assertIn("입력토큰=12", output)
        self.assertIn("출력토큰=7", output)
        self.assertIn("전체토큰=19", output)
        self.assertNotIn("민감한 프롬프트", output)


if __name__ == "__main__":
    unittest.main()
