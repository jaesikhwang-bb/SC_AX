"""서브에이전트 고정답변의 유치조직구분코드 분기 테스트."""

import unittest

from app.subagents.fixed_responses import (
    SUBAGENT_NO_DATA_RESPONSES,
    SubagentFixedResponse,
    get_subagent_fixed_response,
    get_subagent_no_data_response,
)
from app.mcp.models import MCP_NO_DATA_MESSAGE


class SubagentFixedResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.response = SubagentFixedResponse(
            default_message="공통 기본 답변",
            messages_by_recruitment_org_type={
                "11": "일반 모집인 답변",
                "12": "제휴 모집인 답변",
                "13": "복합 모집인 답변",
            },
        )

    def test_registered_recruitment_org_type_uses_specific_message(self) -> None:
        self.assertEqual(self.response.message_for("11"), "일반 모집인 답변")
        self.assertEqual(self.response.message_for("12"), "제휴 모집인 답변")
        self.assertEqual(self.response.message_for("13"), "복합 모집인 답변")

    def test_missing_recruitment_org_type_uses_default_message(self) -> None:
        self.assertEqual(self.response.message_for(None), "공통 기본 답변")
        self.assertEqual(self.response.message_for(""), "공통 기본 답변")

    def test_unknown_recruitment_org_type_uses_default_message(self) -> None:
        self.assertEqual(self.response.message_for("99"), "공통 기본 답변")

    def test_performance_fee_access_restrictions_are_fixed_responses(self) -> None:
        for detail_code in (
            "OTHER_RECRUITER_DATA_REQUEST",
            "CUSTOMER_DISPOSAL_REASON_REQUEST",
            "CUSTOMER_SALES_PERFORMANCE_REQUEST",
            "CUSTOMER_PERFORMANCE_DETAIL_REQUEST",
        ):
            response = get_subagent_fixed_response(
                "PERFORMANCE_FEE",
                detail_code,
            )
            self.assertIsNotNone(response)
            self.assertTrue(response.message_for(None))

    def test_fee_payment_date_has_dedicated_fixed_response(self) -> None:
        response = get_subagent_fixed_response(
            "PERFORMANCE_FEE",
            "FEE_PAYMENT_DATE_GUIDANCE",
        )

        self.assertIsNotNone(response)
        self.assertIn("매월 16일", response.message_for(None))

    def test_customer_restriction_messages_are_selected_by_semantic_detail(self) -> None:
        disposal = get_subagent_fixed_response(
            "PERFORMANCE_FEE",
            "CUSTOMER_DISPOSAL_REASON_REQUEST",
        )
        sales = get_subagent_fixed_response(
            "PERFORMANCE_FEE",
            "CUSTOMER_SALES_PERFORMANCE_REQUEST",
        )
        general = get_subagent_fixed_response(
            "PERFORMANCE_FEE",
            "CUSTOMER_PERFORMANCE_DETAIL_REQUEST",
        )

        self.assertIn("폐기사유", disposal.message_for(None))
        self.assertIn("매출건", sales.message_for(None))
        self.assertIn("환산", general.message_for(None))
        self.assertEqual(len({
            disposal.message_for(None),
            sales.message_for(None),
            general.message_for(None),
        }), 3)

    def test_no_data_message_can_be_customized_per_detail(self) -> None:
        response = get_subagent_no_data_response(
            "PERFORMANCE_FEE",
            "UNREGISTERED_MEMBER_SUMMARY",
        )

        self.assertEqual(response.message_for(None), "미등록건이 없습니다.")
        self.assertIn(
            ("PERFORMANCE_FEE", "UNREGISTERED_MEMBER_SUMMARY"),
            SUBAGENT_NO_DATA_RESPONSES,
        )

    def test_apartment_no_data_requests_a_more_detailed_address(self) -> None:
        response = get_subagent_no_data_response("RP", "APARTMENT_RP_LIST")

        self.assertEqual(
            response.message_for(None),
            "조회된 데이터가 없습니다.",
        )

    def test_unknown_no_data_detail_uses_common_fallback(self) -> None:
        response = get_subagent_no_data_response("TEST", "UNKNOWN")

        self.assertEqual(response.message_for(None), MCP_NO_DATA_MESSAGE)


if __name__ == "__main__":
    unittest.main()
