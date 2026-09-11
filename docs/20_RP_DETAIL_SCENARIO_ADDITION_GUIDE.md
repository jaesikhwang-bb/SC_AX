# RP 세부 시나리오 추가 완전 가이드

이 문서는 2026-08-27 현재 코드 기준으로 `RP` 에이전트 안에 새로운 세부
시나리오 하나를 추가하는 전체 과정을 설명한다. 분류, 파라미터 추출, MCP
payload, 단건·다중·페이지 호출, ACTION/HITL, 결과 전처리, 답변·표·추천질문
SSE 출력, RAG·고정답변 대안과 테스트까지 추적한다.

이 문서의 예시에서는 다음 가상 업무를 사용한다.

| 구분 | 예시 값 |
|---|---|
| 에이전트 | `RP` |
| 최상위 시나리오 코드 | `RP_CUSTOM_LOOKUP` |
| 세부 시나리오 코드 | `RP_CUSTOM_DETAIL` |
| 추출 파라미터 | `target_code`, `closing_year_month` |
| MCP 도구명 | `rp_custom_tool` |
| MCP step 코드 | `RP_CUSTOM_DETAIL_LOOKUP` |

실제 적용 시 위 코드와 이름을 실제 업무명으로 바꾼다. 코드 값은 manifest,
Python handler, ACTION, registry, 추천질문, 테스트에서 완전히 같아야 한다.

## 1. 가장 중요한 현재 구조

현재 활성 업무는 YAML에 MCP 도구와 payload를 선언하는 구조가 아니다. YAML은
분류 metadata, LLM 추출 파라미터 schema와 추천질문을 관리하고, 실제 도구명,
arguments, 호출 순서, 결과 조건과 화면 출력은 Python 함수가 관리한다.

```text
사용자 질문
  -> 마스터 에이전트 분류: RP
  -> RP 서브에이전트 분류: RP_CUSTOM_DETAIL
  -> app/mcp/scenarios/registry.py에서 handler 조회
  -> app/mcp/scenarios/rp.py의 rp_custom_detail()
  -> context.call()/call_many()/paginate()
  -> 공통 MCP JSON-RPC 전송
  -> rp_custom_detail_output()
  -> answer_text + renderables
  -> OUTPUT 가드레일
  -> SSE token/messages/recommendedQuestions
```

중앙 업무별 payload 분기 파일은 더 이상 없다. `app/mcp/scenarios/rp.py`에서
`context.call()` 등에 전달한 `tool_name`과 `arguments`가 실제 MCP 요청이 된다.
신규 업무 규칙도 해당 시나리오 파일에만 추가한다.

## 2. 수정 위치 전체표

| 목적 | 파일 | 필요 조건 |
|---|---|---|
| RP detail 및 파라미터 schema 등록 | `prompts/subagents/rp/v1/manifest.yaml` | 필수 |
| 세부 업무 선택·제외·예시 설명 | `prompts/subagents/rp/v1/scenarios/*.md` | 필수 권장 |
| RP 내부 detail 우선순위·파라미터 규칙 | `prompts/subagents/rp/v1/system.md` | 필수 권장 |
| MCP 입력·도구·호출 흐름 | `app/mcp/scenarios/rp.py` | MCP 조회면 필수 |
| 원본 결과 전처리·본문·표 | `app/mcp/scenarios/rp.py` | 비-RAG 조회면 필수 |
| `(agent, detail)`과 함수 연결 | `app/mcp/scenarios/registry.py` | MCP 조회면 필수 |
| 마스터 RP 선택 범위 | `prompts/intent-classification/v1/agents/rp.md` | RP 업무 경계가 확장되면 필수 |
| 에이전트 간 충돌 우선순위 | `prompts/intent-classification/v1/router/system.md` | 다른 에이전트와 겹치면 필요 |
| ACTION 정의·등록·사용 | `app/mcp/scenarios/rp.py` | 사용자 추가 입력이 필요하면 |
| 수동 추천질문 | RP `manifest.yaml` | 선택, 0개 이상 |
| RAG 검색·전처리·임계점수·무응답·최종 답변 | `app/mcp/scenarios/rp.py` | 문서 검색 시나리오만 |
| MCP 없는 고정답변 | `app/subagents/fixed_responses.py` | 고정답변 시나리오만 |
| 개발용 mock 결과 | `app/mcp/client.py`의 `_build_mock_structured_content()` | 전용 mock이 필요하면 |
| handler·payload·output 회귀 테스트 | `tests/test_mcp_function_handlers.py` 등 | 필수 권장 |

정상적인 신규 함수형 시나리오라면 다음 파일은 수정하지 않는다.

- `app/graph.py`: 등록 handler를 자동 실행하고 ACTION/HITL을 공통 처리
- `app/mcp/result_adapters.py`: 등록 output handler를 자동 호출
- `app/answers.py`: `formatted_result.answer_text`와 renderable을 자동 조합
- `app/api.py`: 가드레일과 SSE 전송을 자동 처리
- `app/streaming.py`: 공통 SSE 직렬화

## 3. 코드와 이름 결정

추가 전에 다음 코드를 먼저 확정한다.

```text
agent_code             = RP
scenario_code          = RP_CUSTOM_LOOKUP
detail_scenario_code   = RP_CUSTOM_DETAIL
handler_code           = rp.rp_custom_detail.v1
output_handler_code    = rp.rp_custom_detail_output.v1
step_code              = RP_CUSTOM_DETAIL_LOOKUP
action_code            = RP_CUSTOM_TARGET_REQUIRED   # ACTION이 있을 때
tool_name              = rp_custom_tool
```

각 코드의 역할은 다르다.

| 코드 | 역할 |
|---|---|
| `scenario_code` | RP manifest 안에서 업무들을 묶는 상위 그룹 |
| `detail_scenario_code` | 분류·실행·출력 정책을 결정하는 핵심 식별자 |
| `handler_code` | 함수형 MCP 실행 원장과 checkpoint를 식별 |
| `output_handler_code` | 어떤 출력 전처리 함수를 썼는지 추적 |
| `step_code` | 한 detail 안의 개별 MCP 단계 및 재사용 checkpoint 식별 |
| `action_code` | 프론트가 어떤 추가 입력 화면을 표시할지 식별 |
| `tool_name` | GenOS MCP `tools/call`의 실제 도구 이름 |

다음 규칙을 지킨다.

- detail 코드는 대문자 `SNAKE_CASE`를 권장한다.
- 같은 RP manifest 안에서 detail 코드는 중복될 수 없다.
- registry는 detail 코드를 자동 대문자 변환하지 않으므로 동일 문자열을 사용한다.
- 운영된 detail, handler, step, action 코드는 Redis/HITL과 trace에 남으므로 가능하면
  이름을 바꾸지 않는다.
