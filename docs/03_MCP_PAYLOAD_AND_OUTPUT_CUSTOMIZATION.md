# MCP 요청·응답·최종 출력 커스터마이징

## 현재 기준

활성 detail의 MCP tool과 arguments는 YAML이 아니라
[`app/mcp/scenarios`](../app/mcp/scenarios)의 Python handler에서 정한다. 연결은
[`SCENARIO_HANDLER_REGISTRY`](../app/mcp/scenarios/registry.py)에 등록한다.

```text
detail 선택
→ registry handler
→ context.call/call_many/paginate
→ GenosMcpToolExecutor
→ result.structuredContent 파싱
→ detail output handler
→ answer_text + renderables
→ /chat token/messages SSE
```

## MCP 입력을 바꾸는 위치

```python
async def sample(context):
    return await context.call(
        step_code="SAMPLE_LOOKUP",
        tool_name="sample_tool",
        arguments={
            "bearerToken": context.request_context.get("access_token", ""),
            "employeeId": context.employee_id,
            "sessionId": context.session_id,
            "threadId": context.thread_id,
            "deptCode": context.request_context.get("user", {}).get(
                "deptcode", ""
            ),
            "query": (
                context.subagent.parameters.get("rag_query")
                or context.refined_query
            ),
            "keywords": context.subagent.parameters.get("keywords", []),
        },
    )
```

사용 가능한 문맥:

| 값 | 의미 |
|---|---|
| `context.subagent.parameters` | LLM 추출 + action 재입력으로 갱신된 detail parameter |
| `context.request_context` | 현재 HTTP 요청의 token, endpoint, user |
| `context.employee_id` | 사용자 사번 또는 session 기반 익명 ID |
| `context.session_id` | 대화 이력 범위 |
| `context.thread_id` | 현재 질문/HITL 흐름 |
| `context.refined_query` | 마스터가 오타·문맥을 반영한 보정 질문 |

HITL 재개 때 access token은 Redis에서 복원하지 않는다. 프론트가 재개 요청에도
Authorization Bearer token을 보내야 한다.

## 호출 형태

- 한 번: `context.call()`
- 같은 tool을 N번: `context.call_many()`
- next key 페이지: `context.paginate()`
- 다른 tool 순차 호출: 같은 handler에서 호출 결과를 다음 arguments에 사용
- MCP 뒤 action: `ACTION.require()` 또는 `ACTION.request()`

상세 코드는 [14번](14_SEQUENTIAL_MCP_WORKFLOW.md), action 정책은
[17번](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)을 따른다.

## 운영계 응답 파싱

공통 parser는 [`app/mcp/client.py`](../app/mcp/client.py)에 있다. 현재 허용하는
업무 데이터는 JSON-RPC `result.structuredContent` 객체 하나다.

```json
{
  "jsonrpc": "2.0",
  "result": {
    "structuredContent": {
      "data": [
        {"objId": "artNm", "objNm": "유치자명", "objVal": "홍길동"}
      ]
    },
    "isError": false
  }
}
```

`result.content.text`를 중첩 `json.loads()`하는 개발계 fallback은 지원하지 않는다.
응답이 다르면 MCP 서버 계약을 고치거나 `_extract_mcp_structured_content()`를
명시적으로 변경하고 호환 테스트를 추가한다.

업무 코드 `1001`은 `NO_DATA`와 `MCP_NO_DATA_MESSAGE`로 변환한다. `isError=true`,
HTTP/JSON/스키마 오류는 `ERROR`와 안전 문구로 변환하며 내부 원문은 `error`와
로그에만 둔다.

## 결과 전처리와 화면 출력

registry의 `output_handler`가 detail별 전처리 함수다.

```python
def sample_output(context: ScenarioMcpOutputContext) -> ScenarioMcpOutput:
    raw = context.raw_result
    items = context.data_items()
    previous = context.results_for("FIRST_STEP")

    rows = custom_flatten_and_filter(items, previous)
    return ScenarioMcpOutput(
        data=rows,
        answer=ScenarioAnswer(
            text=f"총 {len(rows)}건입니다.",
            renderables=[create_table_renderable(...)],
        ),
    )
```

중앙 columns 설정은 없다. 다음을 함수에서 자유롭게 한다.

- objId 선택과 이름 변경
- 중복 유지/제거
- `no1Grid.objVal` 평탄화
- 페이지별 결과 병합
- 조건 count와 합계
- 여러 MCP step 결과 조합
- Markdown table/card/file 생성

`ScenarioMcpOutput.data`는 JSON 직렬화 가능한 어떤 구조도 가능하다. 최종 프론트
표시는 `answer.text`와 `answer.renderables`를 사용한다. `data`, MCP 원본,
arguments는 운영 SSE로 직접 보내지 않고 debug trace에서만 확인한다.

## 수정 위치

| 변경 | 위치 |
|---|---|
| detail 등록 | `app/mcp/scenarios/registry.py` |
| tool/arguments/순서 | `app/mcp/scenarios/<agent>.py` async handler |
| MCP URL/JSON-RPC/auth | `app/mcp/client.py` |
| 운영계 응답 추출 | `_parse_mcp_response()`, `_extract_mcp_structured_content()` |
| NO_DATA 공통 code/message | `app/mcp/client.py`, `app/mcp/models.py` |
| 페이지 next key | 해당 handler 옆 callback |
| 원본 전처리 | 해당 `*_output()` |
| 문장·표·카드 | 해당 `*_output()`과 `app/renderables.py` |
| 최종 복수 detail 조합 | `app/answers.py` |
| 프론트 SSE | `app/api.py`, `app/streaming.py` |

원본·페이지·grid 전처리 예시는
[15번 문서](15_FUNCTION_FIRST_MCP_INPUT_OUTPUT.md)를 참고한다.
