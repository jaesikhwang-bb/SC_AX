"""가처분 문의가 master 예외가 아닌 QUALIFICATION 고정 detail인지 검증한다."""

import unittest

from app.domain import ClassificationType
from app.graph import MasterIntentGraph
from app.prompt_loader import PromptBundleLoader
from app.subagents.fixed_responses import get_subagent_fixed_response
from app.subagents.prompt_loader import SubagentPromptLoader


class ProvisionalDispositionMigrationTests(unittest.TestCase):
    def test_master_exception_code_is_removed(self) -> None:
        values = {item.value for item in ClassificationType}
        self.assertNotIn("PROVISIONAL_DISPOSITION_INQUIRY", values)

        prompt = PromptBundleLoader().load().system_prompt
        self.assertNotIn("PROVISIONAL_DISPOSITION_INQUIRY", prompt)
        self.assertIn("`QUALIFICATION`을 선택한다", prompt)

    def test_qualification_manifest_registers_fixed_guidance(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="qualification")
        scenario = next(
            item
            for item in bundle.manifest["scenarios"]
            if item["code"] == "QUALIFICATION_FIXED_GUIDANCE"
        )
        details = {detail["code"]: detail for detail in scenario["details"]}
        detail = details["PROVISIONAL_DISPOSITION_GUIDANCE"]

        self.assertEqual(detail["parameters"], [])
        self.assertIsNotNone(
            get_subagent_fixed_response(
                "QUALIFICATION",
                "PROVISIONAL_DISPOSITION_GUIDANCE",
            )
        )

    def test_provisional_guidance_skips_mcp_route(self) -> None:
        subagent = {
            "agent_code": "QUALIFICATION",
            "prompt_version": "1.6.0",
            "scenario_code": "QUALIFICATION_FIXED_GUIDANCE",
            "scenario_name": "자격기준 관련 고정 안내",
            "detail_scenario_code": "PROVISIONAL_DISPOSITION_GUIDANCE",
            "detail_scenario_name": "가처분·압류 문의 안내",
            "parameters": {},
            "matches": [
                {
                    "scenario_code": "QUALIFICATION_FIXED_GUIDANCE",
                    "scenario_name": "자격기준 관련 고정 안내",
                    "detail_scenario_code": "PROVISIONAL_DISPOSITION_GUIDANCE",
                    "detail_scenario_name": "가처분·압류 문의 안내",
                    "parameters": {},
                }
            ],
        }

        route = MasterIntentGraph._after_subagent(
            {
                "entry_stage": "NEW_CHAT",
                "subagent": subagent,
            }
        )

        self.assertEqual(route, "end")


if __name__ == "__main__":
    unittest.main()