- 같은 MCP 도구를 쓰더라도 업무별 output이 다르면 detail별 output 함수를 둔다.

## 4. 프롬프트 버전 관리

현재 활성 RP prompt는 다음 파일이 결정한다.

```text
prompts/subagents/rp/active.yaml
```

현재 값은 `v1`이다.

```yaml
active_version: "v1"
```

개발 중에는 현재 `v1`을 수정할 수 있지만 운영 추적성이 필요하면 기존 폴더를
덮어쓰기보다 다음 순서를 권장한다.

1. `prompts/subagents/rp/v1`을 `v2` 같은 새 폴더로 복사한다.
2. 새 폴더의 `manifest.yaml.version`과 `released_at`을 갱신한다.
3. 새 detail과 prompt를 새 버전에 추가한다.
4. 검증 완료 후 `active.yaml.active_version`을 새 버전으로 변경한다.
5. 서버를 재시작한다.

마스터 분류 prompt의 업무 경계도 변경한다면
`prompts/intent-classification` 역시 같은 방식으로 새 버전을 운영할 수 있다.

## 5. RP manifest에 파라미터 정의

`prompts/subagents/rp/v1/manifest.yaml`의 `parameter_definitions`에 LLM이 추출할
파라미터를 먼저 등록한다.

```yaml
parameter_definitions:
  search_query:
    description: "기존 설정"

  address:
    description: "기존 설정"

  closing_year_month:
    description: "마감작업년월. 반드시 YYYYMM 형식이며 질문에 없으면 null"
    pattern: "^[0-9]{6}$"

  reference_date:
    description: "기준일자. 반드시 YYYYMMDD 형식이며 질문에 없으면 null"
    pattern: "^[0-9]{8}$"

  # 신규 추가
  target_code:
    description: >
      RP_CUSTOM_DETAIL 조회 대상 코드. 사용자가 코드값을 명시한 경우에만
      추출하고 질문에 없으면 null
    pattern: "^[A-Za-z0-9_-]+$"
```

현재 동적 Structured Output schema에서 detail 파라미터는 기본적으로
`str | None`이다. 숫자도 문자열로 추출하고 `rp.py`의 업무 함수에서 명시적으로
`int()` 또는 `float()`로 바꾸는 편이 안전하다.

manifest의 `pattern`과 `allowed_values`는 서브에이전트 구조화 출력 schema에
반영된다. 하지만 실제 사용자 ACTION 입력은 LLM 출력과 별개이므로
`ScenarioActionInput`에서 다시 검증해야 한다.

## 6. RP manifest에 detail 등록

새로운 최상위 업무 그룹이라면 `scenarios`에 다음 블록을 추가한다.

```yaml
scenarios:
  # 기존 시나리오들...

  - code: "RP_CUSTOM_LOOKUP"
    name: "RP 신규 업무 조회"
    description: "대상 코드와 마감년월을 기준으로 RP 신규 업무 데이터를 조회"
    details:
      - code: "RP_CUSTOM_DETAIL"
        name: "RP 신규 업무 상세 조회"
        description: >
          사용자가 RP 신규 업무의 대상 코드별 상세 데이터를 조회하려는 경우 선택
        parameters:
          - "target_code"
          - "closing_year_month"
        recommended_questions:
          - "지난달 신규 업무 내역도 보여줘"
          - "신규 업무 조회 기준을 알려줘"
```

기존 최상위 시나리오에 속하는 detail이라면 새 상위 블록을 만들지 않고 기존
`details` 배열에 추가한다.

파라미터가 전혀 없는 detail은 다음처럼 쓸 수 있다.

```yaml
parameters: []
```

manifest loader는 다음을 서버 시작 시 검증한다.

- scenario 코드 중복
- detail 코드 중복
- `detail.parameters`가 배열인지
- detail이 `parameter_definitions`에 없는 파라미터를 참조하는지
- MCP·고정답변 detail에 실행 handler가 등록됐는지

비고정답변 detail을 manifest에 먼저 추가하고 registry에 handler를 등록하지 않으면
서버 시작 시 다음 취지의 오류가 발생한다.

```text
고정답변이 아닌 detail에 Python MCP handler가 없습니다.
app/mcp/scenarios/registry.py에 등록하세요.
```

따라서 manifest와 registry 변경은 한 배포 단위로 적용한다.

## 7. 세부 시나리오 Markdown 작성

다음과 같은 새 파일을 추가한다.

```text
prompts/subagents/rp/v1/scenarios/05_rp_custom_lookup.md
```

권장 내용은 다음과 같다.

```markdown
# RP 신규 업무 조회

## RP_CUSTOM_DETAIL

다음 질문은 RP_CUSTOM_DETAIL을 선택한다.

- RP 신규 업무 대상 코드를 조회해줘
- ABC 코드의 지난달 RP 내역을 보여줘
- 2026년 8월 ABC 코드 상세를 조회해줘

다음 질문은 선택하지 않는다.

- RP 신청 기준을 알려줘
  - RP의 7개 문서 detail 중 질문에 명시된 서비스 대상
- 아파트 관리비 자동납부 가능 단지를 찾아줘
  - APARTMENT_RP_LIST 대상
- 일반 모집인 실적을 조회해줘
  - PERFORMANCE_FEE 대상

파라미터 추출 규칙:

- 대상 코드는 target_code로 추출한다.
- 조회 월은 closing_year_month에 YYYYMM 형식으로 추출한다.
- 질문에 대상 코드가 없으면 target_code는 null이다.
- 질문에 조회 월이 없으면 closing_year_month는 null이다.
- 사용자가 말하지 않은 값을 임의로 생성하지 않는다.
```

정확도를 높이려면 다음을 모두 적는다.

- 선택해야 하는 대표 질문
- 비슷하지만 다른 detail로 가야 하는 반례
- 다른 에이전트로 가야 하는 반례
- 파라미터 이름과 형식
- 값이 없을 때 `null`로 반환하는 규칙
- 상대 날짜를 어떻게 해석하는지
- 임의 생성하면 안 되는 식별자
- 두 detail이 동시에 선택될 수 있는 복합 질문의 기준

그다음 manifest의 `prompt_files`에 파일을 명시한다.

```yaml
prompt_files:
  - "system.md"
  - "scenarios/01_rp_documents.md"
  - "scenarios/02_apartment_management_fee_rp_eligibility.md"
  - "scenarios/04_composite_conversion.md"
  - "scenarios/05_rp_custom_lookup.md"
```

prompt loader는 선언되지 않은 Markdown도 뒤에서 자동 결합하지만, 결합 순서와
추적 가능성을 위해 `prompt_files`에 명시하는 것을 권장한다.

## 8. RP system prompt에 분류 규칙 추가

`prompts/subagents/rp/v1/system.md`의 공통 분류 규칙과 파라미터 추출 규칙에
신규 detail을 명시한다.

