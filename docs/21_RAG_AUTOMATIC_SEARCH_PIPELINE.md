# Databricks RAG 자동 검색·Reranking·대화 이력 답변

## 1. 최종 실행 흐름

RP의 7개 문서 세부 시나리오와 QUALIFICATION의 네 문서 세부 시나리오는
사용자에게 검색어를 다시 입력받지 않는다. 즉 `action`과 `humanInput` 왕복이
없다.

```text
원본 질문 + 같은 agent의 Redis history
→ 마스터 LLM: 오타·멀티턴 문맥을 반영한 refined_query
→ 서브에이전트 LLM: detail 1~N개 + detail별 rag_query + keywords[]
→ 모든 RAG detail의 Databricks 1차 hybrid search 5건
→ 모든 detail의 content 글자 수 합산
→ 합계가 12,000자 이하이면 1차 결과 유지 및 Reranking 생략
→ 합계가 12,000자를 초과하면 모든 RAG detail을 각각 30건으로 2차 검색
→ 2차 결과 전체 검색 점수 필터 및 선택적 GenOS Reranking
→ 전체 문서에 대한 답변 가능성 LLM 판별 1회
→ refined_query + 모든 rag_query/keywords + 같은 agent의 이력 + 최종 문서
→ 최종 답변 LLM 1회 streaming
```

문서가 없으면 RAG LLM을 호출하지 않고 detail별 `no_documents_answer`를 반환한다.
MCP·파싱·reranker·답변 가능성 판별 중 오류가 나면 테이블이나 부분 답변을 만들지
않고 공통 안전 오류답변으로 전환한다.

## 2. 서브에이전트 구조화 출력

RAG detail의 manifest는 다음 배열 타입을 사용한다.

```yaml
parameter_definitions:
  rag_query:
    description: "현재 detail 요청만 분리한 완결된 검색 질문"
  keywords:
    value_type: "string_list"
    min_items: 1
    max_items: 10
    description: "세부 시나리오 검색 핵심어"
```

detail에는 `rag_query`와 `keywords`만 선언한다. `search_query`나
interaction/action은 선언하지 않는다.

```yaml
details:
  - code: "CITY_GAS_AUTOPAY_GUIDE"
    parameters:
      - "rag_query"
      - "keywords"
```

LLM 구조화 출력 예시는 다음과 같다.

```json
{
  "matches": [
    {
      "scenario_code": "RP_DOCUMENTS",
      "detail_scenario_code": "CITY_GAS_AUTOPAY_GUIDE",
      "parameters": {
        "rag_query": "도시가스 자동납부 연결 제한 기준을 알려줘",
        "keywords": ["도시가스", "자동납부", "연결 제한"]
      }
    }
  ]
}
```

배열 스키마 생성과 정규화 위치는
[`app/subagents/router.py`](../app/subagents/router.py)다. keywords가 비정상적으로
비어도 [`keyword_list()`](../app/mcp/scenarios/helpers.py)가 refined query에서
fallback 키워드를 만들기 때문에 Action을 발생시키지 않는다.

## 3. 하이브리드 검색 payload

QUALIFICATION은 [`qualification_document_search()`](../app/mcp/scenarios/qualification.py),
RP는 [`rp_document_search()`](../app/mcp/scenarios/rp.py)에서 직접 수정한다.

```python
query = (
    text(context.subagent.parameters.get("rag_query"))
    or context.refined_query
)
keywords = keyword_list(
    context.subagent.parameters.get("keywords"),
    fallback_query=query,
)

document_number = "128174"  # detail code별 실제 문서번호로 매핑
filter_value = f'{{"blsm_doc_id":["{document_number}"]}}'

if context.rag_search_pass == "SECOND":
    arguments = {
        "query": query,
        "keywords": keywords,
        "output_type": SECOND_SEARCH_OUTPUT_TYPE,
        "num_results": SECOND_SEARCH_NUM_RESULTS,
        "filter": filter_value,
    }
else:
    arguments = {
        "query": query,
        "keywords": keywords,
        "output_type": FIRST_SEARCH_OUTPUT_TYPE,
        "num_results": FIRST_SEARCH_NUM_RESULTS,
        "filter": filter_value,
    }

return await context.call(
    step_code=step_code,
    tool_name="databricks_hybrid_search",
    arguments=arguments,
)
```

1차와 2차 payload는 각 handler의 `if/else` 블록에 별도 dict로 작성되어 있다.
실제 Databricks MCP 계약에서 `output_type`, `num_results`, keywords 필드명이나
filter 형식이 달라지면 해당 블록을 직접 변경한다. 프롬프트나 공통 MCP transport를
수정할 필요가 없다.

`filter_value` 자체는 `{"blsm_doc_id":["128174"]}`이며 역슬래시를 포함하지
않는다. HTTP raw JSON에서 `\"`로 보이는 것은 바깥 JSON 문자열 안의 따옴표를
표현하는 정상적인 JSON escape다. 서버가 바깥 JSON을 한 번 파싱하면 MCP tool이
받는 값은 다시 역슬래시 없는 원래 문자열이다.

LangGraph는 모든 RAG detail handler의 1차 검색이 끝난 다음 요청 전체의 문서
크기를 판정한다.

1. 선택된 모든 RAG detail을 `first_search_num_results` 건씩 검색한다.
2. 모든 결과의 `structuredContent.data[].content`에 `len()`을 적용해 합산한다.
3. 전체 합계가 `document_character_threshold` 이하이면 모든 1차 결과를 유지한다.
4. 전체 합계가 임계값을 초과하면 같은 query·keywords·filter를 유지하고 모든
   RAG detail을 `second_search_num_results` 건씩 한 번 더 검색한다.
