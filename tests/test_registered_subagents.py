"""registry에 추가된 RP·QUALIFICATION 서브에이전트 계약을 검증한다."""

import unittest
from datetime import date

from app.subagents.prompt_loader import SubagentPromptLoader
from app.subagents.router import _create_output_model, _normalize_parameters


class RegisteredSubagentPromptTest(unittest.TestCase):
    """새 프롬프트가 기존 동적 서브에이전트 엔진과 호환되는지 확인한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundles = SubagentPromptLoader().load_all()

    def test_registry_loads_all_active_subagents(self) -> None:
        self.assertEqual(
            {"PERFORMANCE_FEE", "RP", "QUALIFICATION"},
            set(self.bundles),
        )

    def test_rp_manifest_and_structured_output_schema(self) -> None:
        bundle = self.bundles["RP"]
        details = {
            detail["code"]: detail
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }
        self.assertEqual(
            {
                "RP_DOCUMENT_SEARCH",
                "APARTMENT_RP_LIST",
                "COMPOSITE_CONVERSION_SCORE",
                "COMPOSITE_CONVERSION_EXCLUDED",
            },
            set(details),
        )

        # 모든 파라미터 필드를 명시하는 엄격한 JSON Schema 결과가 실제
        # Pydantic 모델로 검증되는지 확인한다.
        output_model = _create_output_model(bundle)
        structured = output_model.model_validate(
            {
                "matches": [{
                    "scenario_code": "APARTMENT_MANAGEMENT_FEE_RP_ELIGIBILITY",
                    "detail_scenario_code": "APARTMENT_RP_LIST",
                    "parameters": {
                        "address": "잠실동",
                        "closing_year_month": None,
                        "reference_date": None,
                    },
                }],
            }
        )
        self.assertEqual("잠실동", structured.matches[0].parameters.address)

        normalized = _normalize_parameters(
            raw=structured.matches[0].parameters.model_dump(),
            manifest=bundle.manifest,
            detail=details["APARTMENT_RP_LIST"],
            today=date(2026, 7, 31),
        )
        self.assertEqual(
            {
                "address": "잠실동",
                "closing_year_month": None,
                "reference_date": None,
            },
            normalized,
        )

        composite_normalized = _normalize_parameters(
            raw={
                "address": None,
                "closing_year_month": None,
                "reference_date": None,
            },
            manifest=bundle.manifest,
            detail=details["COMPOSITE_CONVERSION_EXCLUDED"],
            today=date(2026, 7, 31),
        )
        self.assertEqual(
            "202607",
            composite_normalized["closing_year_month"],
        )

    def test_rp_overlap_contract_is_composite_conversion_only(self) -> None:
        """RP 공통 업무가 수수료로 되돌아가는 회귀를 방지한다."""

        bundle = self.bundles["RP"]
        self.assertIn("COMPOSITE_CONVERSION_SCORE", bundle.system_prompt)
        self.assertIn("COMPOSITE_CONVERSION_EXCLUDED", bundle.system_prompt)
        self.assertNotIn("PERFORMANCE_SUMMARY_TOTAL", bundle.system_prompt)
        self.assertNotIn("FEE_ITEM_DETAILS", bundle.system_prompt)
        self.assertNotIn("FEE_TAX_NET_PAYMENT", bundle.system_prompt)
        self.assertNotIn("FEE_12_MONTH_TREND", bundle.system_prompt)
        self.assertNotIn("WITHHOLDING_TAX", bundle.system_prompt)

    def test_qualification_scenarios_and_parameter_schema(self) -> None:
        bundle = self.bundles["QUALIFICATION"]
        details = {
            detail["code"]: detail
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        }
        self.assertEqual(
            {
                "NEW_MEMBER_QUALIFICATION",
                "FOREIGNER_QUALIFICATION",
                "MINOR_QUALIFICATION",
                "FAMILY_CARD_ISSUANCE_QUALIFICATION",
                "INCOME_PROOF_ACCEPTANCE_CRITERIA",
                "PROVISIONAL_DISPOSITION_INQUIRY",
            },
            set(details),
        )
        output_model = _create_output_model(bundle)
        structured = output_model.model_validate(
            {
                "matches": [{
                    "scenario_code": "PERSONAL_MEMBER_QUALIFICATION",
                    "detail_scenario_code": "FOREIGNER_QUALIFICATION",
                    "parameters": {
                        "member_category": "FOREIGNER",
                        "restricted_request_type": None,
                    },
                }],
            }
        )
        normalized = _normalize_parameters(
            raw=structured.matches[0].parameters.model_dump(),
            manifest=bundle.manifest,
            detail=details["FOREIGNER_QUALIFICATION"],
            today=date(2026, 7, 31),
        )
        self.assertEqual(
            {
                "member_category": "FOREIGNER",
                "restricted_request_type": None,
            },
            normalized,
        )

        # 선택된 세부 시나리오가 외국인이면 LLM이 null을 반환해도 manifest가
        # FOREIGNER 코드로 확정한다.
        fallback = _normalize_parameters(
            raw={
                "member_category": None,
                "restricted_request_type": None,
            },
            manifest=bundle.manifest,
            detail=details["FOREIGNER_QUALIFICATION"],
            today=date(2026, 7, 31),
        )
        self.assertEqual("FOREIGNER", fallback["member_category"])

        with self.assertRaises(ValueError):
            output_model.model_validate(
                {
                    "matches": [{
                        "scenario_code": "PERSONAL_MEMBER_QUALIFICATION",
                        "detail_scenario_code": "NEW_MEMBER_QUALIFICATION",
                        "parameters": {
                            "member_category": "임의값",
                            "restricted_request_type": None,
                        },
                    }],
                }
            )

    def test_every_registered_detail_has_common_mcp_contract(self) -> None:
        details = [
            detail
            for bundle in self.bundles.values()
            for scenario in bundle.manifest["scenarios"]
            for detail in scenario["details"]
        ]
        self.assertEqual(21, len(details))
        for detail in details:
            with self.subTest(detail=detail["code"]):
                self.assertEqual("test_tool", detail["mcp"]["tool_name"])
                self.assertEqual(
                    "data1",
                    detail["mcp"]["arguments"]["param1"]["value"],
                )
                self.assertEqual(
                    "data2",
                    detail["mcp"]["arguments"]["param2"]["value"],
                )