```markdown
- 사용자가 RP 신규 업무의 대상 코드별 실제 데이터를 조회하려는 경우
  `RP_CUSTOM_DETAIL`을 선택한다.
- 신규 업무의 기준이나 정책만 질문한 경우에는 실제 조회가 아니므로
  RP의 7개 문서 detail 중 해당 서비스 코드를 선택한다.
- `RP_CUSTOM_DETAIL`에서는 대상 코드를 `target_code`, 조회 월을
  `closing_year_month`로 추출한다.
- 대상 코드나 조회 월을 사용자가 입력하지 않았다면 임의로 생성하지 않고
  null로 반환한다.
```

다음과 같이 인접 detail의 경계를 함께 적는 것이 중요하다.

```text
실제 신규 업무 데이터 조회       -> RP_CUSTOM_DETAIL
기준·절차·정책 안내              -> 해당 RP 서비스 문서 detail
아파트명·주소로 가능 단지 조회    -> APARTMENT_RP_LIST
일반 모집인 실적 종합조회         -> PERFORMANCE_FEE
```

하나의 질문에 독립적인 RP 업무가 여러 개 포함되면 RP 서브에이전트는
`matches`에 여러 detail을 선택할 수 있다. 각 match의 `parameters`에는 해당
detail이 manifest에서 허용한 키만 들어간다.

## 9. 마스터가 RP를 선택하도록 경계 수정

마스터는 detail을 선택하지 않고 먼저 `RP`, `PERFORMANCE_FEE`, `QUALIFICATION`
중 에이전트 하나를 선택한다. RP로 분류된 뒤에만 RP manifest의 신규 detail을
볼 수 있다.

신규 업무가 기존 RP 정의에 명확히 포함된다면 RP 서브에이전트 prompt만
수정해도 된다. 다음에 해당하면 마스터 prompt도 수정한다.

- 기존 RP 역할 설명에 없는 신규 업무다.
- 질문 표현만 보면 `PERFORMANCE_FEE`나 `QUALIFICATION`으로 갈 가능성이 있다.
- 프론트 선택 agent와 마스터 분류가 자주 달라 `AGENT_CODE_MISMATCH`가 발생한다.
- 특정 공통 업무는 프론트의 현재 선택 agent를 유지해야 한다.

`prompts/intent-classification/v1/agents/rp.md`의 선택 조건 예시는 다음과 같다.

```markdown
* RP 신규 업무 대상 코드별 실제 데이터 조회
  (대상 코드와 조회 월을 이용한 신규 업무 상세 조회)
```

핵심 판단 기준과 선택하지 않는 경우에도 경계를 추가한다.

```markdown
프론트 선택이 RP이고 질문이 RP 신규 업무의 대상 코드별 조회라면 RP를 유지한다.
일반 모집인 실적 종합조회는 PERFORMANCE_FEE로 분류한다.
```

다른 에이전트와 충돌하는 특별 우선순위가 있으면
`prompts/intent-classification/v1/router/system.md`에도 같은 규칙을 추가한다.
필요하면 충돌하는 상대 에이전트의 `agents/*.md` 선택·제외 조건도 함께 조정한다.

## 10. `rp.py`에 단건 MCP handler 추가

`app/mcp/scenarios/rp.py`에 신규 비동기 함수를 추가한다. 파라미터 기본값,
request context 매핑, 도구명과 실제 arguments는 이 함수가 결정한다.

```python
async def rp_custom_detail(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    parameters = context.subagent.parameters

    target_code = str(parameters.get("target_code", "")).strip()
    closing_year_month = month_value(
        parameters,
        today=date.today(),
        default_month="CURRENT",
    )

    user = context.request_context.get("user")
    if not isinstance(user, dict):
        user = {}

    return await context.call(
        step_code="RP_CUSTOM_DETAIL_LOOKUP",
        tool_name="rp_custom_tool",
        arguments={
            "bearerToken": str(
                context.request_context.get("access_token", "")
            ).strip(),
            "employeeId": context.employee_id,
            "deptCode": str(user.get("deptcode") or "").strip(),
            "targetCode": target_code,
            "closingYearMonth": closing_year_month,
        },
    )
```

handler에서 사용할 수 있는 주요 값은 다음과 같다.

```python
context.subagent.parameters       # LLM 또는 ACTION으로 확정된 업무 파라미터
context.employee_id               # 사번 또는 서버 생성 익명 식별자
context.session_id
context.thread_id
context.request_context           # API 요청 문맥
context.results                   # 현재까지의 MCP 개별·집계 결과
context.results_for("STEP_CODE")  # 특정 step 결과만 선택
context.checkpoint("STEP_CODE")   # 재진입 시 저장된 terminal 결과 확인
```

현재 API가 handler에 전달하는 `request_context`는 다음 형태다.

```python
{
    "access_token": "...",
    "endpoint": "...",
    "recruitment_org_type_code": "...",
    "user": {
        "id": "...",
        "deptcode": "...",
        "deptname": "...",
    },
}
```

`access_token`은 MCP 도구 arguments에 자동 삽입되지 않는다. 실제 도구 schema가
`bearerToken` 같은 필드를 요구할 때 handler가 직접 추가해야 한다. MCP Gateway의
HTTP `Authorization` 헤더에는 별도의 환경변수 `MCP_BEARER_TOKEN`이 사용된다.

## 11. 실제 MCP payload 생성 과정

함수형 handler의 실제 흐름은 다음과 같다.

```text
rp_custom_detail()
  -> context.call(tool_name, arguments)
  -> ScenarioMcpHandlerContext._invoke()
  -> GenosMcpToolExecutor.execute(tool_name, arguments)
  -> JSON-RPC tools/call body 생성
  -> MCP HTTP POST
```

최종 JSON-RPC body는 다음 형태다.

```json
{
  "jsonrpc": "2.0",
  "id": "자동 생성된 request_id",
  "method": "tools/call",
  "params": {
    "name": "rp_custom_tool",
    "arguments": {
      "bearerToken": "...",
      "employeeId": "...",
      "deptCode": "...",
      "targetCode": "ABC",
      "closingYearMonth": "202608"
    }
  }
}
```

공통 JSON-RPC 봉투는 `app/mcp/client.py`의 `GenosMcpToolExecutor.execute()`가
만든다. 이 실행기에는 agent/detail별 업무 분기가 없다. 신규 함수형 handler의
업무 규칙은 `app/mcp/scenarios/rp.py`에만 작성한다.

## 12. 호출 방식 선택

### 12.1 단건 호출

도구를 한 번 호출하면 `context.call()`을 사용한다.

```python
result = await context.call(
    step_code="RP_CUSTOM_DETAIL_LOOKUP",
    tool_name="rp_custom_tool",
    arguments={"targetCode": target_code},
)
return result
```