5. 2차 검색을 실행했다면 답변용 결과를 전부 2차 결과로 교체한다. 1차 결과는
   `mcp_workflow_results`에 추적용으로 보존한다.

이 동작은 네트워크 오류 재시도가 아니라 LLM context 크기 제어를 위한 조건부
재조회이며 최대 한 번만 실행된다. 기본값은 두 agent 모두 1차 5건, 12,000자 초과,
2차 detail별 30건이다. RP는 `rp.py` 상단의 `RP_*` 상수, 자격기준은
`qualification.py` 상단의 `QUALIFICATION_*` 상수에서 각각 수정한다. 로그의 `세부글자수`,
`전체글자수`, `글자수임계값`, `2차검색필요`, `최종답변결과`로 실행 결과를 확인한다.

## 4. 검색 문서와 Reranking

QUALIFICATION과 RP는 각각 전용 파일의 RAG output handler가 다음 순서를 전부
직접 처리한다.

- QUALIFICATION: [`qualification_rag_output()`](../app/mcp/scenarios/qualification.py)
- RP: [`rp_document_rag_output()`](../app/mcp/scenarios/rp.py)

1. 모든 RAG detail의 최종 선택 `structuredContent.data[]`를 agent 문서 객체로 변환
2. detail별 retrieval score 임계값 적용
3. 문서가 0건이면 `no_documents_answer`
4. 1차 결과이면 Reranking 없이 전체 문서를 answerability LLM에 전달
5. 2차 결과이고 `reranking_enabled=true`이면 전체 문서를 GenOS reranker에 전달
6. 2차 Reranking score와 `top_n` 적용
7. 선택적으로 전체 문서 answerability LLM 판별 1회
8. 모든 detail의 질문·키워드·문서를 최종 답변 LLM에 한 번 전달해 스트리밍

QUALIFICATION의 컬럼 매핑과 임계값은 `preprocess_qualification_documents()`와
`QUALIFICATION_RETRIEVAL_SCORE_THRESHOLDS`에서 바꾼다. RP는 `preprocess_rp_documents()`와
`rp.py` 상단의 `RP_*` 설정에서 바꾼다. [`app/answers.py`](../app/answers.py)는
같은 agent의 모든 RAG detail을 하나의 batch context로 전용 RAG output handler에
전달하고 결과를 SSE 응답에 조합할 뿐이며, agent별 문서 스키마나 정책을 알지 않는다.
실제 reranker 연결은 `.env`의 다음 값으로 제어한다.

```dotenv
RERANKING_ENABLED=true
RERANKING_SERVING_ID=226
RERANKING_MODEL=bge-reranker-v2-m3
RERANKING_BEARER_TOKEN=<secret>
RERANKING_ENDPOINT_PATH=rerank
```

`RERANKING_ENABLED=false`이면 2차 검색이어도 외부 reranker HTTP 호출을 생략한다.
에이전트별로 끄려면 RP의 `RP_RERANKING_ENABLED` 또는 Qualification의
`QUALIFICATION_RERANKING_ENABLED`를 `False`로
변경한다. 1차 검색 결과는 설정과 관계없이 항상 Reranking을 생략한다.

## 5. 최종 답변 LLM 입력

최종 Human prompt에는 다음 네 블록을 넣는다.

```text
마스터 보정 사용자 질문
모든 세부 시나리오 검색 질문
모든 세부 시나리오 검색 키워드
같은 에이전트의 이전 대화 이력
최종 선택된 하이브리드 검색 및 선택적 Reranking 통과 문서
```

대화 이력은 [`_persist_user_message()`](../app/graph.py)에서 현재 질문을 저장하기
직전에 최종 `agent_code` 범위로 다시 조회한다. 따라서 프론트가 다른 agent를
선택했다가 HITL 승인으로 전환해도 다른 서브에이전트 이력이 섞이지 않는다.
Redis key나 저장 형식은 변경하지 않고 기존 history 조회 API만 재사용한다.

과거 대화는 생략된 문맥을 해석하는 보조정보일 뿐 업무 사실의 근거가 아니다.
업무 답변 근거는 1차 검색 점수 필터 또는 2차 검색의 선택적 Reranking을 통과한
문서로 제한한다. 이 규칙은
[`prompts/answer-generation/v1/rag/system.md`](../prompts/answer-generation/v1/rag/system.md)에
있다.

## 6. 운영 커스터마이징 순서

새 RAG detail을 추가할 때는 다음 순서로 작업한다.

1. subagent manifest에 detail과 `keywords` parameter 등록
2. system/scenario prompt에 detail 선택 규칙과 keyword 추출 규칙 추가
3. `app/mcp/scenarios/<agent>.py`에 hybrid search handler 작성
4. 같은 파일에 문서 전처리, detail 정책, reranking, answerability와 최종 답변
   스트림을 담당하는 전용 RAG output handler 작성
5. `app/mcp/scenarios/registry.py`의 detail에 `rag_output_handler`와 안정적인
   `rag_output_handler_code` 연결
6. 실제 MCP `structuredContent.data` 컬럼을 해당 에이전트 파일의 문서 전처리
   함수에서 매핑
7. 무문서, 낮은 검색점수, 낮은 reranking 점수, 오류, 정상 답변을 각각 테스트

새 RAG 에이전트도 QUALIFICATION 방식으로 전용 handler를 등록하면 공통
`answers.py`의 문서 schema나 다른 에이전트 정책을 수정할 필요가 없다.

RAG detail에는 검색어 Action을 추가하지 않는다. 검색 품질이 낮으면 프론트 입력을
다시 받는 대신 prompt의 keywords 규칙, Databricks query/filter, 검색 임계값,
reranking 정책을 조정한다.
