# 시나리오별 무결과·오류·Action·후속 MCP 정책 가이드

## 1. 검토 결론

현재 함수형 시나리오 런타임은 아래 유형을 지원한다. 다만 현재 운영 기본정책은
MCP 오류를 재입력 action으로 바꾸지 않고 즉시 안전 고정답변으로 종료한다.

| 유형 | 지원 | 결정 위치 |
|---|---:|---|
| 무결과를 고정 답변으로 종료 | 예 | 해당 scenario handler가 action을 요청하지 않음 |
| 무결과·오류 후 파라미터 재입력 action | 예 | MCP 결과 다음의 `ACTION.request()` |
| action 입력 형식 오류 후 동일 action 재표시 | 예 | `ScenarioActionInput` + graph 검증 노드 |
| action 입력값에 따라 서로 다른 후속 MCP 호출 | 예 | `ACTION.require()` 반환값을 검사하는 handler 분기 |

공통 런타임이 모든 MCP 오류에 같은 action을 자동으로 붙이지 않는 것이 중요하다.
다시 받아야 할 값과 복구 가능 여부가 세부 시나리오마다 다르기 때문이다. 정책은
각 [`app/mcp/scenarios`](../app/mcp/scenarios) 함수에 명시하고 공통 계층은 저장,
검증, 체크포인트와 재개만 담당한다.

## 2. MCP 결과 판정 기준

공통 결과 모델은 [`McpExecutionResult`](../app/mcp/models.py)다.

| 필드 | 의미 |
|---|---|
| `succeeded` | 전송·파싱·도구 실행이 기술적으로 성공했는지 |
| `outcome` | `SUCCESS`, `NO_DATA`, `ERROR` 중 업무 결과 |
| `business_code` | MCP 업무 응답 코드. 현재 `1001`은 `NO_DATA` |
| `result` | 운영계 `result.structuredContent` 객체 |
| `user_message` | 내부 오류를 노출하지 않는 고정 사용자 문구 |
| `error` | 서버 로그·tester용 내부 오류. 운영 답변에 직접 사용하지 않음 |

HTTP 응답 파싱과 `1001 → NO_DATA` 변환은
[`GenosMcpToolExecutor.execute()`](../app/mcp/client.py)에서 한다. HTTP 성공이면서
`data: []`인 응답은 MCP가 `1001`을 주지 않으면 공통 계층상 `SUCCESS`일 수 있다.
빈 배열도 무결과로 취급할지는 아래처럼 scenario 함수에서 추가 판정한다.

### 공통 최종 안전장치

[`DefaultAnswerService.prepare()`](../app/answers.py)는 답변과 테이블을 만들기 전에
모든 terminal MCP 결과를 먼저 검사한다.

- 한 건이라도 `outcome=ERROR`, `succeeded=false`, `error` 존재: 전체 테이블과
  출처 문서를 버리고 오류 고정답변 한 건만 반환
- `business_code=1001` 또는 `outcome=NO_DATA`: 해당 시나리오의
  `SUBAGENT_NO_DATA_RESPONSES` 문구를 반환하고 테이블은 만들지 않음. 등록되지
  않은 detail만 공통 `조회 결과가 없습니다.`를 사용
- 세부 시나리오 수와 MCP 결과 수가 불일치: 실행 흐름 오류로 처리
- RAG의 필터·reranking 후 문서가 0건: 세부 시나리오별
  `no_documents_answer` 반환

개발 중 오류 코드와 내용을 답변 옆에서 확인하려면 다음 값을 사용한다.

```dotenv
RESPONSE_ERROR_DETAILS_ENABLED=true
```

운영에서는 반드시 `false`로 두는 것을 권장한다. 이 설정과 관계없이 전체 오류는
서버 로그에 남는다. 고정 문구 조립과 기본 마스킹은
[`build_safe_error_answer()`](../app/error_responses.py)에서 변경한다.

1001 무데이터 문구는
[`app/subagents/fixed_responses.py`](../app/subagents/fixed_responses.py)의
`SUBAGENT_NO_DATA_RESPONSES`에서 `(agent_code, detail_scenario_code)`별로 바꾼다.
예를 들어 `UNREGISTERED_MEMBER_SUMMARY`에는 `미등록건이 없습니다.`를 설정할 수
있다. 직접 고정답변은 같은 파일의 `SUBAGENT_FIXED_RESPONSES`, RAG 무문서·
답변불가는 `RAG_FIXED_RESPONSES`에서 관리한다.

```python
def _requires_retry(result: McpExecutionResult) -> bool:
    if result.backend == "disabled":
        return False
    if not result.succeeded or result.outcome in {"NO_DATA", "ERROR"}:
        return True
    if result.result is None:
        return True
    data = result.result.get("data")
    return isinstance(data, list) and not data
```

`backend == "disabled"`는 아직 연결하지 않은 MCP의 고정 안내를 보여 주는 개발
상태이므로 현재 RP/자격 검색 예시는 재입력 루프에 넣지 않는다. 업무에 따라 이
조건도 자유롭게 변경할 수 있다.