### 12.2 같은 도구 N번 호출

여러 arguments를 같은 도구에 전달하려면 `context.call_many()`를 사용한다.

```python
arguments_list = [
    {"targetCode": code, "employeeId": context.employee_id}
    for code in target_codes
]

result = await context.call_many(
    step_code="RP_CUSTOM_DETAIL_MANY",
    tool_name="rp_custom_tool",
    arguments_list=arguments_list,
    error_policy="continue",
    max_items=100,
)
return result
```

집계 terminal 결과에는 다음 정보가 생긴다.

```text
result.data       모든 성공 호출의 data를 평탄하게 결합
result.batches    호출별 arguments/result/error 원장
result.execution 호출수·성공·NO_DATA·ERROR 집계
```

### 12.3 next key 페이지 호출

다음 arguments 생성 함수를 시나리오 옆에 두고 `context.paginate()`를 사용한다.

```python
def _rp_custom_next_arguments(
    page: McpExecutionResult,
    page_number: int,
) -> dict[str, Any] | None:
    # page.result를 해당 도구 규격대로 직접 해석한다.
    # 다음 페이지가 없으면 None 또는 {}를 반환한다.
    ...


result = await context.paginate(
    step_code="RP_CUSTOM_DETAIL_PAGES",
    tool_name="rp_custom_tool",
    initial_arguments={"targetCode": target_code},
    next_arguments=_rp_custom_next_arguments,
    max_pages=100,
    error_policy="fail_fast",
)
return result
```

next key 이름, 종료 조건, page size 매핑은 도구마다 다르므로 중앙 YAML에 넣지
않고 해당 handler 옆의 Python 함수가 결정한다.

### 12.4 서로 다른 MCP 순차 호출

같은 handler 안에서 호출을 순서대로 작성한다.

```python
first = await context.call(
    step_code="RP_CUSTOM_CODES",
    tool_name="rp_code_list_tool",
    arguments={"employeeId": context.employee_id},
)

codes = parse_codes(first.result)

second = await context.call_many(
    step_code="RP_CUSTOM_DETAILS",
    tool_name="rp_custom_tool",
    arguments_list=[{"targetCode": code} for code in codes],
    error_policy="continue",
)

return second
```

출력 함수에서는 `context.results_for("RP_CUSTOM_CODES")`와
`context.results_for("RP_CUSTOM_DETAILS")`로 각 단계 결과를 따로 읽을 수 있다.

## 13. 필수 입력 ACTION 추가

필수 파라미터가 질문에 없거나 형식을 서버에서 재확인해야 한다면 ACTION을
`rp.py`에서 함수형으로 정의한다.

```python
def _rp_custom_target_action() -> ScenarioActionDefinition:
    return ScenarioActionDefinition(
        agent_code="RP",
        detail_scenario_code="RP_CUSTOM_DETAIL",
        action_code="RP_CUSTOM_TARGET_REQUIRED",
        message="조회할 대상 코드와 조회 월을 입력해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="target_code",
                input_code="targetCode",
                label="대상 코드",
                pattern=r"^[A-Za-z0-9_-]+$",
                validation_message="대상 코드 형식을 확인해 주세요.",
            ),
            ScenarioActionInput(
                parameter_name="closing_year_month",
                input_code="closingYearMonth",
                label="조회 월",
                pattern=r"^[0-9]{6}$",
                validation_message="조회 월은 YYYYMM 형식이어야 합니다.",
            ),
        ),
        invalidate_step_codes=("RP_CUSTOM_DETAIL_LOOKUP",),
    )


RP_CUSTOM_TARGET_ACTION = register_scenario_action(
    _rp_custom_target_action()
)
```

handler에서는 MCP arguments를 만들기 전에 `require()`를 호출한다.

```python
async def rp_custom_detail(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    action_values = RP_CUSTOM_TARGET_ACTION.require(
        context.subagent.parameters
    )

    return await context.call(
        step_code="RP_CUSTOM_DETAIL_LOOKUP",
        tool_name="rp_custom_tool",
        arguments={
            "targetCode": action_values["target_code"],
            "closingYearMonth": action_values["closing_year_month"],
        },
    )
```

`parameter_name`과 `input_code`는 용도가 다르다.

| 값 | 용도 |
|---|---|
| `parameter_name` | 서버 내부 `subagent.parameters`에 저장할 키 |
| `input_code` | 프론트 `humanInput[].code`와 action 배열의 `inputCode` |

ACTION 입력으로 추가될 모든 `parameter_name`은 manifest의
`parameter_definitions`와 해당 detail의 `parameters`에도 포함하는 것이 안전하다.

`invalidate_step_codes`는 ACTION의 새 값 때문에 다시 호출해야 하는 MCP step만
지정한다. 비어 있으면 ACTION 재진입 뒤 기존 checkpoint가 재사용된다.

## 14. ACTION 프론트 입출력

위 정의는 공통 graph와 streaming 계층에서 다음 형태로 변환된다.

```json
[
  {
    "code": "RP_CUSTOM_TARGET_REQUIRED",
    "inputCode": "targetCode",
    "thread_id": "...",
    "message": "조회할 대상 코드와 조회 월을 입력해 주세요.",
    "label": "대상 코드",
    "type": "text",
    "required": true,
    "pattern": "^[A-Za-z0-9_-]+$"
  },
  {
    "code": "RP_CUSTOM_TARGET_REQUIRED",
    "inputCode": "closingYearMonth",
    "thread_id": "...",
    "message": "조회할 대상 코드와 조회 월을 입력해 주세요.",
    "label": "조회 월",
    "type": "text",
    "required": true,
    "pattern": "^[0-9]{6}$"
  }
]
```

프론트는 같은 `session_id`, `thread_id`, `endpoint`를 사용하여 다음처럼 재입력한다.

```json
{
  "message": "추가 입력",
  "session_id": "기존 session_id",
  "thread_id": "기존 thread_id",
  "endpoint": "기존 endpoint",
  "agent_code": "RP",
  "humanInput": [
    {
      "code": "targetCode",
      "input": "ABC"
    },
    {
      "code": "closingYearMonth",
      "input": "202608"
    }
  ]
}
```

형식이 잘못되면 해당 ACTION 배열 원소에 `error`가 붙어서 다시 반환된다. ACTION과 MCP
checkpoint 상태는 HITL store에 보관되고 완료되면 삭제된다. `access_token`과
request context는 HITL Redis에 저장하지 않고 재입력 HTTP 요청에서 다시 받는다.

## 15. MCP 결과에 따른 ACTION·답변 정책

### 15.1 무결과를 답변으로 종료

결과를 그대로 반환한다.

```python
result = await context.call(...)
return result
```

