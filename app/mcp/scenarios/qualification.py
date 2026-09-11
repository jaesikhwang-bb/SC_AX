"""QUALIFICATION 문서 검색부터 RAG 답변까지 담당하는 전용 파이프라인.

RAG 검색어는 프론트 Action으로 받지 않는다. 마스터 보정 질문과 서브에이전트가
세부 시나리오별로 추출한 keywords를 Databricks 하이브리드 검색에 전달한다.
검색 결과 schema 변환, 임계값, reranking, 답변 가능성 판별과 최종 LLM
스트리밍도 이 파일에서 직접 관리한다. 다른 RAG 에이전트의 규칙과 섞지 않는다.
"""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.mcp.models import McpExecutionResult
from app.mcp.scenario_runtime import ScenarioMcpHandlerContext
from app.mcp.scenarios.contracts import (
    ScenarioRagOutput,
    ScenarioRagOutputContext,
)
from app.mcp.scenarios.helpers import keyword_list, text
from app.observability import log_failure_diagnostic, logger
from app.streaming import split_text
from app.subagents.fixed_responses import get_rag_fixed_response


_NO_DOCUMENTS_ANSWER = get_rag_fixed_response("QUALIFICATION", "NO_DOCUMENTS")
_NOT_ANSWERABLE_ANSWER = get_rag_fixed_response(
    "QUALIFICATION",
    "NOT_ANSWERABLE",
)


# ---------------------------------------------------------------------------
# 자격기준 RAG 커스터마이징 지점
# ---------------------------------------------------------------------------
# 별도의 공통 policy/registry에서 가져오지 않는다. 자격기준 검색을 바꿀 때는
# 이 파일의 값과 아래 qualification_document_search()의 payload만 수정한다.
QUALIFICATION_DOCUMENT_CHARACTER_THRESHOLD = 12_000
QUALIFICATION_FIRST_SEARCH_NUM_RESULTS = 5
QUALIFICATION_SECOND_SEARCH_NUM_RESULTS = 30

# 중요: 아래 문자열은 현재 테스트용 기본값이다. 실제 Databricks MCP가 허용하는
# output_type 명칭이 다르면 이 두 줄만 실제 계약값으로 변경한다.
QUALIFICATION_FIRST_SEARCH_OUTPUT_TYPE = "summary"
QUALIFICATION_SECOND_SEARCH_OUTPUT_TYPE = "full"

# 운영 문서번호는 이 dict에 세부 시나리오별로 직접 등록한다.
# 예: "NEW_MEMBER_QUALIFICATION": "128174"
QUALIFICATION_DOCUMENT_NUMBER_BY_DETAIL_CODE: dict[str, str] = {}

QUALIFICATION_RERANKING_ENABLED = True
QUALIFICATION_RERANKING_SCORE_THRESHOLD = 0.50
QUALIFICATION_RERANKING_TOP_N = 10
QUALIFICATION_ANSWERABILITY_CHECK_ENABLED = True

# 세부 시나리오별 검색 점수 임계값도 이 파일에서 직접 관리한다.
QUALIFICATION_RETRIEVAL_SCORE_THRESHOLDS: dict[str, float] = {
    "NEW_MEMBER_QUALIFICATION": 0.60,
    "FOREIGNER_QUALIFICATION": 0.62,
    "MINOR_QUALIFICATION": 0.62,
    "FAMILY_CARD_ISSUANCE_QUALIFICATION": 0.60,
}


