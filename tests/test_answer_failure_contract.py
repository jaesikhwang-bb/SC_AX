"""MCP 오류·무데이터·RAG 무문서 최종 답변 계약 테스트."""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from app.answers import (
    AnswerPromptBundle,
    DefaultAnswerService,
    _result_chat_history,
)
from app.domain import ClassificationType, IntentClassification
from app.graph import MasterResult
from app.mcp.models import (
    MCP_SAFE_ERROR_MESSAGE,
    McpExecutionResult,
)
from app.subagents.models import SubagentResult
from app.subagents.fixed_responses import get_subagent_no_data_response


class AnswerFailureContractTests(unittest.IsolatedAsyncioTestCase):
    def test_legacy_master_result_without_chat_history_uses_empty_history(self) -> None:
        legacy_result = SimpleNamespace()

        self.assertEqual(_result_chat_history(legacy_result), [])

    def _service(self, *, include_details: bool) -> DefaultAnswerService:
        # 아래 테스트는 prepare의 고정답변 조립까지만 실행하므로 외부 LLM이나
        # reranker를 만들지 않고 필요한 설정만 직접 주입한다.
        service = object.__new__(DefaultAnswerService)
        service._include_error_details = include_details
        service._prompt = AnswerPromptBundle(
            version="test",
            rag_system_prompt="test",
            temperature=0,
            agent_response_modes={},
            default_response_mode="fixed_data",
            exception_answers={},
        )
        return service

    async def _text(self, prepared) -> str:
        return "".join([chunk async for chunk in prepared.tokens])

    def _result(
        self,
        *,
        agent_code: str,
        detail_codes: list[str],
        mcp_results: list[McpExecutionResult],
    ) -> MasterResult:
        matches = [
            {
                "scenario_code": "TEST_SCENARIO",
                "scenario_name": "테스트 시나리오",
                "detail_scenario_code": detail_code,
                "detail_scenario_name": detail_code,
                "parameters": {
                    "rag_query": "세부 시나리오 테스트 질문",
                    "keywords": ["테스트", "질문"],
                },
            }
            for detail_code in detail_codes
        ]
        subagent = SubagentResult.model_validate(
            {
                "agent_code": agent_code,
                "prompt_version": "test",
                **matches[0],
                "matches": matches,
            }
        )
        return MasterResult(
            status="PASS",
            thread_id="thread-test",
            classification=IntentClassification(
                refined_query="테스트 질문",
                classification_type=ClassificationType.AGENT,
                agent_code=agent_code,
            ),
            subagent=subagent,
            mcp_results=mcp_results,
        )

    def _mcp(
        self,
        *,
        outcome: str,
        succeeded: bool,
        result=None,
        error=None,
        business_code=None,
    ) -> McpExecutionResult:
        return McpExecutionResult(
            backend="http",
            tool_name="test_tool",
            request_id="mcp-test",
            arguments={},
            succeeded=succeeded,
            outcome=outcome,
            business_code=business_code,
            result=result,
            error=error,
        )

    async def test_any_mcp_error_removes_all_tables_and_returns_one_error(self) -> None:
        success = self._mcp(
            outcome="SUCCESS",
            succeeded=True,
            result={"data": []},
        )
        failure = self._mcp(
            outcome="ERROR",
            succeeded=False,
            error="upstream failed",
            business_code="E500",
        )
        result = self._result(
            agent_code="PERFORMANCE_FEE",
            detail_codes=["PERFORMANCE_SUMMARY_TOTAL", "FEE_ITEM_DETAILS"],
            mcp_results=[success, failure],
        )

        prepared = await self._service(include_details=True).prepare(result)
        text = await self._text(prepared)

        self.assertEqual(prepared.mode, "error")
        self.assertEqual(prepared.renderables, [])
        self.assertEqual(prepared.source_documents, [])
        self.assertIn(MCP_SAFE_ERROR_MESSAGE, text)
        self.assertIn("E500", text)
        self.assertIn("upstream failed", text)

    async def test_production_error_answer_hides_internal_detail(self) -> None:
        failure = self._mcp(
            outcome="ERROR",
            succeeded=False,
            error="secret upstream detail",
        )
        result = self._result(
            agent_code="PERFORMANCE_FEE",
            detail_codes=["PERFORMANCE_SUMMARY_TOTAL"],
            mcp_results=[failure],
        )

        prepared = await self._service(include_details=False).prepare(result)

        self.assertEqual(await self._text(prepared), MCP_SAFE_ERROR_MESSAGE)
        self.assertEqual(prepared.renderables, [])

    async def test_business_code_1001_returns_no_data_without_table(self) -> None:
        no_data = self._mcp(
            outcome="NO_DATA",
            succeeded=True,
            result={"data": []},
            business_code="1001",
        )
        result = self._result(
            agent_code="PERFORMANCE_FEE",
            detail_codes=["PERFORMANCE_SUMMARY_TOTAL"],
            mcp_results=[no_data],
        )

        prepared = await self._service(include_details=True).prepare(result)

        expected = get_subagent_no_data_response(
            "PERFORMANCE_FEE",
            "PERFORMANCE_SUMMARY_TOTAL",
        ).message_for(None)
        self.assertEqual(await self._text(prepared), expected)
        self.assertEqual(prepared.renderables, [])

    async def test_output_counts_all_1001_batches_even_if_outcome_is_success(self) -> None:
        # 집계 계층의 outcome이 실수로 SUCCESS로 전달된 상황도 output에서
        # 개별 batch를 직접 세어 무데이터 고정답변으로 방어한다.
        aggregate = self._mcp(
            outcome="SUCCESS",
            succeeded=True,
            result={
                "data": [],
                "fanout": {"callCount": 2, "noDataCount": 2},
                "batches": [
                    {"outcome": "NO_DATA", "businessCode": "1001"},
                    {"outcome": "NO_DATA", "businessCode": "1001"},
                ],
            },
        )
        result = self._result(
            agent_code="PERFORMANCE_FEE",
            detail_codes=["PERFORMANCE_SUMMARY_TOTAL"],
            mcp_results=[aggregate],
        )

        prepared = await self._service(include_details=True).prepare(result)

        expected = get_subagent_no_data_response(
            "PERFORMANCE_FEE",
            "PERFORMANCE_SUMMARY_TOTAL",
        ).message_for(None)
        self.assertEqual(await self._text(prepared), expected)
        self.assertEqual(prepared.renderables, [])

    async def test_unregistered_no_data_uses_detail_specific_fixed_answer(self) -> None:
        no_data = self._mcp(
            outcome="NO_DATA",
            succeeded=True,
            result={"data": []},
            business_code="1001",
        )
        result = self._result(
            agent_code="PERFORMANCE_FEE",
            detail_codes=["UNREGISTERED_MEMBER_SUMMARY"],
            mcp_results=[no_data],
        )

        prepared = await self._service(include_details=True).prepare(result)

        self.assertEqual(await self._text(prepared), "미등록건이 없습니다.")
        self.assertEqual(prepared.renderables, [])

    async def test_fee_payment_date_finishes_without_mcp_result(self) -> None:
        result = self._result(
            agent_code="PERFORMANCE_FEE",
            detail_codes=["FEE_PAYMENT_DATE_GUIDANCE"],
            mcp_results=[],
        )

        prepared = await self._service(include_details=True).prepare(result)
        text = await self._text(prepared)

        self.assertIn("매월 16일", text)
        self.assertEqual(prepared.renderables, [])

    async def test_empty_rag_documents_return_policy_fixed_answer(self) -> None:
        empty_documents = self._mcp(
            outcome="SUCCESS",
            succeeded=True,
            result={"data": []},
        )
        result = self._result(
            agent_code="RP",
            detail_codes=["APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"],
            mcp_results=[empty_documents],
        )

        prepared = await self._service(include_details=True).prepare(result)
        text = await self._text(prepared)

        self.assertEqual(prepared.mode, "rag")
        self.assertTrue(text)
        self.assertEqual(prepared.source_documents, [])
        self.assertEqual(prepared.renderables, [])

    async def test_rag_answer_receives_refined_query_keywords_history_and_documents(self) -> None:
        class CapturingReranker:
            def __init__(self):
                self.query = None

            async def rerank(self, *, query, documents, **kwargs):
                self.query = query
                return documents

        class Answerable:
            async def is_answerable(self, *, query, documents):
                return True

        class CapturingLlm:
            def __init__(self):
                self.messages = None

            async def astream(self, messages):
                self.messages = messages
                yield SimpleNamespace(content="문서 기반 답변")

        mcp = self._mcp(
            outcome="SUCCESS",
            succeeded=True,
            result={
                "data": [
                    {
                        "document_id": "doc-1",
                        "title": "RP 연결 기준",
                        "content": "RP 연결 제한 기준 문서",
                        "score": 0.99,
                    }
                ]
            },
        ).model_copy(
            update={
                "tool_name": "databricks_hybrid_search",
                "workflow_step_code": "RP_DOCUMENT_SEARCH_SECOND",
                "arguments": {
                    "query": "RP 연결 기준을 알려줘",
                    "keywords": ["RP", "연결 기준"],
                },
            }
        )
        result = self._result(
            agent_code="RP",
            detail_codes=["APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"],
            mcp_results=[mcp],
        )
        result = replace(
            result,
            chat_history=[
                {"role": "user", "content": "RP 신청 방법을 알려줘"},
                {"role": "assistant", "content": "이전 답변"},
            ],
        )
        reranker = CapturingReranker()
        llm = CapturingLlm()
        service = self._service(include_details=True)
        service._reranker = reranker
        service._answerability = Answerable()
        service._llm = llm
        service._endpoint = "test-endpoint"
        service._model = "test-model"
        service._max_retries = 0

        prepared = await service.prepare(result)
        text = await self._text(prepared)

        self.assertEqual(text, "문서 기반 답변")
        self.assertIn("세부 시나리오 테스트 질문", reranker.query)
        self.assertIn("키워드: 테스트, 질문", reranker.query)
        human_prompt = llm.messages[1].content
        self.assertIn("마스터 보정 사용자 질문", human_prompt)
        self.assertIn("현재 RP 문서 검색 질문과 키워드", human_prompt)
        self.assertIn('"테스트"', human_prompt)
        self.assertIn("RP 신청 방법을 알려줘", human_prompt)
        self.assertIn("RP 연결 제한 기준 문서", human_prompt)

    async def test_qualification_uses_its_specialized_rag_output_handler(self) -> None:
        class CapturingReranker:
            async def rerank(self, *, query, documents, **kwargs):
                return documents

        class Answerable:
            async def is_answerable(self, *, query, documents):
                return True

        class CapturingLlm:
            def __init__(self):
                self.messages = None

            async def astream(self, messages):
                self.messages = messages
                yield SimpleNamespace(content="자격기준 전용 답변")

        mcp = self._mcp(
            outcome="SUCCESS",
            succeeded=True,
            result={
                "data": [
                    {
                        "document_id": "qualification-1",
                        "title": "신규회원 입회 기준",
                        "content": "신규회원 자격기준 문서 본문",
                        "score": 0.99,
                    }
                ]
            },
        ).model_copy(
            update={
                "tool_name": "databricks_hybrid_search",
                "arguments": {"document_number": 128174},
            }
        )
        result = self._result(
            agent_code="QUALIFICATION",
            detail_codes=["NEW_MEMBER_QUALIFICATION"],
            mcp_results=[mcp],
        )
        llm = CapturingLlm()
        service = self._service(include_details=True)
        service._reranker = CapturingReranker()
        service._answerability = Answerable()
        service._llm = llm
        service._endpoint = "test-endpoint"
        service._model = "test-model"
        service._max_retries = 0

        prepared = await service.prepare(result)
        text = await self._text(prepared)

        self.assertEqual(text, "자격기준 전용 답변")
        self.assertEqual(prepared.mode, "rag")
        self.assertEqual(
            prepared.source_documents[0]["document_id"],
            "qualification-1",
        )
        self.assertIn(
            "현재 자격기준 세부 시나리오 검색 질문",
            llm.messages[1].content,
        )

    async def test_multiple_qualification_details_create_one_llm_answer(self) -> None:
        class MustNotRerankFirstSearch:
            def __init__(self):
                self.call_count = 0

            async def rerank(self, *, query, documents, **kwargs):
                self.call_count += 1
                return documents

        class CapturingAnswerability:
            def __init__(self):
                self.call_count = 0
                self.documents = None

            async def is_answerable(self, *, query, documents):
                self.call_count += 1
                self.documents = documents
                return True

        class CapturingLlm:
            def __init__(self):
                self.call_count = 0
                self.messages = None

            async def astream(self, messages):
                self.call_count += 1
                self.messages = messages
                yield SimpleNamespace(content="통합 자격기준 답변")

        first = self._mcp(
            outcome="SUCCESS",
            succeeded=True,
            result={
                "data": [
                    {
                        "document_id": "foreigner-1",
                        "title": "외국인 기준",
                        "content": "외국인 자격기준 문서",
                        "score": 0.99,
                    }
                ]
            },
        ).model_copy(
            update={
                "request_id": "qualification-foreigner",
                "tool_name": "databricks_hybrid_search",
                "workflow_step_code": "QUALIFICATION_DOCUMENT_SEARCH",
            }
        )
        second = self._mcp(
            outcome="SUCCESS",
            succeeded=True,
            result={
                "data": [
                    {
                        "document_id": "minor-1",
                        "title": "미성년자 기준",
                        "content": "미성년자 자격기준 문서",
                        "score": 0.99,
                    }
                ]
            },
        ).model_copy(
            update={
                "request_id": "qualification-minor",
                "tool_name": "databricks_hybrid_search",
                "workflow_step_code": "QUALIFICATION_DOCUMENT_SEARCH",
            }
        )
        result = self._result(
            agent_code="QUALIFICATION",
            detail_codes=[
                "FOREIGNER_QUALIFICATION",
                "MINOR_QUALIFICATION",
            ],
            mcp_results=[first, second],
        )
        reranker = MustNotRerankFirstSearch()
        answerability = CapturingAnswerability()
        llm = CapturingLlm()
        service = self._service(include_details=True)
        service._reranker = reranker
        service._answerability = answerability
        service._llm = llm
        service._endpoint = "test-endpoint"
        service._model = "test-model"
        service._max_retries = 0

        prepared = await service.prepare(result)
        text = await self._text(prepared)

        self.assertEqual(text, "통합 자격기준 답변")
        self.assertEqual(reranker.call_count, 0)
        self.assertEqual(answerability.call_count, 1)
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(len(answerability.documents), 2)
        human_prompt = llm.messages[1].content
        self.assertIn("FOREIGNER_QUALIFICATION", human_prompt)
        self.assertIn("MINOR_QUALIFICATION", human_prompt)
        self.assertIn("외국인 자격기준 문서", human_prompt)
        self.assertIn("미성년자 자격기준 문서", human_prompt)


if __name__ == "__main__":
    unittest.main()