MCP 업무 코드가 `1001`이면 공통 client가 `NO_DATA`로 변환한다. 최종 문구는
`app/subagents/fixed_responses.py`의 `SUBAGENT_NO_DATA_RESPONSES`에서
`("RP", "세부코드")` 키로 지정한다. 키가 없을 때만 공통
`조회 결과가 없습니다.`를 사용한다.

### 15.2 시나리오 전용 무결과 문구

```python
("RP", "NEW_DETAIL_CODE"): SubagentFixedResponse(
    default_message="해당 조건의 RP 신규 업무 내역이 없습니다.",
),
```

### 15.3 무결과·오류 후 다시 입력받기

```python
if (
    not result.succeeded
    or result.outcome in {"NO_DATA", "ERROR"}
    or result.result is None
):
    RP_CUSTOM_TARGET_ACTION.request(
        message="조회 결과가 없습니다. 다른 대상 코드를 입력해 주세요."
    )
return result
```

### 15.4 결과에 따라 다른 MCP 호출

```python
first = await context.call(...)

if should_call_detail(first):
    return await context.call(
        step_code="RP_CUSTOM_SECOND",
        tool_name="rp_custom_second_tool",
        arguments=build_second_arguments(first),
    )

return first
```

예외를 업무 분기 수단으로 사용하지 않는다. handler의 일반 예외는 공통 runtime이
안전한 `ERROR` 결과로 바꾸며 프론트에는 내부 오류 대신 안전한 문구가 전달된다.

## 16. MCP output handler 작성

운영계 `structuredContent`가 다음처럼 온다고 가정한다.

```json
{
  "data": [
    {"objId": "targetName", "objVal": "테스트 대상"},
    {"objId": "resultCount", "objVal": "10"}
  ]
}
```

`rp.py`에 detail 전용 output 함수를 추가한다.

```python
def rp_custom_detail_output(
    context: ScenarioMcpOutputContext,
) -> ScenarioMcpOutput:
    items = context.data_items()

    values = {
        str(item.get("objId", "")).strip(): item.get("objVal")
        for item in items
        if str(item.get("objId", "")).strip()
    }

    target_name = values.get("targetName", "")
    result_count = values.get("resultCount", "")

    rows = [
        ("대상명", target_name),
        ("조회 건수", result_count),
    ]

    return ScenarioMcpOutput(
        data={
            "rawItems": items,
            "targetName": target_name,
            "resultCount": result_count,
        },
        answer=ScenarioAnswer(
            text=(
                "[RP 신규 업무 조회 결과]\n"
                f"- 대상명: {target_name}\n"
                f"- 조회 건수: {result_count}"
            ),
            renderables=[
                create_table_renderable(
                    code="rp-custom-result-table",
                    title="RP 신규 업무 조회 결과",
                    format="markdown",
                    columns=("항목", "값"),
                    rows=rows,
                )
            ],
        ),
        metadata={
            "resultCount": result_count,
        },
    )
```

output 함수가 받는 값은 다음과 같다.

```python
context.data_items()               # terminal 결과에서 추출한 data 목록
context.raw_result                 # terminal 원본 structuredContent/집계 결과
context.parameters                 # 추출·ACTION 반영 파라미터 복사본
context.request_context            # access_token을 제외한 안전한 요청 문맥
context.workflow_results           # 시나리오 전체 MCP 개별·집계 결과
context.results_for("STEP_CODE")   # 특정 step 결과만 반환
context.workflow                   # results/by_step/batches/execution 직렬화 정보
```

`context.request_context` 출력용 안전 문맥에는 다음이 포함된다.

```text
employee_id
session_id
thread_id
agent_code
scenario_code
detail_scenario_code
endpoint
recruitment_org_type_code
user.id
user.deptcode
user.deptname
mcp_workflow             # 함수형 다단계 MCP 호출 원장
```

출력 함수에는 `access_token`이 전달되지 않는다.

## 17. output 반환값의 의미

`ScenarioMcpOutput`은 다음 네 부분으로 구성된다.

| 필드 | 사용 위치 |
|---|---|
| `data` | tester/trace와 프론트 내부 정제 결과 확인용 임의 JSON 구조 |
| `answer.text` | 최종 답변 본문과 SSE `token`/`messages[].content` |
| `answer.renderables` | 표·카드·파일 등 `messages[].metadata.renderables` |
| `metadata` | 시나리오 전용 추적·확장 metadata |

`data`는 `objId/objVal` 목록일 필요가 없다. 다음을 자유롭게 넣을 수 있다.

- 선택된 공통 컬럼
- 평탄화한 grid row 목록
- 페이지별 요약
- 합계·건수·그룹화 결과
- 앞선/후속 MCP 결과 조합
- 차트용 JSON
- tester에서 확인할 raw 일부

`answer.text`는 비어 있을 수 없다. 비어 있으면 결과 어댑터가 포맷 오류로
판정하고 안전한 오류 답변으로 전환한다.

## 18. grid·페이지·다단계 결과 전처리

단건 MCP의 표준 data만 필요하면 `context.data_items()`를 사용한다. MCP 결과가
비표준이거나 중첩 grid라면 `context.raw_result`를 직접 해석한다.

페이지 또는 `call_many()` 집계 결과의 terminal 구조는 대략 다음과 같다.

```python
{
    "data": [
        # 성공한 모든 호출의 data 항목, _function_call metadata 포함
    ],
    "batches": [
        {
            "index": 0,
            "toolName": "...",
            "requestId": "...",
            "arguments": {...},
            "succeeded": True,
            "outcome": "SUCCESS",
            "result": {...},
            "error": None,
        }
    ],
    "execution": {
        "mode": "pagination",
        "callCount": 2,
        "successCount": 2,
        "noDataCount": 0,
        "errorCount": 0,
        "pageCount": 2,
        "stopReason": "no_next_key",
    },
}
```

동일 `objId`가 여러 번 나올 수 있으므로 dict comprehension으로 무조건 바꾸면
뒤 값이 앞 값을 덮어쓴다. 페이지별 grid를 모두 합쳐야 하면 `items` 전체를
순회하거나 `raw_result["batches"]`를 페이지별로 해석한다.

다단계 호출에서는 다음 패턴을 사용한다.

```python
code_results = context.results_for("RP_CUSTOM_CODES")
detail_results = context.results_for("RP_CUSTOM_DETAILS")

for execution in detail_results:
    raw = execution.result
    arguments = execution.arguments
    # 호출별 arguments와 결과를 연결하여 전처리
```

## 19. registry에 handler와 output 연결

`app/mcp/scenarios/registry.py`의 `SCENARIO_HANDLER_REGISTRY` RP 구간에 추가한다.

```python
("RP", "RP_CUSTOM_DETAIL"): _spec(
    "rp.rp_custom_detail.v1",
    rp.rp_custom_detail,
    output_handler=rp.rp_custom_detail_output,
    output_handler_code="rp.rp_custom_detail_output.v1",
),
```