async def qualification_document_search(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    """자격기준 1차/2차 MCP payload를 이 함수에서 직접 구성해 호출한다."""

    query = text(context.subagent.parameters.get("rag_query")) or context.refined_query
    keywords = keyword_list(
        context.subagent.parameters.get("keywords"),
        fallback_query=query,
    )
    detail_code = context.subagent.detail_scenario_code.strip().upper()

    # document_number는 운영자가 세부 시나리오에 맞게 직접 매핑할 수 있다.
    # 현재 요청 파라미터나 request_context에 값이 있으면 우선 사용하고, 값이
    # 없을 때는 detail_code를 사용해 filter 형식 자체가 깨지지 않게 한다.
    document_number = text(
        QUALIFICATION_DOCUMENT_NUMBER_BY_DETAIL_CODE.get(detail_code)
    )
    if not document_number:
        document_number = text(context.subagent.parameters.get("document_number"))
    if not document_number:
        document_number = text(context.request_context.get("document_number"))
    if not document_number:
        document_number = detail_code

    # 이 변수의 실제 값에는 역슬래시가 없다. raw HTTP JSON에서 보이는 \"는
    # 문자열 안쪽 따옴표를 표현하기 위한 JSON 표기일 뿐이다.
    filter_value = f'{{"blsm_doc_id":["{document_number}"]}}'

    if context.rag_search_pass == "SECOND":
        # ---------------------- 자격기준 2차 조회 payload ----------------------
        step_code = "QUALIFICATION_DOCUMENT_SEARCH_SECOND"
        arguments: dict[str, Any] = {
            "query": query,
            "keywords": keywords,
            "query_type": "HYBRID",
            "index_name": "qualification_documents",
            "columns": [
                "document_id",
                "title",
                "content",
                "source_uri",
                "updated_at",
            ],
            "output_type": QUALIFICATION_SECOND_SEARCH_OUTPUT_TYPE,
            "num_results": QUALIFICATION_SECOND_SEARCH_NUM_RESULTS,
            "filter": filter_value,
        }
    else:
        # ---------------------- 자격기준 1차 조회 payload ----------------------
        step_code = "QUALIFICATION_DOCUMENT_SEARCH"
        arguments = {
            "query": query,
            "keywords": keywords,
            "query_type": "HYBRID",
            "index_name": "qualification_documents",
            "columns": [
                "document_id",
                "title",
                "content",
                "source_uri",
                "updated_at",
            ],
            "output_type": QUALIFICATION_FIRST_SEARCH_OUTPUT_TYPE,
            "num_results": QUALIFICATION_FIRST_SEARCH_NUM_RESULTS,
            "filter": filter_value,
        }

    logger.info(
        "======== 자격기준 문서 검색 실행\n"
        "세부시나리오=%s\n검색차수=%s\noutput_type=%s\n조회건수=%d\n"
        "검색질문길이=%d\n키워드개수=%d\nMCP인자키=%s\n"
        "filter와 arguments 본문로그=생략",
        detail_code,
        context.rag_search_pass,
        arguments["output_type"],
        arguments["num_results"],
        len(query),
        len(keywords),
        sorted(arguments),
    )
    return await context.call(
        step_code=step_code,
        tool_name="databricks_hybrid_search",
        arguments=arguments,
    )


def qualification_document_character_count(
    execution: McpExecutionResult,
) -> int:
    """자격기준 검색 결과의 모든 ``content`` 본문 글자 수를 합산한다."""

    raw_result = execution.result
    if not isinstance(raw_result, Mapping):
        return 0
    rows = raw_result.get("data")
    if not isinstance(rows, list):
        return 0
    return sum(
        len(str(row.get("content") or ""))
        for row in rows
        if isinstance(row, Mapping)
    )


def qualification_second_search_decision(
    executions: Sequence[tuple[str, McpExecutionResult]],
) -> dict[str, Any]:
    """자격기준 전체 1차 결과를 합산해 2차 조회 여부를 직접 결정한다."""

    detail_character_counts = {
        detail_code: qualification_document_character_count(execution)
        for detail_code, execution in executions
    }
    total_character_count = sum(detail_character_counts.values())
    already_second = any(
        execution.workflow_step_code == "QUALIFICATION_DOCUMENT_SEARCH_SECOND"
        for _, execution in executions
    )
    all_first_searches_succeeded = bool(executions) and all(
        execution.succeeded and execution.outcome == "SUCCESS"
        for _, execution in executions
    )
    should_search_again = (
        all_first_searches_succeeded
        and not already_second
        and total_character_count > QUALIFICATION_DOCUMENT_CHARACTER_THRESHOLD
    )
    return {
        "detail_character_counts": detail_character_counts,
        "total_character_count": total_character_count,
        "character_threshold": QUALIFICATION_DOCUMENT_CHARACTER_THRESHOLD,
        "all_first_searches_succeeded": all_first_searches_succeeded,
        "already_second": already_second,
        "should_search_again": should_search_again,
    }


async def qualification_rag_output(
    context: ScenarioRagOutputContext,
) -> ScenarioRagOutput:
    """모든 자격기준 detail 문서를 합쳐 LLM 답변을 정확히 한 번 만든다."""

    if not context.items:
        raise ValueError("자격기준 RAG batch에는 하나 이상의 검색 결과가 필요합니다.")

    documents: list[dict[str, Any]] = []
    detail_codes: list[str] = []
    for item in context.items:
        detail_code = item.detail_scenario_code.strip().upper()
        logger.info(
            "======== 자격기준 조회 결과별 전처리 시작\n"
            "세부시나리오=%s\nMCP추적ID=%s\nMCP도구=%s",
            detail_code,
            item.execution.request_id,
            item.execution.tool_name,
        )
        retrieval_score_threshold = QUALIFICATION_RETRIEVAL_SCORE_THRESHOLDS.get(
            detail_code
        )
        if retrieval_score_threshold is None:
            raise ValueError(
                "자격기준 RAG 정책이 등록되지 않았습니다: "
                f"detail_scenario_code={detail_code}"
            )
        detail_documents = preprocess_qualification_documents(
            execution=item.execution,
            detail_scenario_code=detail_code,
        )
        detail_documents = _filter_qualification_documents(
            documents=detail_documents,
            threshold=retrieval_score_threshold,
            agent_code=context.agent_code,
            detail_scenario_code=detail_code,
        )
        detail_codes.append(detail_code)
        documents.extend(detail_documents)
        logger.info(
            "======== 자격기준 조회 결과별 전처리 완료\n"
            "세부시나리오=%s\n현재결과유효문서=%d\n누적유효문서=%d",
            detail_code,
            len(detail_documents),
            len(documents),
        )

    if not documents:
        logger.info(
            "======== 자격기준 RAG 고정답변 선택\n"
            "세부시나리오=%s\n사유=전체 검색 결과 중 임계점수 통과 문서 없음",
            detail_codes,
        )
        return ScenarioRagOutput(
            answer=_NO_DOCUMENTS_ANSWER,
            metadata={"reason": "no_retrieval_documents"},
        )

    second_search_used = any(
        item.execution.workflow_step_code
        == "QUALIFICATION_DOCUMENT_SEARCH_SECOND"
        for item in context.items
    )
    reranking_query = _qualification_batch_query(context)
    reranking_service_enabled = bool(getattr(context.reranker, "enabled", True))
    reranking_applied = (
        second_search_used
        and QUALIFICATION_RERANKING_ENABLED
        and reranking_service_enabled
    )
    if reranking_applied:
        documents = await context.reranker.rerank(
            query=reranking_query,
            documents=documents,
            top_n=QUALIFICATION_RERANKING_TOP_N,
            score_threshold=QUALIFICATION_RERANKING_SCORE_THRESHOLD,
        )
        if not documents:
            logger.info(
                "======== 자격기준 RAG 고정답변 선택\n"
                "세부시나리오=%s\n사유=2차 검색 Reranking 통과 문서 없음",
                detail_codes,
            )
            return ScenarioRagOutput(
                answer=_NO_DOCUMENTS_ANSWER,
                metadata={"reason": "no_reranked_documents"},
            )
    else:
        logger.info(
            "======== 자격기준 Reranking 생략\n"
            "세부시나리오=%s\n검색차수=%s\n정책활성화=%s\n"
            "전역서비스활성화=%s\n사유=%s",
            detail_codes,
            "SECOND" if second_search_used else "FIRST",
            QUALIFICATION_RERANKING_ENABLED,
            reranking_service_enabled,
            (
                "1차 검색 결과는 요구사항에 따라 바로 답변 가능성 판별로 전달"
                if not second_search_used
                else "시나리오 정책 또는 전역 Reranking 서비스 비활성화"
            ),
        )

    if QUALIFICATION_ANSWERABILITY_CHECK_ENABLED:
        answerable = await context.answerability.is_answerable(
            query=reranking_query,
            documents=documents,
        )
        if not answerable:
            logger.info(
                "======== 자격기준 RAG 고정답변 선택\n"
                "세부시나리오=%s\n사유=통합 LLM 문서 답변 가능성 판별 false",
                detail_codes,
            )
            return ScenarioRagOutput(
                answer=_NOT_ANSWERABLE_ANSWER,
                metadata={"reason": "not_answerable"},
            )

    logger.info(
        "======== 자격기준 통합 RAG output 준비 완료\n"
        "세부시나리오=%s\n검색차수=%s\nReranking적용=%s\n"
        "최종문서개수=%d\n통합질문길이=%d",
        detail_codes,
        "SECOND" if second_search_used else "FIRST",
        reranking_applied,
        len(documents),
        len(reranking_query),
    )
    return ScenarioRagOutput(
        answer=_stream_qualification_answer(context, documents),
        source_documents=documents,
        metadata={
            "detailScenarioCodes": detail_codes,
            "documentCount": len(documents),
            "documentCharacterCount": sum(
                qualification_document_character_count(item.execution)
                for item in context.items
            ),
            "searchPass": "SECOND" if second_search_used else "FIRST",
            "rerankingApplied": reranking_applied,
        },
    )


def preprocess_qualification_documents(
    *,
    execution: McpExecutionResult,
    detail_scenario_code: str,
) -> list[dict[str, Any]]:
    """자격기준 MCP ``data`` 행을 RAG 문서 계약으로 변환한다.

    Databricks MCP의 실제 컬럼명이 바뀌면 이 함수의 row 필드 매핑만 수정한다.
    다른 서브에이전트와 공통 파서를 공유하지 않아 독립적으로 변경할 수 있다.
    """

    raw_result = execution.result
    if not isinstance(raw_result, Mapping):
        raise ValueError("자격기준 MCP structuredContent는 object 형식이어야 합니다.")
    rows = raw_result.get("data")
    if not isinstance(rows, list):
        raise ValueError(
            "자격기준 MCP structuredContent.data는 list 형식이어야 합니다."
        )

    documents: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            raise ValueError(
                "자격기준 MCP data의 각 문서는 object 형식이어야 합니다: "
                f"index={index}"
            )
        content = str(row.get("content") or "").strip()
        if not content:
            logger.info(
                "======== 자격기준 검색 문서 제외\n"
                "순번=%d\n사유=content 없음",
                index,
            )
            continue
        document_id = str(
            row.get("document_id")
            or f"{execution.request_id}:qualification-document:{index}"
        )
        documents.append(
            {
                "document_id": document_id,
                "title": str(row.get("title") or "자격기준 조회 문서"),
                "source": execution.tool_name,
                "content": content,
                "metadata": {
                    "detail_scenario_code": detail_scenario_code,
                    "mcp_request_id": execution.request_id,
                    "source_uri": row.get("source_uri"),
                    "updated_at": row.get("updated_at"),
                    "score": row.get("score"),
                    "matched_query": row.get("matched_query"),
                    "search_arguments": execution.arguments,
                },
            }
        )

    logger.info(
        "======== 자격기준 MCP 검색 결과 전처리 완료\n"
        "세부시나리오=%s\n조회행=%d\n유효문서=%d\n문서본문로그=생략",
        detail_scenario_code,
        len(rows),
        len(documents),
    )
    return documents


def _filter_qualification_documents(
    *,
    documents: list[dict[str, Any]],
    threshold: float,
    agent_code: str,
    detail_scenario_code: str,
) -> list[dict[str, Any]]:
    """자격기준 검색 점수 정책을 다른 RAG 에이전트와 독립적으로 적용한다."""

    selected: list[dict[str, Any]] = []
    rejected_count = 0
    for document in documents:
        metadata = document.get("metadata")
        score = metadata.get("score") if isinstance(metadata, Mapping) else None
        if (
            isinstance(score, (int, float))
            and not isinstance(score, bool)
            and float(score) >= threshold
        ):
            selected.append(document)
            continue
        rejected_count += 1
    logger.info(
        "======== 자격기준 검색 임계값 적용 완료\n"
        "에이전트=%s\n세부시나리오=%s\n임계점수=%.3f\n"
        "입력문서개수=%d\n통과문서개수=%d\n제외문서개수=%d",
        agent_code,
        detail_scenario_code,
        threshold,
        len(documents),
        len(selected),
        rejected_count,
    )
    return selected


def _qualification_batch_query(context: ScenarioRagOutputContext) -> str:
    """마스터 질문과 모든 detail별 검색 질문·키워드를 하나로 묶는다."""

    request_lines = [
        (
            f"- {item.detail_scenario_code}: {item.query} "
            f"(키워드: {', '.join(item.keywords)})"
        )
        for item in context.items
    ]
    return f"{context.master_query}\n세부 요청:\n" + "\n".join(request_lines)


async def _stream_qualification_answer(
    context: ScenarioRagOutputContext,
    documents: list[dict[str, Any]],
) -> AsyncIterator[str]:
    """자격기준 질문·이력·정제 문서를 LLM에 전달하고 토큰을 그대로 중계한다."""

    document_text = json.dumps(documents, ensure_ascii=False, default=str)
    detail_requests = [
        {
            "detail_scenario_code": item.detail_scenario_code,
            "rag_query": item.query,
            "keywords": list(item.keywords),
        }
        for item in context.items
    ]
    detail_requests_text = json.dumps(
        detail_requests,
        ensure_ascii=False,
        default=str,
    )
    history_text = json.dumps(
        [dict(item) for item in context.chat_history],
        ensure_ascii=False,
        default=str,
    )
    user_text = json.dumps(
        dict(context.request_user),
        ensure_ascii=False,
        default=str,
    )
    human_prompt = (
        f"마스터 보정 사용자 질문:\n{context.master_query}\n\n"
        f"현재 요청 사용자 정보:\n{user_text}\n\n"
        f"현재 자격기준 세부 시나리오 검색 질문과 키워드:\n"
        f"{detail_requests_text}\n\n"
        f"같은 에이전트의 이전 대화 이력:\n{history_text}\n\n"
        f"검색·임계값·Reranking을 통과한 자격기준 문서:\n{document_text}"
    )
    if context.llm is None:
        fallback = (
            "테스트 RAG 답변입니다. 조회된 자격기준 문서를 기반으로 안내합니다. "
            f"참고 문서: {document_text}"
        )
        for chunk in split_text(fallback):
            yield chunk
        return

    logger.info(
        "======== 자격기준 최종 답변 LLM 스트리밍 시작\n"
        "코드위치=app/mcp/scenarios/qualification.py:"
        "_stream_qualification_answer\n엔드포인트=%s\n모델=%s\n"
        "질문길이=%d\n문서개수=%d\n문서JSON길이=%d\n대화이력개수=%d",
        context.endpoint,
        context.model,
        len(context.master_query),
        len(documents),
        len(document_text),
        len(context.chat_history),
    )
    try:
        async for chunk in context.llm.astream(
            [
                SystemMessage(content=context.system_prompt),
                HumanMessage(content=human_prompt),
            ]
        ):
            chunk_text = _message_content_text(getattr(chunk, "content", ""))
            if chunk_text:
                yield chunk_text
    except Exception as exc:
        log_failure_diagnostic(
            stage="자격기준 최종 RAG 답변 LLM 스트리밍",
            code_location=(
                "app/mcp/scenarios/qualification.py:"
                "_stream_qualification_answer"
            ),
            exc=exc,
            likely_cause=(
                "GenOS LLM 연결·인증·모델 오류, 입력 문서 크기 초과 또는 "
                "스트림 도중 연결 종료"
            ),
            corrective_action=(
                "qualification.py의 문서 전처리 결과와 GenOS LLM 설정을 "
                "확인하세요."
            ),
            retry_count=context.max_retries,
            context={
                "detail_scenario_codes": [
                    item.detail_scenario_code for item in context.items
                ],
                "document_count": len(documents),
                "document_json_length": len(document_text),
            },
        )
        raise
    logger.info(
        "======== 자격기준 최종 답변 LLM 스트리밍 완료\n"
        "세부시나리오=%s\n문서개수=%d",
        [item.detail_scenario_code for item in context.items],
        len(documents),
    )


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content) if content is not None else ""
