"""프롬프트 전체 결합과 동적 JSON Schema 생성을 검증한다."""

import unittest

from app.domain import create_structured_output_model
from app.prompt_loader import PromptBundleLoader


class PromptBundleLoaderTest(unittest.TestCase):
    def test_loads_all_six_agent_prompts(self) -> None:
        bundle = PromptBundleLoader().load()

        self.assertEqual(6, len(bundle.agent_codes))
        self.assertEqual(8, len(bundle.files))
        for code in bundle.agent_codes:
            self.assertIn(f"## [{code}]", bundle.system_prompt)

        output_model = create_structured_output_model(bundle.agent_codes)
        schema = output_model.model_json_schema()
        agent_enum = schema["$defs"]["AgentCode"]["enum"]
        self.assertEqual(set(bundle.agent_codes), set(agent_enum))

        # RP와 PERFORMANCE_FEE가 함께 지원하는 복합환산 조회는 프론트에서
        # 선택한 업무 화면을 우선하고, 미선택이면 PERFORMANCE_FEE를 기본으로
        # 사용한다는 중복 업무 정책이 결합 프롬프트에서 빠지지 않아야 한다.
        self.assertIn(
            "`RP`와 `PERFORMANCE_FEE`가 공통 지원하는",
            bundle.system_prompt,
        )
        self.assertIn(
            "프론트 에이전트를 선택하지 않은 경우",
            bundle.system_prompt,
        )
        self.assertIn(
            "프론트 선택이 `PERFORMANCE_FEE`이고 질문이 `내 RP 실적 조회`",
            bundle.system_prompt,
        )
        self.assertIn(
            "`RP`라는 단어 자체는 RP 에이전트로 교체하는 근거가 아니다",
            bundle.system_prompt,
        )

    def test_history_is_used_only_for_incomplete_current_question(self) -> None:
        """완결된 현재 질문에 과거 주제를 덧붙이지 않는 규칙을 검증한다."""

        bundle = PromptBundleLoader().load()

        self.assertIn(
            "현재 질문을 먼저 독립적으로 해석한다",
            bundle.system_prompt,
        )
        self.assertIn(
            "현재 질문만으로 생략된 대상이나 참조를 확정할 수 없을 때만",
            bundle.system_prompt,
        )
        self.assertIn(
            '현재 질문: "내 실적 알려줘."',
            bundle.system_prompt,
        )
        self.assertIn(
            "`내 RP 실적 알려줘`로 변경하지",
            bundle.system_prompt,
        )
        self.assertIn(
            '보정 질의: "지난달 내 환산점수를 알려줘."',
            bundle.system_prompt,
        )
        self.assertIn(
            '현재 질문: "저번달은?"',
            bundle.system_prompt,
        )
        self.assertIn(
            "절대로 `EMPTY_QUERY`로 분류하지 않는다",
            bundle.system_prompt,
        )


if __name__ == "__main__":
    unittest.main()