예상 위치는 다음과 같다.

```python
SCENARIO_HANDLER_REGISTRY = {
    # 기존 에이전트...

    ("RP", "APARTMENT_MANAGEMENT_FEE_AUTOPAY_GUIDE"): ...,
    ("RP", "CITY_GAS_AUTOPAY_GUIDE"): ...,
    ("RP", "SOCIAL_INSURANCE_AUTOPAY_GUIDE"): ...,
    ("RP", "ELECTRICITY_TV_FEE_AUTOPAY_GUIDE"): ...,
    ("RP", "SAMSUNG_POSTPAID_HIPASS_CARD_GUIDE"): ...,
    ("RP", "PAYMENT_NOTIFICATION_SERVICE_GUIDE"): ...,
    ("RP", "PAYMENT_DATE_CREDIT_PERIOD_GUIDE"): ...,
    ("RP", "APARTMENT_RP_LIST"): ...,
    ("RP", "COMPOSITE_CONVERSION_SCORE"): ...,
    ("RP", "COMPOSITE_CONVERSION_EXCLUDED"): ...,

    ("RP", "RP_CUSTOM_DETAIL"): _spec(
        "rp.rp_custom_detail.v1",
        rp.rp_custom_detail,
        output_handler=rp.rp_custom_detail_output,
        output_handler_code="rp.rp_custom_detail_output.v1",
    ),
}
```

다음 값이 정확히 일치해야 한다.

```text
manifest detail code             RP_CUSTOM_DETAIL
rp.py ACTION detail code         RP_CUSTOM_DETAIL
registry key detail              RP_CUSTOM_DETAIL
추천질문 후속 detail             RP_CUSTOM_DETAIL
테스트 SubagentResult detail     RP_CUSTOM_DETAIL
```

RAG가 아닌 일반 MCP detail은 handler와 output handler를 둘 다 등록해야 한다.
현재 테스트는 모든 비-RAG handler에 output handler가 있는지 확인한다.

## 20. 최종 답변과 프론트 SSE 출력 경로

output 함수의 데이터는 다음 경로를 통과한다.

```text
rp_custom_detail_output()
  -> ScenarioMcpOutput
  -> app/mcp/result_adapters.py:adapt_mcp_result()
  -> McpExecutionResult.formatted_result
  -> app/answers.py:DefaultAnswerService.prepare()
  -> app/api.py OUTPUT 가드레일
  -> SSE token/messages/recommendedQuestions
```

본문 경로:

```text
ScenarioAnswer.text
  -> formatted_result.answer_text
  -> PreparedAnswer.tokens
  -> OUTPUT 가드레일 문장 검사
  -> SSE event=token
  -> SSE event=messages의 assistant.content
```

표 경로:

```text
ScenarioAnswer.renderables
  -> formatted_result.renderables
  -> PreparedAnswer.renderables
  -> table markdown OUTPUT 가드레일
  -> messages[].metadata.renderables
```

추천질문 경로:

```text
manifest.recommended_questions
  -> RecommendedQuestionRegistry
  -> SSE event=recommendedQuestions
  -> messages[].metadata.recommendedQuestions
```

가드레일은 API 공통 경계에서 자동 적용된다. 시나리오가 만든 고정 본문, RAG
답변 본문과 Markdown table도 기존 공통 OUTPUT 가드레일을 통과한다. 신규
시나리오를 위해 `app/api.py`에 별도 가드레일 코드를 추가하지 않는다.

## 21. 추천질문 0~N개

추천질문이 없으면 필드를 생략하거나 빈 배열로 둔다.

```yaml
recommended_questions: []
```

일반 추천질문은 문자열 배열로 작성한다.

```yaml
recommended_questions:
  - "지난달 신규 업무 내역도 보여줘"
  - "신규 업무 기준도 알려줘"
```

클릭 후에는 question 문장 그대로 일반 질문으로 전송한다. 특정 detail을
강제 지정하거나 '네'를 자동 실행으로 해석하는 설정은 없다.

## 22. RAG 문서 검색 detail인 경우

신규 업무가 기간계 고정 데이터 조회가 아니라 Databricks 문서 검색이면 다음
구조를 사용한다.

1. manifest와 prompt에 detail 및 `rag_query`, `keywords`를 등록한다.
2. `rp.py`의 MCP handler에서 문서 검색 payload를 직접 구성한다.
3. 같은 파일에 검색 결과 전처리 함수와 해당 detail의 상수를 작성한다.
4. 같은 파일에 검색 임계값, reranking, answerability, 최종 LLM 스트림을 담당하는
   전용 RAG output handler를 작성한다.
5. registry에는 MCP handler와 RAG output handler를 함께 등록하되 일반
   `output_handler`는 두지 않는다.

커스터마이징 값 예시는 다음과 같다. 별도 공통 policy 객체를 만들지 않는다.

```python
RP_FIRST_SEARCH_NUM_RESULTS = 5
RP_FIRST_SEARCH_OUTPUT_TYPE = "summary"
RP_DOCUMENT_CHARACTER_THRESHOLD = 12_000
RP_SECOND_SEARCH_NUM_RESULTS = 30
RP_SECOND_SEARCH_OUTPUT_TYPE = "full"
RP_RETRIEVAL_SCORE_THRESHOLD = 0.58
RP_RERANKING_ENABLED = True
RP_RERANKING_SCORE_THRESHOLD = 0.48
RP_RERANKING_TOP_N = 5
```

registry 예시는 다음과 같다.

```python
("RP", "RP_NEW_DOCUMENT_SEARCH"): _spec(
    "rp.new_document_search.v1",
    rp.new_document_search,
    rag_output_handler=rp.new_document_rag_output,
    rag_output_handler_code="rp.new_document_rag_output.v1",
),
```

전용 RAG output handler가 원본 문서를 RP 문서 구조로 전처리하고 retrieval 임계값,
선택적 reranking, answerability 검사와 최종 답변 LLM까지 처리한다. 일반 기간계
조회용 `output_handler`와 RAG output handler를 동시에 적용하지 않는다.

RP 문서 검색은 현재 요청의 모든 RP RAG detail 1차 `content` 글자 수 합계가
`document_character_threshold`를 초과할 때만 각 detail을
`second_search_num_results` 건으로 최대 한 번 재조회한다. 임계값 이하이면 모든
1차 결과가 최종 RAG 입력이 되고 Reranking은 생략된다. 초과하면 모든 2차 결과를
합쳐 선택적으로 Reranking하고, 답변 가능성 판별과 최종 LLM은 각각 한 번만
실행한다. 이 값은 `rp.py` 상단의 상수에서 변경한다.

## 23. MCP가 없는 고정답변 detail인 경우

