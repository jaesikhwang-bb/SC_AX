"""RP·PERFORMANCE_FEE 공통 복합환산의 프론트 선택 유지 규칙 테스트."""

import unittest

from app.csv_trace import EmptyTraceRecorder
from app.graph import MasterIntentGraph


class SharedCompositeAgentRoutingTests(unittest.TestCase):
    @staticmethod
    def _verify(
        *,
        frontend_agent_code: str | None,
        classified_agent_code: str,
        query: str,
    ) -> dict:
        graph = MasterIntentGraph.__new__(MasterIntentGraph)
        graph._trace_recorder = EmptyTraceRecorder()
        return graph._verify_selection(
            {
                "message": query,
                "frontend_agent_code": frontend_agent_code,
                "classification": {
                    "refined_query": query,
                    "classification_type": "AGENT",
                    "agent_code": classified_agent_code,
                },
            }
        )

    def test_rp_selection_keeps_composite_score_without_hitl(self) -> None:
        result = self._verify(
            frontend_agent_code="RP",
            classified_agent_code="PERFORMANCE_FEE",
            query="지난달 복합환산 점수와 실적 건수를 보여줘",
        )

        self.assertEqual(result["status"], "PASS")
        self.assertIsNone(result["interrupt"])
        self.assertEqual(result["classification"]["agent_code"], "RP")

    def test_rp_selection_keeps_composite_excluded_without_hitl(self) -> None:
        result = self._verify(
            frontend_agent_code="RP",
            classified_agent_code="PERFORMANCE_FEE",
            query="복합환산 미반영 내역을 보여줘",
        )

        self.assertEqual(result["status"], "PASS")
        self.assertIsNone(result["interrupt"])
        self.assertEqual(result["classification"]["agent_code"], "RP")

    def test_performance_fee_selection_is_also_preserved_for_shared_query(self) -> None:
        result = self._verify(
            frontend_agent_code="PERFORMANCE_FEE",
            classified_agent_code="RP",
            query="내 RP 환산점수와 실적 건수를 알려줘",
        )

        self.assertEqual(result["status"], "PASS")
        self.assertIsNone(result["interrupt"])
        self.assertEqual(
            result["classification"]["agent_code"],
            "PERFORMANCE_FEE",
        )

    def test_rp_selection_still_requests_change_for_fee_only_query(self) -> None:
        result = self._verify(
            frontend_agent_code="RP",
            classified_agent_code="PERFORMANCE_FEE",
            query="지난달 실제 지급 수수료와 세금을 보여줘",
        )

        self.assertEqual(result["status"], "INPUT_REQUIRED")
        self.assertIsNotNone(result["interrupt"])
        self.assertEqual(result["interrupt"]["action_code"], "SWITCH_AGENT")
        self.assertEqual(
            result["interrupt"]["fields"][0]["name"],
            "SWITCH_AGENT",
        )
        self.assertEqual(
            result["interrupt"]["fields"][0]["allowed_values"],
            ["yes", "no"],
        )
        self.assertEqual(
            result["interrupt"]["context"]["classified_agent_code"],
            "PERFORMANCE_FEE",
        )

    def test_unselected_agent_requests_switch_confirmation(self) -> None:
        """agent_code=null이면 자동 PASS하지 않고 분류 에이전트 전환을 확인한다."""

        result = self._verify(
            frontend_agent_code=None,
            classified_agent_code="PERFORMANCE_FEE",
            query="지난달 실적을 보여줘",
        )

        self.assertEqual(result["status"], "INPUT_REQUIRED")
        self.assertFalse(result["approved"])
        self.assertEqual(result["interrupt"]["action_code"], "SWITCH_AGENT")
        self.assertIsNone(
            result["interrupt"]["context"]["frontend_agent_code"]
        )
        self.assertEqual(
            result["interrupt"]["context"]["classified_agent_code"],
            "PERFORMANCE_FEE",
        )

    def test_blank_agent_requests_switch_confirmation(self) -> None:
        """그래프에 빈 문자열이 직접 전달되어도 미선택으로 동일하게 처리한다."""

        result = self._verify(
            frontend_agent_code="   ",
            classified_agent_code="QUALIFICATION",
            query="개인회원 입회 자격기준을 알려줘",
        )

        self.assertEqual(result["status"], "INPUT_REQUIRED")
        self.assertEqual(result["interrupt"]["action_code"], "SWITCH_AGENT")
        self.assertIsNone(
            result["interrupt"]["context"]["frontend_agent_code"]
        )


if __name__ == "__main__":
    unittest.main()
