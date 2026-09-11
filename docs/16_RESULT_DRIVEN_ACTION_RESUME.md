# MCP 결과 기반 Action·체크포인트 재개 가이드

## 1. 지원하는 흐름

함수형 세부 시나리오는 다음 흐름을 모두 지원한다.

```text
action → 입력 → action → 입력 → MCP
MCP 1 → action → 입력 → MCP 1 결과 재사용 → MCP 2
MCP 조회 NO_DATA/ERROR → 재입력 action → 기존 조회 무효화 → MCP 재조회
MCP 1 → 결과 조건 분기 → action A 또는 action B → 후속 MCP
```

MCP 뒤 action이 발생하면 [`app/graph.py`](../app/graph.py)가 그 직전까지의 함수형
MCP 원장을 HITL 상태와 함께 Redis에 저장한다. 재진입 시
[`ScenarioMcpHandlerContext`](../app/mcp/scenario_runtime.py)가 저장 결과를 복원하고
같은 `step_code`를 다시 만났을 때 HTTP 호출 대신 체크포인트를 반환한다.

## 2. MCP 1 뒤 입력받고 MCP 2 호출

handler는 처음부터 읽히는 순서 그대로 작성한다.

```python
async def sample_handler(context):
    first = await context.call(
        step_code="CUSTOMER_LOOKUP",
        tool_name="customer_lookup",
        arguments={"employeeId": context.employee_id},
    )

    selection = CUSTOMER_SELECTION_ACTION.require(
        context.subagent.parameters
    )

    return await context.call(
        step_code="CUSTOMER_DETAIL",
        tool_name="customer_detail",
        arguments={
            "sourceRequestId": first.request_id,
            "customerId": selection["customer_id"],
        },
    )
```

첫 요청에서는 `CUSTOMER_LOOKUP`만 호출하고 action을 반환한다. 재진입하면 handler가
다시 시작되지만 `CUSTOMER_LOOKUP`은 Redis 체크포인트를 반환하므로 실제 MCP는
호출하지 않는다. 그 다음 `CUSTOMER_DETAIL`만 새로 호출한다.

각 handler 안에서 `step_code`는 안정적이고 고유하게 유지해야 한다. 이미 저장된
step code를 다른 도구나 다른 실행 방식으로 재사용하면 안 된다.

## 3. NO_DATA·ERROR 후 “다시 입력해 주세요”

이 패턴은 조회조건을 정말로 사용자에게 다시 받아야 하는 업무에만 사용한다.
RP·자격기준 Databricks RAG handler에는 적용하지 않는다. RAG는 마스터 보정 질문과
서브에이전트 keywords로 즉시 검색하며, 무문서·오류는 고정답변으로 종료한다.

기본 형태는 다음과 같다.

```python
result = await context.call(
    step_code="SEARCH",
    tool_name="search_tool",
    arguments={"query": query},
)

if result.outcome in {"NO_DATA", "ERROR"} or result.result is None:
    SEARCH_ACTION.request(
        message="조회 결과가 없거나 오류가 발생했습니다. 다시 입력해 주세요."
    )

return result
```

`require()`는 값이 없거나 형식이 잘못되었을 때 action을 만든다. `request()`는 현재
값이 이미 있어도 MCP 결과에 따라 action을 강제로 만든다.

## 4. 새 입력으로 이전 MCP를 다시 호출하는 방법

재입력 action에는 변경된 입력의 영향을 받는 step을 선언한다.

```python
SEARCH_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="SAMPLE",
        detail_scenario_code="DOCUMENT_SEARCH",
        action_code="DOCUMENT_SEARCH_RETRY_REQUIRED",
        message="다른 검색어를 입력해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="search_query",
                input_code="search_query",
                label="검색어",
            ),
        ),
        invalidate_step_codes=("SEARCH",),
    )
)
```

사용자 입력 검증이 성공하면 graph가 현재 handler 원장 중 `SEARCH` 결과만 제거한다.
handler 재실행 시 체크포인트가 없으므로 새 검색어로 MCP를 다시 호출한다.

- 후속 MCP에서 이전 결과를 계속 써야 함: `invalidate_step_codes=()`
- 검색 조건이 바뀌어 같은 MCP를 다시 조회해야 함: 해당 step code 지정
- 여러 선행 결과가 모두 무효가 됨: 관련 step code를 tuple에 모두 지정

명령·전송 MCP처럼 부작용이 있는 step은 일반적으로 무효화하면 안 된다. 실제 MCP도
`thread_id` 또는 MCP request ID 기반 멱등성을 지원하는 것이 안전하다.