## 3. 유형 A: 무결과를 답변으로 종료

조회조건을 바꿔도 의미가 없거나 “데이터 없음” 자체가 정상 결과인 시나리오는
handler에서 결과를 그대로 반환한다.

```python
async def sample_summary(context):
    return await context.call(
        step_code="SUMMARY",
        tool_name="summary_tool",
        arguments={"employeeId": context.employee_id},
    )
```

`NO_DATA`인데 `ACTION.request()`를 호출하지 않으면 graph status는 `PASS`다.
[`DefaultAnswerService.prepare()`](../app/answers.py)가 `user_message`를 사용하여
현재 기본 문구인 `조회 결과가 없습니다.`를 token으로 스트리밍한다.

문구 변경 위치:

- 전체 MCP 공통 문구: `app/mcp/models.py:MCP_NO_DATA_MESSAGE`
- 특정 MCP만 다른 문구: handler에서 반환할 `McpExecutionResult.user_message`
- 성공했지만 빈 `data`를 별도 문장·표로 만들기: 해당 detail의 `*_output()`

주의: [`adapt_mcp_result()`](../app/mcp/result_adapters.py)은 `SUCCESS`가 아닌 결과에
대해 output handler를 호출하지 않는다. `NO_DATA`의 시나리오별 화면 문구가
필요하면 handler에서 `user_message`를 바꾸거나 공통 어댑터의 조기 반환 정책을
명시적으로 변경해야 한다.

## 4. 유형 B: 무결과·오류 후 재입력 action

검색어·주소·기준값을 바꾸면 복구 가능한 조회는 MCP 호출 직후 결과를 검사하고
`request()`를 호출한다.

```python
SEARCH_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="SAMPLE",
        detail_scenario_code="DOCUMENT_SEARCH",
        action_code="DOCUMENT_SEARCH_QUERY_REQUIRED",
        message="검색어를 입력해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="search_query",
                input_code="search_query",
                label="검색어",
                min_length=2,
            ),
        ),
        invalidate_step_codes=("DOCUMENT_SEARCH",),
    )
)

async def document_search(context):
    query = SEARCH_ACTION.require(context.subagent.parameters)["search_query"]
    result = await context.call(
        step_code="DOCUMENT_SEARCH",
        tool_name="document_search_tool",
        arguments={"query": query},
    )
    if _requires_retry(result):
        SEARCH_ACTION.request(
            message="조회 결과가 없습니다. 다른 검색어를 입력해 주세요."
        )
    return result
```

`require()`와 `request()`는 용도가 다르다.

- `require(parameters)`: 누락되거나 형식이 잘못된 값을 받을 때 사용
- `request(message=...)`: 값은 존재하지만 MCP 결과 때문에 다시 받아야 할 때 사용

`invalidate_step_codes`에는 새 입력으로 다시 실행해야 하는 step만 넣는다. 유효한
입력이 도착하면 [`_validate_mcp_parameter_input()`](../app/graph.py)이 해당 handler의
저장 원장에서 지정 step을 제거한다. 이후 handler가 다시 실행될 때 체크포인트가
없으므로 MCP를 새 값으로 호출한다.

RP/자격기준 Databricks 검색은 현재 이 재입력 정책을 사용하지 않는다. 최초 검색
결과를 그대로 최종 답변 단계로 보내고, 문서가 없으면 각 RAG 정책의 고정답변,
MCP 오류면 공통 안전 오류답변을 반환한다. 특정 시나리오에서만 검색어 재입력을
복원하려는 경우 위 예시처럼 해당 handler에 명시적으로 `request()`를 추가한다.

## 5. 유형 C: action 입력 형식 오류 후 동일 action 재표시

표준 검증은 `ScenarioActionInput`에 선언한다.

```python
ScenarioActionInput(
    parameter_name="fax_part_1",
    input_code="INPUT_FAX",
    order=1,
    label="지역번호",
    input_type="tel",
    pattern=r"^0\d{1,2}$",
    validation_message="팩스번호 지역번호를 확인해 주세요.",
    sensitive=True,
)
```

지원 규칙:

- 정확한 값: `expected_value`
- 허용 목록: `allowed_values`
- 정규식: `pattern` (`re.fullmatch`)
- 길이: `min_length`, `max_length`
- 업무별 파서: `value_parser(raw) -> (normalized, error_or_none)`

프론트는 같은 `/chat`으로 action의 `thread_id`와 `humanInput`을 보낸다.

```json
{
  "message": "action 입력",
  "session_id": "최초 요청과 같은 session",
  "thread_id": "action에서 받은 thread",
  "humanInput": [
    {"code": "INPUT_FAX", "input": "잘못된 값", "order": 1},
    {"code": "INPUT_FAX", "input": "1234", "order": 2},
    {"code": "INPUT_FAX", "input": "5678", "order": 3}
  ]
}
```

검증 실패 시 동작:

