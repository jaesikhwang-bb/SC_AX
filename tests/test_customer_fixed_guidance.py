"""고객 상세 고정 안내의 프롬프트 등록·무조회 분기 계약을 검증한다."""

import unittest

from app.graph import MasterIntentGraph
from app.subagents.fixed_responses import SUBAGENT_FIXED_RESPONSES
from app.subagents.prompt_loader import SubagentPromptLoader


class CustomerFixedGuidanceTests(unittest.TestCase):
    def test_every_registered_fixed_detail_loads_and_skips_mcp(self):
        """새 detail도 handler 없이 로딩되고 MCP 호출 분기로 가지 않아야 한다."""
        bundles = SubagentPromptLoader().load_all()
        checked = set()
        for agent, bundle in bundles.items():
            for scenario in bundle.manifest["scenarios"]:
                for detail in scenario["details"]:
                    key = (agent, detail["code"])
                    response = SUBAGENT_FIXED_RESPONSES.get(key)
                    if response is None:
                        continue
                    checked.add(key)
                    with self.subTest(agent=agent, detail=detail["code"]):
                        self.assertEqual(detail["parameters"], [])
                        self.assertTrue(response.message_for(None))
                        state = {"subagent": {
                            "agent_code": agent,
                            "prompt_version": bundle.version,
                            "matches": [{
                                "scenario_code": scenario["code"],
                                "scenario_name": scenario["name"],
                                "detail_scenario_code": detail["code"],
                                "detail_scenario_name": detail["name"],
                                "parameters": {},
                            }],
                        }}
                        self.assertNotEqual(MasterIntentGraph._after_subagent(state), "call_mcp")
        self.assertEqual(checked, set(SUBAGENT_FIXED_RESPONSES))

    def test_customer_messages_and_fee_fax_are_distinct(self):
        expected = {
            ("PERFORMANCE_FEE", "CUSTOMER_WITHDRAWAL_REQUEST"): "고객 탈회 이력",
            ("PERFORMANCE_FEE", "CUSTOMER_CONVERSION_REQUEST"): "고객별 환산",
            ("PERFORMANCE_FEE", "CUSTOMER_UNREGISTERED_IDENTITY_REQUEST"): "고객 내역",
            ("RP", "RP_APPLICATION_CANCEL_GUIDANCE"): "취소 불가",
            ("RP", "RP_CUSTOMER_PAYMENT_GUIDANCE"): "고객 자동납부 내역",
            ("RP", "RP_POST_ISSUANCE_APPLICATION_GUIDANCE"): "1588-8700",
            ("QUALIFICATION", "PROVISIONAL_DISPOSITION_GUIDANCE"): "고객 가처분",
        }
        for key, phrase in expected.items():
            self.assertIn(phrase, SUBAGENT_FIXED_RESPONSES[key].message_for(None))
        self.assertNotIn(("PERFORMANCE_FEE", "WITHHOLDING_TAX_FAX_SEND"), SUBAGENT_FIXED_RESPONSES)