## 5. MCP 결과에 따라 서로 다른 action 선택

action 정의 여러 개를 등록하고 결과 조건으로 선택한다.

```python
status = extract_status(first.result)

if status == "ADDRESS_REQUIRED":
    values = ADDRESS_ACTION.require(context.subagent.parameters)
elif status == "APPROVAL_REQUIRED":
    values = APPROVAL_ACTION.require(context.subagent.parameters)
else:
    values = {}
```

MCP 값에 따라 안내문만 바뀌는 경우에는 `request(message=...)`를 사용한다. 필드
구조나 검증이 달라지면 action 정의를 별도로 등록하는 편이 재진입 검증과 추적에
안전하다.

## 6. 외부 action output 최소 계약

직렬화 위치는 [`build_action_event()`](../app/streaming.py)다. 현재 기본 출력은
프론트 처리에 필요한 값만 포함한다.

```json
[
  {
    "code": "DOCUMENT_SEARCH_RETRY_REQUIRED",
    "thread_id": "server-generated-thread-id",
    "message": "다른 검색어를 입력해 주세요.",
    "inputCode": "search_query",
    "label": "검색어",
    "type": "text",
    "required": true
  }
]
```

다음 값은 해당할 때만 추가된다.

- `expectedValue`, `pattern`, `minLength`, `maxLength`, `allowedValues`
- `sensitive: true`
- 같은 code 입력의 표시·복원 순서를 정하는 `order`
- 검증 실패 시 해당 배열 원소의 `error`

내부 `interrupt.context`, MCP 원본 결과, handler code, 체크포인트는 운영 action에
포함하지 않는다. 새 프론트 필드가 실제로 필요하면 `build_action_event()`의
whitelist에 명시적으로 추가한다.

## 7. 입력 ID와 endpoint 기본값

요청 모델은 [`StreamingChatRequest`](../app/models.py)이고 `message`만 필수다.

| 입력 | 누락 | `""`/공백 | `null` | 서버 처리 |
|---|---:|---:|---:|---|
| `session_id` | 허용 | 허용 | 허용 | UUID 생성 |
| `thread_id` | 허용 | 허용 | 허용 | UUID 생성 |
| `endpoint` | 허용 | 허용 | 허용 | `PROJECT_CODE` 기본값 |

정규화는 `app/models.py:normalize_optional_identifiers()`, 실제 생성·기본값 적용은
`app/api.py:create_app.stream_chat()`에서 수행한다. action 재진입에서는 새 UUID를
만들면 저장 상태를 찾을 수 없으므로 프론트가 action에서 받은 `thread_id`를 다시
보내야 한다.

## 8. 기능별 수정 위치

| 변경 목적 | 수정 위치 |
|---|---|
| action code/message/input/검증 | 세부 시나리오의 `ScenarioActionDefinition` |
| 결과와 무관한 필수 입력 | `ACTION.require(parameters)` 호출부 |
| NO_DATA/ERROR 결과 후 action | MCP 호출 다음의 `ACTION.request()` 조건문 |
| 재입력 시 다시 조회할 MCP | `invalidate_step_codes` |
| MCP 1 뒤 action 후 MCP 2 | 같은 async handler의 `context.call()` 순서 |
| 체크포인트 조회·재사용 정책 | `app/mcp/scenario_runtime.py` |
| 체크포인트 Redis 저장·복원 | `app/graph.py:_call_mcp()` |
| action 입력 검증·step 무효화 | `app/graph.py:_validate_mcp_parameter_input()` |
| 외부 action 필드 추가/제거 | `app/streaming.py:build_action_event()` |
| 요청 선택 필드 정규화 | `app/models.py:StreamingChatRequest` |
| UUID/endpoint 기본값 | `app/api.py:stream_chat()` |

## 9. 테스트 체크리스트

1. MCP 1 다음 action에서 workflow 원장에 MCP 1 결과가 저장되는지 확인한다.
2. action 재진입 후 MCP 1 request ID가 같고 실제 executor 호출 수가 늘지 않는지
   확인한다.
3. 후속 MCP 2만 새로 호출되는지 확인한다.
4. NO_DATA/ERROR 후 재입력 action code/message/input이 맞는지 확인한다.
5. 유효한 재입력 후 `invalidate_step_codes` 대상이 원장에서 제거되는지 확인한다.
6. 새 값으로 MCP가 다시 호출되는지 확인한다.
7. 빈 action에는 `errors`와 null validation 필드가 출력되지 않는지 확인한다.
8. session/thread/endpoint의 누락·빈 문자열·null 조합을 모두 확인한다.
