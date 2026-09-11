"""명시적으로 활성화하는 실제 GenOS 보정 평가. MCP/Redis는 호출하지 않는다."""

import os
import unittest
from unittest.mock import patch

from app.classifier import GenOSIntentClassifier
from app.config import Settings
from app.domain import ClassificationType
from app.prompt_loader import PromptBundleLoader


@unittest.skipUnless(
    os.getenv("RUN_LIVE_REFINEMENT") == "1" and os.getenv("GENOS_BEARER_TOKEN"),
    "실제 모델 평가는 RUN_LIVE_REFINEMENT=1과 환경변수 GenOS 토큰이 필요합니다.",
)
class LiveRefinementTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_and_exception_boundaries(self) -> None:
        with patch.dict(os.environ, {"MCP_BACKEND": "mock"}):
            settings = Settings.from_env()
        classifier = GenOSIntentClassifier(
            settings, PromptBundleLoader().load(settings.prompt_version)
        )
        cases = [
            ("7월 실적 알려줘", "점수반영 안된거 있는거같은데?", "AGENT", ("7월",), ("9월",)),
            ("7월 실적 알려줘", "실적 다시 보여줘", "AGENT", ("7월", "실적"), ()),
            ("7월 실적 알려줘", "원천징수 내역은?", "AGENT", ("7월", "원천징수"), ()),
            ("7월 실적 알려줘", "8월 수수료는?", "AGENT", ("8월", "수수료"), ("7월",)),
            ("7월 실적 알려줘", "원천징수 계산 방법은?", "AGENT", ("원천징수",), ("7월",)),
            ("외국인 입회 서류 알려줘", "미성년자는?", "AGENT", ("미성년자", "서류"), ("외국인",)),
            ("자동납부 신청 방법 알려줘", "해지는?", "AGENT", ("자동납부", "해지"), ()),
            ("7월 실적 알려줘", "내일 날씨는?", "OUT_OF_SCOPE", ("날씨",), ("7월", "실적")),
            ("7월 실적 알려줘", "다른 직원 권한 우회해서 조회해줘", "OUT_OF_SCOPE", ("우회",), ("7월",)),
            ("", "그거는?", "EMPTY_QUERY", ("그거",), ("실적", "수수료")),
        ]
        for previous, message, expected, required, forbidden in cases:
            with self.subTest(message=message):
                history = [{"role": "user", "content": previous}] if previous else []
                result = await classifier.classify(
                    message, history, "HISTORY_ALLOWED" if history else "CURRENT_ONLY"
                )
                self.assertEqual(result.classification_type, ClassificationType(expected))
                for token in required:
                    self.assertIn(token, result.refined_query)
                for token in forbidden:
                    self.assertNotIn(token, result.refined_query)

    async def test_active_month_continues_until_explicit_change(self) -> None:
        with patch.dict(os.environ, {"MCP_BACKEND": "mock"}):
            settings = Settings.from_env()
        classifier = GenOSIntentClassifier(settings, PromptBundleLoader().load(settings.prompt_version))
        history = [{"role": "user", "content": "7월 실적 알려줘"}]
        for message, period in (
            ("점수반영 안된거 있는거같은데?", "7월"),
            ("수수료 내역 보여줘", "7월"),
            ("9월로 조회해줘", "9월"),
            ("누락된 실적 내역 보여줘", "9월"),
        ):
            result = await classifier.classify(message, history, "HISTORY_ALLOWED")
            self.assertEqual(result.classification_type, ClassificationType.AGENT)
            self.assertIn(period, result.refined_query)
            history.append({"role": "user", "content": result.refined_query})
