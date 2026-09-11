# 함수형 MCP 단건·다중·페이지·순차 호출 가이드

## 공통 문맥

모든 활성 MCP detail은 registry에 연결된 async Python handler를 사용한다.

```python
async def handler(context: ScenarioMcpHandlerContext) -> McpExecutionResult:
    ...
```

| 값 | 용도 |
|---|---|
| `context.subagent.parameters` | LLM/action parameter |
| `context.request_context` | access token, endpoint, user |
| `context.employee_id` | 사번/익명 ID |
| `context.session_id` | 대화 범위 |
| `context.thread_id` | 질문/HITL 흐름 |
| `context.results` | 현재 handler 전체 호출 원장 |
| `context.results_for(step)` | 특정 step 결과 |

## 단건 호출

```python
result = await context.call(
    step_code="LOOKUP",
    tool_name="lookup_tool",
    arguments={"employeeId": context.employee_id},
)
```

## 같은 tool N건 호출

```python
payloads = [{"code": row["code"]} for row in source_rows]
aggregate = await context.call_many(
    step_code="DETAILS",
    tool_name="detail_tool",
    arguments_list=payloads,
    error_policy="continue",
    max_items=100,
)
```

- child 결과와 aggregate가 원장에 모두 남는다.
- `fail_fast`: 첫 실패에서 중단
- `continue`: 실패를 포함해 나머지 호출 진행
- 빈 arguments list도 aggregate 결과로 정규화된다.

## next key 페이지 호출

```python
def next_arguments(page, page_number):
    items = extract_data_items(page.result)
    next_values = {
        str(item.get("objId", "")): item.get("objVal")
        for item in items
        if "nextkey" in str(item.get("objId", "")).casefold()
    }
    if next_values and all(
        str(value or "").strip().casefold() in {"", "null", "none"}
        for value in next_values.values()
    ):
        return None
    return {
        **next_values,
        "_page_number": page_number + 1,
    }

aggregate = await context.paginate(
    step_code="PAGED_LOOKUP",
    tool_name="paged_tool",
    initial_arguments={"_page_number": 1},
    next_arguments=next_arguments,
    max_pages=100,
)
```

next key 이름, `gridct → no1PgeSize`, 종료값, 페이지 번호는 tool마다 다르므로
callback을 해당 handler 옆에 둔다. 공통 runtime은 같은 cursor 반복과 최대 page만
안전하게 제한한다.

## 다른 tool 순차 호출

```python
first = await context.call(
    step_code="CODES",
    tool_name="code_tool",
    arguments={...},
)
codes = custom_extract_codes(first.result)

second = await context.call_many(
    step_code="DETAILS",
    tool_name="detail_tool",
    arguments_list=[
        {
            "code": code,
            "bearerToken": context.request_context.get("access_token", ""),
        }
        for code in codes
    ],
)
return second
```

첫 결과 10개를 두 번째 MCP 10회로 조회한 뒤 aggregate terminal을 output 함수에
전달할 수 있다. output에서는 `context.results_for("CODES")`와
`context.results_for("DETAILS")`로 child/aggregate를 모두 읽는다.

## code별 페이지 호출

각 code마다 `paginate()`를 호출하되 step code가 고유해야 한다.

```python
terminal = source
for index, row in enumerate(code_rows):
    code = str(row["code"]).strip()
    terminal = await context.paginate(
        step_code=f"SCORE_{index:03d}_{code}",
        tool_name="score_tool",
        initial_arguments={"code": code, "_page_number": 1},
        next_arguments=score_next_arguments,
    )
return terminal
```

동일 step code를 여러 code에 재사용하면 action 재개 체크포인트가 잘못 매칭될 수
있다.

## MCP 사이 action

```python
first = await context.call(...)
choice = CHOICE_ACTION.require(context.subagent.parameters)
return await context.call(
    step_code="SECOND",
    tool_name="second_tool",
    arguments={"choice": choice["choice"], "source": first.request_id},
)
```

action 이전 결과는 HITL Redis에 저장된다. 재개 시 같은 step/mode 체크포인트를
재사용한다. 새 입력으로 다시 실행할 step만 action의 `invalidate_step_codes`에
지정한다. 자세한 조건 분기는 [17번 문서](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)를
참고한다.

## output에서 전체 원장 읽기

```python
def output(context):
    terminal = context.execution
    all_results = context.workflow_results
    detail_children = [
        result
        for result in context.results_for("DETAILS")
        if not result.workflow_is_aggregate
    ]
    rows = custom_merge([item.result for item in detail_children])
    return ScenarioMcpOutput(...)
```

## 안전 규칙

- `step_code`는 handler 안에서 안정적이고 고유하게 유지
- 최대 items/pages를 업무 상한으로 제한
- 전송/등록 tool은 MCP 서버 멱등성 적용
- access token을 output/Redis/trace에 넣지 않음
- MCP 원본 오류를 사용자 답변에 직접 넣지 않음
- output handler에서 원하는 컬럼·페이지·중복 정책을 직접 결정

## 수정 위치

| 목적 | 위치 |
|---|---|
| detail 연결 | `app/mcp/scenarios/registry.py` |
| 실제 workflow | `app/mcp/scenarios/<agent>.py` |
| 공통 호출·체크포인트 | `app/mcp/scenario_runtime.py` |
| JSON-RPC/HTTP | `app/mcp/client.py` |
| 결과 전처리 | detail `*_output()` |
| tester trace | `app/graph.py`, `app/csv_trace.py` |
