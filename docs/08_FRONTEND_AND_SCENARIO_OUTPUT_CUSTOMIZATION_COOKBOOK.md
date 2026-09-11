# 프론트·세부 시나리오 Output 실전 가이드

## 외부 출력 원칙

운영 프론트는 `POST /chat` SSE만 사용한다. MCP 원본·arguments·classification과
graph state는 보내지 않는다. debug 요청에서만 `trace` event로 확인한다.

주요 event:

- `request_id`, `session_id`, `thread_id`
- 초기/완성 `messages`
- 답변 `token`
- 입력 요청 `action`
- `sourceDocuments`, `recommendedQuestions`
- `duration`, `end`
- `trace`: `X-Debug-Trace: true`일 때만

## 성공 답변 수정

detail별 [`*_output()`](../app/mcp/scenarios)에서 아래 두 값을 만든다.

```python
return ScenarioMcpOutput(
    data=preprocessed_rows,
    answer=ScenarioAnswer(
        text="사용자에게 보일 답변",
        renderables=[table_or_card],
    ),
)
```

- `data`: 내부 정제·tester 확인용 JSON 구조
- `answer.text`: token과 최종 assistant content
- `answer.renderables`: 최종 assistant metadata의 표·카드·파일

## 무결과 출력

### 답변만 보낼 때

handler에서 `NO_DATA` 결과를 그대로 반환한다. `user_message`가 최종 답변이 된다.

### 재입력 action을 보낼 때

handler에서 결과를 판정해 `ACTION.request()`를 호출한다. 상세는
[17번 문서](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)를 따른다.

## 표 만들기

```python
create_table_renderable(
    code="result-table",
    title="조회 결과",
    format="markdown",
    columns=("항목", "금액"),
    rows=tuple((row["name"], row["amount"]) for row in rows),
)
```

Markdown table은 OUTPUT guardrail을 통과한 뒤 messages metadata에 들어간다.
스타일·폭·스크롤은 서버 Markdown 문자열이 아니라 프론트 table renderer/CSS에서
조정한다.

## action 출력 수정

내부 정의:

- code/message/input/검증: scenario 파일의 `ScenarioActionDefinition`
- 외부 JSON whitelist: `app/streaming.py:build_action_event()`

기본 외부 형식:

```json
[
  {
    "code": "SEARCH_QUERY_REQUIRED",
    "thread_id": "thread-id",
    "message": "다른 검색어를 입력해 주세요.",
    "inputCode": "search_query",
    "label": "검색어",
    "type": "text",
    "required": true
  }
]
```

검증 규칙과 `error`는 있을 때만 추가된다. 새 프론트 필드는 필요성이 확정된 경우에만
`build_action_event()` whitelist에 추가한다.

## 시나리오별 위치

| agent | handler/output 파일 |
|---|---|
| PERFORMANCE_FEE | `app/mcp/scenarios/performance_fee.py` |
| RP | `app/mcp/scenarios/rp.py` |
| QUALIFICATION | `app/mcp/scenarios/qualification.py` |

output handler가 없는 문서검색 detail은 RAG policy와 `app/answers.py`가 문서 답변을
만든다. 고정 안내 detail은 `app/subagents/fixed_responses.py`를 사용한다.

## 변경 체크리스트

1. registry의 detail과 handler/output 연결 확인
2. 실제 MCP raw result를 tester에서 확인
3. detail output에서 필요한 데이터만 선택·정제
4. 빈 결과 정책을 답변/action 중 하나로 명시
5. text와 renderables가 같은 의미인지 확인
6. table OUTPUT guardrail 결과 확인
7. 일반 `/chat`에는 trace가 없는지 확인
8. assistant history metadata에 추천질문/renderables가 필요한 범위만 저장되는지 확인

원본 전처리는 [15번](15_FUNCTION_FIRST_MCP_INPUT_OUTPUT.md), 프론트 전체 계약은
[02번](02_FRONTEND_INPUT_OUTPUT_CONTRACT.md)을 참고한다.
