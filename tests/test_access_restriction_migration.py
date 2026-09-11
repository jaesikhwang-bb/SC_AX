"""개인별 조회 제한이 master 예외가 아닌 PERFORMANCE_FEE detail인지 검증한다."""

import unittest

from app.domain import ClassificationType
from app.graph import MasterIntentGraph
from app.prompt_loader import PromptBundleLoader
from app.subagents.fixed_responses import get_subagent_fixed_response
from app.subagents.prompt_loader import SubagentPromptLoader


class AccessRestrictionMigrationTests(unittest.TestCase):
    def test_removed_master_exception_codes_are_not_in_output_schema(self) -> None:
        values = {item.value for item in ClassificationType}
        self.assertNotIn("OTHER_RECRUITER_DATA_REQUEST", values)
        self.assertNotIn("CUSTOMER_DISPOSAL_REASON_REQUEST", values)

    def test_master_routes_restricted_performance_requests_to_agent(self) -> None:
        prompt = PromptBundleLoader().load().system_prompt
        self.assertNotIn("### [OTHER_RECRUITER_DATA_REQUEST]", prompt)
        self.assertNotIn("### [CUSTOMER_DISPOSAL_REASON_REQUEST]", prompt)
        self.assertIn("PERFORMANCE_FEE 서브에이전트가 판단한다", prompt)
        self.assertIn("식별 표현의 형식이 불완전해도", prompt)

    def test_performance_manifest_registers_both_fixed_details(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="performance-fee")
        access_scenario = next(
            scenario
            for scenario in bundle.manifest["scenarios"]
            if scenario["code"] == "ACCESS_RESTRICTION"
        )
        detail_codes = {detail["code"] for detail in access_scenario["details"]}
        self.assertEqual(
            detail_codes,
            {
                "OTHER_RECRUITER_DATA_REQUEST",
                "CUSTOMER_DISPOSAL_REASON_REQUEST",
                "CUSTOMER_SALES_PERFORMANCE_REQUEST",
                "CUSTOMER_PERFORMANCE_DETAIL_REQUEST",
                "CUSTOMER_WITHDRAWAL_REQUEST",
                "CUSTOMER_CONVERSION_REQUEST",
                "CUSTOMER_UNREGISTERED_IDENTITY_REQUEST",
            },
        )
        for detail_code in detail_codes:
            self.assertIsNotNone(
                get_subagent_fixed_response("PERFORMANCE_FEE", detail_code)
            )

    def test_required_multi_match_does_not_add_mcp_details_to_restriction(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="performance-fee")
        rule = bundle.manifest["required_match_rules"][0]
        skipped = set(rule["skip_if_selected_detail_codes"])
        self.assertIn("OTHER_RECRUITER_DATA_REQUEST", skipped)
        self.assertIn("CUSTOMER_DISPOSAL_REASON_REQUEST", skipped)
        self.assertIn("CUSTOMER_SALES_PERFORMANCE_REQUEST", skipped)
        self.assertIn("CUSTOMER_PERFORMANCE_DETAIL_REQUEST", skipped)

    def test_access_restriction_detail_skips_mcp_route(self) -> None:
        subagent = {
            "agent_code": "PERFORMANCE_FEE",
            "prompt_version": "1.6.0",
            "scenario_code": "ACCESS_RESTRICTION",
            "scenario_name": "개인별 상세정보 조회 제한",
            "detail_scenario_code": "CUSTOMER_DISPOSAL_REASON_REQUEST",
            "detail_scenario_name": "고객 폐기 여부·사유 조회 제한",
            "parameters": {},
            "matches": [
                {
                    "scenario_code": "ACCESS_RESTRICTION",
                    "scenario_name": "개인별 상세정보 조회 제한",
                    "detail_scenario_code": "CUSTOMER_DISPOSAL_REASON_REQUEST",
                    "detail_scenario_name": "고객 폐기 여부·사유 조회 제한",
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
