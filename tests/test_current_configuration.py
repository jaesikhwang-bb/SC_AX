"""활성 prompt와 실행 registry가 현재 지원 범위에서 일치하는지 검증한다."""

import unittest

from app.answers import AnswerPromptLoader
from app.mcp.scenarios.rp import (
    RP_DOCUMENT_NUMBER_BY_DETAIL_CODE,
    RP_RAG_DETAIL_CODES,
)
from app.prompt_loader import PromptBundleLoader
from app.subagents.prompt_loader import SubagentPromptLoader
from app.subagents.router import _create_output_model, _normalize_parameters
from app.mcp.scenarios.registry import get_scenario_handler_spec


class CurrentConfigurationTests(unittest.TestCase):
    def test_answerability_accepts_semantically_equivalent_user_phrases(self) -> None:
        """동일 업무의 표현 차이·오탈자를 답변 불가로 과잉 판정하지 않는다."""

        prompt = AnswerPromptLoader().load()

        # AnswerPromptLoader의 실행 버전은 manifest 내부 배포 버전이 아니라
        # 활성 디렉터리 이름(v1)을 사용한다.
        self.assertEqual(prompt.version, "v1")
        self.assertIn("맞춤법·띄어쓰기", prompt.answerability_system_prompt)
        self.assertIn(
            "`자동이체`, `자동납부`, `자동이체 납부`",
            prompt.answerability_system_prompt,
        )
        self.assertIn(
            "아파트 자동이체 납부 신청 어떻게해?",
            prompt.answerability_system_prompt,
        )
        self.assertIn(
            "핵심 요청에 직접 사용할 수 있는 사실·기준·절차 중 하나",
            prompt.answerability_system_prompt,
        )

    def test_master_codes_include_planned_agents_and_enabled_subagents_are_subset(self) -> None:
        master_codes = set(PromptBundleLoader().load().agent_codes)
        subagent_codes = set(SubagentPromptLoader().load_all())

        self.assertEqual(
            master_codes,
            {
                "PERFORMANCE_FEE",
                "FEE_POLICY",
                "QUALIFICATION",
                "RP",
                "PRODUCT_GUIDE",
                "TABLET",
            },
        )
        self.assertEqual(
            subagent_codes,
            {"PERFORMANCE_FEE", "RP", "QUALIFICATION"},
        )
        self.assertTrue(subagent_codes.issubset(master_codes))

    def test_active_manifests_do_not_declare_action_or_mcp_workflow(self) -> None:
        for agent_code, bundle in SubagentPromptLoader().load_all().items():
            for scenario in bundle.manifest.get("scenarios", []):
                for detail in scenario.get("details", []):
                    with self.subTest(
                        agent_code=agent_code,
                        detail_code=detail.get("code"),
                    ):
                        self.assertNotIn("interaction", detail)
                        self.assertNotIn("mcp_workflow", detail)

    def test_removed_performance_fee_details_are_not_executable(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="performance-fee")
        active_details = {
            str(detail["code"])
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }
        removed_details = {
            "UNREGISTERED_MEMBER_HANDOFF_DETAIL",
            "DISPOSAL_FEE_CUSTOMER_DETAIL",
            "FEE_TAX_NET_PAYMENT",
            "FEE_12_MONTH_TREND",
            "PERFORMANCE_FEE_INFORMATION_INQUIRY",
        }

        self.assertTrue(removed_details.isdisjoint(active_details))
        for detail_code in removed_details:
            self.assertIsNone(
                get_scenario_handler_spec("PERFORMANCE_FEE", detail_code)
            )

    def test_rag_details_extract_keywords_without_search_action(self) -> None:
        bundles = SubagentPromptLoader().load_all()
        rag_details = {
            "QUALIFICATION": {
                "NEW_MEMBER_QUALIFICATION",
                "FOREIGNER_QUALIFICATION",
                "MINOR_QUALIFICATION",
                "FAMILY_CARD_ISSUANCE_QUALIFICATION",
            },
            "RP": {
                "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE",
                "CITY_GAS_AUTOPAY_GUIDE",
                "SOCIAL_INSURANCE_AUTOPAY_GUIDE",
                "ELECTRICITY_TV_FEE_AUTOPAY_GUIDE",
                "SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE",
                "PAYMENT_NOTIFICATION_SERVICE_GUIDE",
                "PAYMENT_DATE_CREDIT_PERIOD_GUIDE",
            },
        }
        for agent_code, detail_codes in rag_details.items():
            bundle = bundles[agent_code]
            self.assertEqual(
                bundle.manifest["parameter_definitions"]["keywords"]["value_type"],
                "string_list",
            )
            for scenario in bundle.manifest["scenarios"]:
                for detail in scenario["details"]:
                    if detail["code"] not in detail_codes:
                        continue
                    self.assertIn("rag_query", detail["parameters"])
                    self.assertIn("keywords", detail["parameters"])
                    self.assertNotIn("search_query", detail["parameters"])

    def test_rag_keywords_are_a_structured_string_array(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="rp")
        output_model = _create_output_model(bundle)

        result = output_model.model_validate(
            {
                "matches": [
                    {
                        "scenario_code": "RP_DOCUMENTS",
                        "detail_scenario_code": (
                            "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"
                        ),
                        "parameters": {
                            "rag_query": "RP 연결 제한 기준을 알려줘",
                            "keywords": ["RP", "연결 제한"],
                            "address": None,
                            "closing_year_month": None,
                            "reference_date": None,
                        },
                    }
                ]
            }
        )

        self.assertEqual(
            result.matches[0].parameters.keywords,
            ["RP", "연결 제한"],
        )

    def test_rp_has_seven_document_details_and_document_mappings(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="rp")
        document_scenario = next(
            scenario
            for scenario in bundle.manifest["scenarios"]
            if scenario["code"] == "RP_DOCUMENTS"
        )
        manifest_codes = {
            str(detail["code"])
            for detail in document_scenario["details"]
        }

        self.assertEqual(len(manifest_codes), 7)
        self.assertEqual(manifest_codes, set(RP_RAG_DETAIL_CODES))
        self.assertEqual(
            manifest_codes,
            set(RP_DOCUMENT_NUMBER_BY_DETAIL_CODE),
        )
        self.assertTrue(
            all(RP_DOCUMENT_NUMBER_BY_DETAIL_CODE[code] for code in manifest_codes)
        )

    def test_rp_prompt_keeps_document_guides_separate_from_data_lookups(self) -> None:
        """RP 문서 질문이 아파트·복합환산 조회로 끌려가지 않도록 검증한다."""

        bundle = SubagentPromptLoader().load_one(directory="rp")
        details = {
            str(detail["code"]): detail
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }

        # 세부 설명 뒤에 최종 판정 규칙을 배치해 마지막 지시가 분류표가 되게 한다.
        self.assertEqual(bundle.files[-1], "system.md")
        self.assertIn(
            "도시가스 자동납부 신청절차를 알려줘",
            bundle.system_prompt,
        )
        self.assertIn(
            "CITY_GAS_AUTOPAY_GUIDE만 선택",
            bundle.system_prompt,
        )
        self.assertIn(
            "아파트 자동납부 신청 방법 및 처리 절차를 알려줘",
            bundle.system_prompt,
        )
        self.assertIn(
            "실제 수치 조회 표현이 질문에 명확히 있을 때만 선택",
            bundle.system_prompt,
        )

        score_questions = " ".join(
            details["COMPOSITE_CONVERSION_SCORE"]["recommended_questions"]
        )
        excluded_questions = " ".join(
            details["COMPOSITE_CONVERSION_EXCLUDED"]["recommended_questions"]
        )
        self.assertNotIn("제외", score_questions)
        self.assertNotIn("미반영", score_questions)
        self.assertNotIn("환산점수를 조회", excluded_questions)

    def test_rp_uses_flat_enum_schema_instead_of_ten_way_one_of(self) -> None:
        """RP 10분기 discriminator가 모델 선택을 왜곡하지 않도록 검증한다."""

        bundle = SubagentPromptLoader().load_one(directory="rp")
        schema = _create_output_model(bundle).model_json_schema()
        items = schema["properties"]["matches"]["items"]

        self.assertNotIn("oneOf", items)
        self.assertNotIn("discriminator", items)
        self.assertIn("$ref", items)
        match_schema = schema["$defs"]["RPFlatMatch"]
        detail_schema = match_schema["properties"]["detail_scenario_code"]
        self.assertEqual(len(detail_schema["enum"]), 13)

    def test_rp_structured_output_accepts_multiple_document_details(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="rp")
        output_model = _create_output_model(bundle)

        result = output_model.model_validate(
            {
                "matches": [
                    {
                        "scenario_code": "RP_DOCUMENTS",
                        "detail_scenario_code": "CITY_GAS_AUTOPAY_GUIDE",
                        "parameters": {
                            "rag_query": "도시가스 자동납부 기준을 알려줘",
                            "keywords": ["도시가스", "자동납부"],
                            "address": None,
                            "closing_year_month": None,
                            "reference_date": None,
                        },
                    },
                    {
                        "scenario_code": "RP_DOCUMENTS",
                        "detail_scenario_code": (
                            "ELECTRICITY_TV_FEE_AUTOPAY_GUIDE"
                        ),
                        "parameters": {
                            "rag_query": "전기요금 자동납부 기준을 알려줘",
                            "keywords": ["전기요금", "자동납부"],
                            "address": None,
                            "closing_year_month": None,
                            "reference_date": None,
                        },
                    },
                ]
            }
        )

        self.assertEqual(len(result.matches), 2)

    def test_qualification_removes_income_detail_and_defaults_member_type(self) -> None:
        bundle = SubagentPromptLoader().load_one(directory="qualification")
        details = {
            detail["code"]: detail
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }

        self.assertNotIn("INCOME_PROOF_ACCEPTANCE_CRITERIA", details)
        normalized = _normalize_parameters(
            raw={
                "member_category": None,
                "rag_query": "소득증빙 인정서류를 알려줘",
                "keywords": ["소득증빙"],
            },
            manifest=bundle.manifest,
            detail=details["NEW_MEMBER_QUALIFICATION"],
        )

        self.assertEqual(normalized["member_category"], "NEW_MEMBER")


if __name__ == "__main__":
    unittest.main()