MCP 없이 안내 문구만 반환할 detail은 `app/subagents/fixed_responses.py`에 등록한다.

```python
(
    "RP",
    "RP_CUSTOM_INFORMATION",
): SubagentFixedResponse(
    message="자세한 내용은 RP 업무 담당 채널을 통해 확인해 주세요."
),
```

이 경우 필요한 항목은 다음과 같다.

- RP manifest detail 등록
- RP scenario/system 분류 규칙
- 필요 시 master RP 경계
- `SUBAGENT_FIXED_RESPONSES` 등록

이 경우에는 다음이 필요 없다.

- `rp.py` MCP handler
- registry MCP handler
- MCP output handler

고정답변도 최종 OUTPUT 가드레일을 통과한 뒤 token으로 스트리밍된다.

## 24. 개발용 MCP mock

`MCP_BACKEND=mock`이면 `app/mcp/client.py`의
`_build_mock_structured_content()`가 결과를 만든다. 현재 일반 도구의 기본 mock은
대략 다음 형태다.

```json
{
  "data": [
    {"objId": "column1", "objVal": "param1 값"},
    {"objId": "column2", "objVal": "param2 값"}
  ]
}
```

신규 output handler가 `targetName`, `resultCount` 같은 실제 컬럼을 요구하면 기본
mock에서는 값이 비게 된다. 다음 중 하나를 선택한다.

1. 개발 초기 output 함수가 `column1`, `column2`를 읽게 한다.
2. `_build_mock_structured_content()`에 `rp_custom_tool` 전용 branch를 추가한다.
3. 단위 테스트 fake executor가 실제 운영 응답 fixture를 반환하게 한다.

운영 결과 전처리 정확도는 3번으로 검증하는 것을 권장한다. mock 공통 코드를
업무별 예외로 계속 키우기보다 실제 `structuredContent` fixture를 테스트에 두면
시나리오별 계약이 명확해진다.

## 25. 테스트 작성 범위

최소 다음을 검증한다.

### 25.1 설정·등록

- 활성 RP manifest가 정상 로딩된다.
- 신규 detail의 parameter schema가 정상 생성된다.
- `get_scenario_handler_spec("RP", "RP_CUSTOM_DETAIL")`이 존재한다.
- 비-RAG detail의 output handler가 존재한다.
- 추천질문 후속 detail 참조가 유효하다.

### 25.2 handler 입력과 MCP 호출

- 질문 파라미터가 실제 MCP arguments 이름으로 정확히 바뀐다.
- 기본 조회 월 정책이 요구사항과 일치한다.
- `employee_id`, `session_id`, `thread_id` 중 필요한 값이 전달된다.
- `access_token`, 부서코드 등 필요한 request context가 전달된다.
- tool 이름과 stable step code가 정확하다.
- 단건·N건·페이지 호출 횟수가 정확하다.
- 다단계에서 첫 결과로 두 번째 arguments가 정확히 생성된다.

### 25.3 ACTION/HITL

- 필수 파라미터가 없으면 지정 ACTION이 나온다.
- 한 ACTION에 여러 입력 필드가 모두 나온다.
- invalid `humanInput`이면 같은 ACTION과 필드별 `errors`가 나온다.
- valid 입력이면 파라미터가 갱신되고 MCP가 실행된다.
- `invalidate_step_codes` 대상만 다시 호출된다.
- 기존 `session_id`, `thread_id`, `endpoint` 재사용이 검증된다.

### 25.4 결과와 프론트 출력

- 실제 운영계 `structuredContent.data` fixture가 정상 파싱된다.
- 중복 `objId`, 중첩 grid, 여러 페이지가 손실 없이 처리된다.
- `ScenarioMcpOutput.data`가 예상 구조다.
- `answer_text`가 비어 있지 않고 정확하다.
- Markdown table의 columns/rows가 정확하다.
- 추천질문 0개와 여러 개가 모두 정상이다.
- `NO_DATA`와 `ERROR`가 각 시나리오 정책대로 답변 또는 ACTION이 된다.

### 25.5 분류

- 대표 질문이 마스터에서 RP로 분류된다.
- RP 서브에이전트가 신규 detail을 선택한다.
- 유사 질문이 기존 RP detail로 잘 구분된다.
- 일반 실적 질문이 PERFORMANCE_FEE로 유지된다.
- 다른 에이전트 질문에 신규 detail이 선택되지 않는다.

기존 테스트 참고 위치는 다음과 같다.

- `tests/test_current_configuration.py`
- `tests/test_mcp_function_handlers.py`
- `tests/test_scenario_actions.py`
- `tests/test_mcp_response_compatibility.py`
- `tests/test_developer_trace.py`

검증 명령:

```text
python -m ruff check app tests
python -m compileall -q app tests
python -m unittest discover -s tests -q
```

## 26. tester에서 확인할 추적 순서

`/tester` 또는 `/chatting`에서 개발 trace를 활성화하고 다음 순서로 확인한다.

1. 원본 사용자 질문과 INPUT 가드레일 결과
2. 조회된 대화 이력
3. 마스터 보정 질문
4. 마스터 `agent_code=RP`
5. 서브에이전트 `scenario_code`
6. 서브에이전트 `detail_scenario_code=RP_CUSTOM_DETAIL`
7. 추출된 `parameters`
8. registry `handlerCode`
9. handler 파일·함수 `codeLocation`
10. 각 step의 tool 이름과 arguments
11. MCP request ID, outcome, business code
12. 호출별 raw result와 aggregate 결과
13. output handler code
14. 전처리 `data`, 답변 본문, renderable 개수
15. OUTPUT 가드레일 결과
16. 최종 token/messages/recommendedQuestions 이벤트

운영 프론트에는 `trace`가 기본으로 전송되지 않는다. 개발 요청 헤더로 trace를
명시적으로 활성화했을 때만 SSE `trace` 이벤트가 추가된다.

## 27. 자주 발생하는 오류와 원인