1. `ScenarioActionDefinition.validate_submission()`이 오류를 만든다.
2. graph는 status를 `INPUT_REQUIRED`로 유지한다.
3. 같은 action 배열을 다시 보내고 잘못된 원소에 `error`만 추가한다.
4. `_save_hitl_state()`가 같은 Redis HITL 키를 갱신하고 TTL을 다시 설정한다.
5. MCP와 LLM은 호출하지 않는다.

검증 성공 시에만 parameter가 `SubagentResult.matches[match_index].parameters`에
반영된다. 민감 필드는 로그와 tester 출력에서 마스킹되지만, 다음 단계 실행에
필요한 값은 HITL graph state에 저장될 수 있으므로 Redis 접근 통제가 필요하다.

## 6. 유형 D: action 결과에 따른 다른 MCP 호출

`require()` 반환값은 정규화가 끝난 dict이므로 일반 Python 분기로 사용한다.

```python
ROUTE_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="SAMPLE",
        detail_scenario_code="DETAIL_LOOKUP",
        action_code="DETAIL_ROUTE_REQUIRED",
        message="조회 유형을 선택해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="route",
                input_code="route",
                label="조회 유형",
                allowed_values=("SUMMARY", "DETAIL"),
            ),
        ),
    )
)

async def detail_lookup(context):
    source = await context.call(
        step_code="SOURCE",
        tool_name="source_tool",
        arguments={"employeeId": context.employee_id},
    )

    route = ROUTE_ACTION.require(context.subagent.parameters)["route"]
    if route == "SUMMARY":
        return await context.call(
            step_code="SUMMARY",
            tool_name="summary_tool",
            arguments={"sourceRequestId": source.request_id},
        )
    return await context.call(
        step_code="DETAIL",
        tool_name="detail_tool",
        arguments={"sourceRequestId": source.request_id},
    )
```

첫 요청에서 `SOURCE` 뒤 action이 발생하면 그 결과가 HITL 상태에 저장된다. 재개
후 handler가 처음부터 평가되더라도 `SOURCE`는
[`ScenarioMcpHandlerContext.checkpoint()`](../app/mcp/scenario_runtime.py)에서
재사용하고 선택된 후속 MCP만 호출한다.

선택값에 따라 선행 MCP도 다시 조회해야 한다면 action의
`invalidate_step_codes=("SOURCE",)`를 사용한다. 팩스 전송·등록처럼 부작용이
있는 MCP는 무효화하지 말고 MCP 자체에도 `thread_id`/업무 요청 ID 기반 멱등성을
적용한다.

## 7. 여러 번 왕복하는 action 흐름

한 handler에서 `require()`를 여러 번 배치할 수 있다.

```python
first = await context.call(...)
choice = CHOICE_ACTION.require(context.subagent.parameters)
second = await context.call(...)
approval = APPROVAL_ACTION.require(context.subagent.parameters)
return await context.call(...)
```

각 action은 서로 다른 `action_code`로 등록한다. 재개할 때 Python registry가
`agent_code + detail_scenario_code + action_code`로 정의를 복원한다. 이전 MCP
결과는 step code별 체크포인트로 재사용된다.

## 8. 수정 위치 표

| 바꿀 내용 | 위치 |
|---|---|
| 시나리오와 handler/output 연결 | `app/mcp/scenarios/registry.py` |
| MCP 호출·결과 조건·후속 분기 | `app/mcp/scenarios/<agent>.py`의 async handler |
| 무결과/오류 판정 | handler 옆의 업무별 판정 함수 |
| action code/message/input/검증 | 같은 scenario 파일의 `ScenarioActionDefinition` |
| action 강제 발생 | `ACTION.request()` 호출부 |
| action 누락·형식 검증 | `ACTION.require()` 및 `ScenarioActionInput` |
| 재입력 시 재조회 step | `invalidate_step_codes` |
| 체크포인트 정책 | `app/mcp/scenario_runtime.py` |
| action 검증·state 반영 | `app/graph.py:_validate_mcp_parameter_input()` |
| HITL 저장 허용 목록 | `app/graph.py:_save_hitl_state()` |
| action SSE 최소 필드 | `app/streaming.py:build_action_event()` |
| 성공 결과 문장·표 | scenario 파일의 `*_output()` |
| NO_DATA/ERROR 공통 문구 | `app/mcp/models.py` |

## 9. 필수 회귀 테스트

[`tests/test_scenario_actions.py`](../tests/test_scenario_actions.py)가 다음을 검증한다.

- MCP 1 뒤 action, 재개 후 MCP 1 재사용과 MCP 2 호출
- NO_DATA 뒤 재입력 action과 해당 조회 step 무효화
- NO_DATA를 action 없이 정상 고정 답변으로 종료
- 잘못된 action 입력에서 같은 action과 errors 반환
- 유효한 action 선택값에 따라 다른 후속 MCP 호출

신규 action을 추가할 때도 최소한 위 다섯 유형 중 해당하는 테스트를 복사하여
agent/detail/action/step/tool code를 실제 값으로 고정한다.