| 증상 | 주된 원인 | 확인 위치 |
|---|---|---|
| 마스터가 RP가 아닌 다른 agent 선택 | master RP 범위·충돌 규칙 누락 | `prompts/intent-classification` |
| RP지만 다른 detail 선택 | RP system/scenario 설명이 모호함 | RP `system.md`, scenario `.md` |
| 서버 시작 시 handler 없음 | manifest detail만 추가하고 registry 누락 | `app/mcp/scenarios/registry.py` |
| payload 설정 없음 | registry 코드 불일치 또는 legacy 경로 진입 | manifest/registry/handler code |
| MCP arguments가 빈 값 | parameter 이름 불일치 또는 request context 매핑 누락 | `rp.py` handler |
| `access_token`이 MCP에 없음 | arguments에 직접 넣지 않음 | `rp.py` handler |
| ACTION 반복 | `input_code`와 `humanInput.code` 불일치 | ACTION 정의와 프론트 요청 |
| ACTION 뒤 MCP가 재호출되지 않음 | checkpoint 재사용, invalidate 누락 | `invalidate_step_codes` |
| ACTION 뒤 앞선 MCP까지 다시 호출 | invalidate 범위를 너무 넓게 지정 | ACTION 정의 |
| 결과가 안전 오류 문구로 바뀜 | output handler 예외·빈 answer | `rp.py` output, trace error |
| 표가 비어 있음 | mock/운영 objId와 output 컬럼 불일치 | raw result와 output 함수 |
| 페이지 일부만 보임 | 중복 objId를 dict로 덮어씀 | output 함수의 평탄화 로직 |
| 추천질문이 안 나옴 | manifest 위치·형식 오류 또는 PASS가 아님 | manifest, API 결과 상태 |
| 단순 `네`가 실행 안 됨 | 실행 가능한 긍정 추천질문이 여러 개 | recommendation ID 사용 |

## 28. 배포 전 최종 체크리스트

일반 RP MCP 조회 detail이라면 다음 항목을 모두 확인한다.

```text
[ ] 실제 업무의 scenario/detail/handler/output/step/action/tool 코드를 확정했다.
[ ] RP manifest parameter_definitions에 신규 파라미터를 등록했다.
[ ] detail.parameters에는 그 detail이 쓰는 키만 등록했다.
[ ] RP manifest scenarios/details에 신규 detail을 등록했다.
[ ] 추천질문이 없으면 생략하거나 []로 두었다.
[ ] 세부 scenario Markdown에 선택·제외·파라미터 예시를 작성했다.
[ ] manifest.prompt_files에 새 Markdown을 명시했다.
[ ] RP system.md에 분류 우선순위와 파라미터 규칙을 추가했다.
[ ] 필요 시 master agents/rp.md와 router/system.md 경계를 조정했다.
[ ] rp.py에 MCP handler를 작성했다.
[ ] 실제 MCP schema대로 tool_name과 arguments를 작성했다.
[ ] 사용자 access token이 필요하면 arguments에 명시적으로 넣었다.
[ ] 추가 입력이 필요하면 Python ACTION을 등록하고 require()를 호출했다.
[ ] ACTION 재조회 범위에 맞게 invalidate_step_codes를 지정했다.
[ ] 무결과·오류 시 답변 또는 재입력 정책을 명시했다.
[ ] rp.py에 detail 전용 output handler를 작성했다.
[ ] 원본 결과의 중복 objId, grid, 페이지 구조를 고려했다.
[ ] ScenarioAnswer.text가 항상 비어 있지 않다.
[ ] 필요한 table/card/file renderable만 추가했다.
[ ] registry.py에 handler와 output handler를 연결했다.
[ ] manifest·ACTION·registry·테스트의 detail 코드가 완전히 같다.
[ ] RAG라면 rp.py에 전용 정책·전처리·RAG output을 만들고 일반 output handler를 중복 적용하지 않았다.
[ ] 고정답변이라면 fixed_responses에 등록하고 MCP handler를 만들지 않았다.
[ ] 실제 structuredContent fixture 기반 테스트를 추가했다.
[ ] ACTION valid/invalid/재개 테스트를 추가했다.
[ ] NO_DATA/ERROR 정책 테스트를 추가했다.
[ ] 전체 테스트를 통과했다.
[ ] 서버를 재시작해 활성 prompt와 registry를 다시 로드했다.
[ ] tester에서 분류부터 최종 SSE까지 전체 trace를 확인했다.
```

## 29. 일반 조회 detail의 최소 수정 세트

가장 일반적인 단건 MCP 조회라면 실제 필수 수정은 다음 여섯 단계다.

```text
1. prompts/subagents/rp/<active>/manifest.yaml
   - parameter definition + detail + 추천질문

2. prompts/subagents/rp/<active>/scenarios/*.md
   - 선택/제외 예시 + 파라미터 추출 규칙

3. prompts/subagents/rp/<active>/system.md
   - 기존 detail과의 우선순위

4. app/mcp/scenarios/rp.py
   - async handler: tool/arguments/call
   - output handler: data/answer/renderables

5. app/mcp/scenarios/registry.py
   - ("RP", detail) -> handler/output 연결

6. tests/
   - 실제 입력 arguments와 structuredContent 출력 fixture 검증
```

신규 업무가 기존 RP 범위를 넓히는 경우에만 마스터 분류 prompt 수정이 추가된다.
ACTION, RAG, 고정답변, mock 변경은 해당 기능을 사용할 때만 추가한다.

## 30. 관련 코드와 상세 문서

- RP 시나리오 구현: [`app/mcp/scenarios/rp.py`](../app/mcp/scenarios/rp.py)
- detail handler registry: [`app/mcp/scenarios/registry.py`](../app/mcp/scenarios/registry.py)
- 함수형 MCP runtime: [`app/mcp/scenario_runtime.py`](../app/mcp/scenario_runtime.py)
- output 계약: [`app/mcp/scenarios/contracts.py`](../app/mcp/scenarios/contracts.py)
- MCP 결과 어댑터: [`app/mcp/result_adapters.py`](../app/mcp/result_adapters.py)
- JSON-RPC와 운영계 응답 파싱: [`app/mcp/client.py`](../app/mcp/client.py)
- ACTION 계약: [`app/scenario_actions.py`](../app/scenario_actions.py)
- 표·확장 데이터: [`app/renderables.py`](../app/renderables.py)
- 추천질문: [`app/recommended_questions.py`](../app/recommended_questions.py)
- RP RAG 정책·전처리·최종 답변: [`app/mcp/scenarios/rp.py`](../app/mcp/scenarios/rp.py)
- 고정답변: [`app/subagents/fixed_responses.py`](../app/subagents/fixed_responses.py)
- 최종 답변 조합: [`app/answers.py`](../app/answers.py)
- 가드레일·SSE 출력: [`app/api.py`](../app/api.py)
- RP 활성 manifest: [`prompts/subagents/rp/v1/manifest.yaml`](../prompts/subagents/rp/v1/manifest.yaml)
- RP 내부 분류 prompt: [`prompts/subagents/rp/v1/system.md`](../prompts/subagents/rp/v1/system.md)
- 마스터 RP 경계: [`prompts/intent-classification/v1/agents/rp.md`](../prompts/intent-classification/v1/agents/rp.md)
- 프롬프트·시나리오 일반 가이드: [04](04_PROMPT_AGENT_SCENARIO_CUSTOMIZATION.md)
- 함수 중심 MCP 가이드: [15](15_FUNCTION_FIRST_MCP_INPUT_OUTPUT.md)
- 결과/action 정책: [17](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)
- 개발 추적 화면: [13](13_DEVELOPMENT_TRACE_CONSOLE.md)
