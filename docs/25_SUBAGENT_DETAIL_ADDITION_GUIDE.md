# 서브에이전트 신규 세부 시나리오 추가 완전 가이드

## 1. 목적

이 문서는 `PERFORMANCE_FEE`, `QUALIFICATION`, `RP` 중 하나에 새로운 세부
시나리오를 추가하는 전체 절차를 실행 유형별로 설명한다.

지원하는 네 가지 구현 유형은 다음과 같다.

1. MCP 없는 고정답변
2. MCP 데이터 조회 + 정해진 포맷 답변
3. Databricks 문서 검색 + RAG 답변
4. 사용자 Action/HITL 입력 + MCP 실행

## 2. 현재 실행 구조

```text
마스터 AGENT 분류
  -> 최종 agent 범위 Redis user 이력 저장
  -> 서브 LLM이 matches[] 분류 및 parameters 추출
  -> detail별 실행 방식 판정
       fixed response -> MCP 생략
       MCP/RAG        -> Python handler 실행
       parameter 부족 -> Action 저장 후 종료
  -> detail별 output 또는 agent별 batch RAG output
  -> 여러 답변을 match 순서대로 조합
  -> OUTPUT 가드레일
  -> SSE token/messages/renderables/recommendedQuestions/end
```

YAML은 분류와 파라미터 schema를 관리한다. MCP 도구명, payload, 호출 순서,
결과 parsing, Action은 Python에서 관리한다.

## 3. 추가 전에 확정할 식별자

다음 값을 먼저 표로 만든다.

| 구분 | 예시 | 사용 위치 |
|---|---|---|
| `agent_code` | `RP` | master/subagent/registry/history |
| `scenario_code` | `RP_NEW_SERVICE` | manifest 상위 업무 |
| `detail_scenario_code` | `RP_NEW_SERVICE_GUIDE` | 핵심 실행 식별자 |
| handler code | `rp.new_service.v1` | MCP 실행 trace |
| output handler code | `rp.new_service_output.v1` | 결과 전처리 trace |
| step code | `RP_NEW_SERVICE_SEARCH` | MCP checkpoint |
| action code | `INPUT_TARGET` | 프론트 humanInput 연계 |
| tool name | `new_service_tool` | 실제 GenOS MCP |

코드 규칙은 다음을 권장한다.

- scenario/detail/action/step: 대문자 `SNAKE_CASE`
- handler code: 소문자 점 구분 + 버전
- 운영된 코드는 Redis와 trace에 남으므로 의미가 같으면 이름을 바꾸지 않음
- 같은 agent 안의 detail 코드는 반드시 고유
- 동일 tool을 사용해도 답변 형식이 다르면 detail output 함수는 분리

## 4. 버전 폴더 준비

현재 활성 버전은 다음 파일에서 확인한다.

```text
prompts/subagents/<agent>/active.yaml
```

운영 변경이면 기존 폴더를 직접 덮지 말고 새 버전을 권장한다.

```text
prompts/subagents/rp/v1 -> prompts/subagents/rp/v2
manifest.version        -> 2.0.0 또는 운영 규칙에 맞는 값
manifest.released_at    -> 배포 예정일
active.yaml             -> 검증 완료 후 v2
```

Python handler registry는 prompt 버전별 registry가 아니다. 새 버전 detail을 등록한
Python 코드와 active version 전환을 같은 배포 단위로 관리한다.

## 5. manifest parameter 정의

LLM이 추출할 값은 먼저 `parameter_definitions`에 등록한다.

```yaml
parameter_definitions:
  target_code:
    description: >
      신규 업무 조회 대상 코드. 사용자가 명시한 경우에만 추출하고 없으면 null
    pattern: "^[A-Za-z0-9_-]+$"

  service_type:
    description: "서비스 구분. 질문에 없으면 null"
    allowed_values:
      - "TYPE_A"
      - "TYPE_B"

  keywords:
    value_type: "string_list"
    min_items: 1
    max_items: 10
    description: "문서 검색용 핵심 업무 키워드"
```

지원되는 기본 값 유형은 현재 다음과 같다.

- `value_type: string` 또는 생략: `str | None`
- `value_type: string_list`: `list[str] | None`
- `allowed_values`: 문자열 enum + `None`
- `pattern`: 문자열 정규식
- `min_items`, `max_items`: 문자열 배열 길이

숫자처럼 보이는 값도 LLM 단계에서는 문자열로 받고 handler에서 안전하게 변환하는
편이 좋다. `int("")` 같은 오류를 피하려면 다음처럼 검증한다.

```python
raw_value = str(context.parameters.get("count") or "").strip()
count = int(raw_value) if raw_value.isdigit() else DEFAULT_COUNT
```

업무상 기본값은 두 위치 중 하나를 선택한다.

- 분류 후 항상 같은 기본값: manifest `parameter_defaults`
- MCP 종류·날짜·다른 값에 따라 달라지는 기본값: agent Python handler

현재 운영 방향은 변동성이 큰 payload 규칙을 Python handler에 두는 것이다.

## 6. manifest scenario/detail 등록

```yaml
scenarios:
  - code: "RP_NEW_SERVICE"
    name: "RP 신규 서비스"
    description: "신규 서비스 조회와 안내"
    details:
      - code: "RP_NEW_SERVICE_DETAIL"
        name: "RP 신규 서비스 상세"
        description: >
          사용자가 신규 서비스의 실제 데이터를 조회하려는 경우 선택
        parameters:
          - "target_code"
        recommended_questions:
          - "지난달 신규 서비스 내역도 보여줘"
```

기존 상위 업무에 속하면 새 scenario를 만들지 않고 기존 `details`에 추가한다.
파라미터가 없으면 반드시 명확히 빈 배열로 둔다.

```yaml
parameters: []
```

### 시작 시 자동 검증

`SubagentPromptLoader._validate_manifest()`가 다음을 검증한다.

- scenario 코드 중복
- detail 코드 중복
- parameters가 배열인지
- parameters가 선언된 parameter definition만 참조하는지
- parameter default가 detail parameters 안에 있는지
- default가 allowed values에 포함되는지
- required match rule이 존재하는 detail만 참조하는지
- 모든 detail이 fixed response 또는 MCP handler를 갖는지

신규 detail은 아래 둘 중 정확히 하나의 실행 경로를 가져야 한다.

```text
fixed_responses.py 등록
또는
mcp/scenarios/registry.py 등록
```

둘 다 등록하면 현재 Graph는 fixed response를 우선하여 MCP를 호출하지 않는다.
혼동을 막기 위해 한 가지만 등록한다.

## 7. scenario Markdown 작성

새 파일 예시:

```text
prompts/subagents/rp/v2/scenarios/05_new_service.md
```

권장 템플릿:

```markdown
# RP 신규 서비스

## RP_NEW_SERVICE_DETAIL

### 선택하는 질문

- ABC 코드의 신규 서비스 내역을 조회해줘
- 신규 서비스 처리 상태를 보여줘

### 선택하지 않는 질문

- 신규 서비스의 신청 기준만 알려줘
  - RAG 안내 detail 대상
- 내 전체 실적을 보여줘
  - PERFORMANCE_FEE 대상
- 아파트 연결 가능 단지를 찾아줘
  - APARTMENT_RP_LIST 대상

### 파라미터

- 질문에 실제 대상 코드가 있으면 `target_code`로 추출한다.
- 대상 코드가 없으면 `null`이다.
- 사용자가 말하지 않은 값을 임의로 생성하지 않는다.

### 복합 질문

- 다른 독립 요청이 함께 있으면 각각의 detail을 `matches`에 한 번씩 반환한다.
- 같은 detail은 중복 반환하지 않는다.
```

manifest의 `prompt_files`에 파일을 추가한다. 선언하지 않아도 loader가 모든 Markdown을
자동 결합하지만, 순서 제어와 추적성을 위해 반드시 명시하는 것을 권장한다.

RP처럼 `system.md`를 마지막에 두어 최종 우선순위를 가까이 적용하는 agent는 기존
순서를 유지한다.

## 8. subagent system prompt 수정

`system.md`에는 scenario 문서보다 압축된 최종 판정표를 둔다.

```markdown
- 실제 데이터·건수·금액·상태 조회가 명확하면 `RP_NEW_SERVICE_DETAIL`을 선택한다.
- 정의·신청 절차·제한 기준 질문이면 문서 RAG detail을 선택한다.
- 대상 코드가 없다는 이유만으로 다른 detail을 선택하지 않는다.
- 필수 대상 코드는 임의 생성하지 않고 null로 반환하며 Python Action이 받는다.
```

LLM이 자주 혼동하는 인접 detail의 양방향 반례를 반드시 작성한다.

```text
실제 값/목록/상태 조회 -> 조회 detail
방법/절차/기준/정의    -> RAG detail
사용자 확인만 필요     -> 고정답변 detail
```

## 9. 마스터 agent 경계 수정 여부

마스터는 detail을 보지 않고 agent 하나만 선택한다. 신규 detail이 기존 agent 설명에
이미 명확히 포함되면 master prompt는 건드리지 않아도 된다.

다음 경우에는 수정한다.

- 기존 agent 역할에 없는 새 업무
- 다른 agent와 키워드가 겹침
- 프론트 선택 agent와 master 분류가 반복적으로 달라짐
- 개인정보/예외 규칙을 master에서 서브로 이전함

수정 위치:

```text
prompts/intent-classification/v1/agents/<agent>.md
prompts/intent-classification/v1/router/system.md  # 공통 우선순위가 필요할 때
```

마스터 agent 코드 자체를 새로 만드는 것이 아니라 기존 agent에 detail만 추가하는
경우 `prompts/intent-classification/v1/manifest.yaml.agent_code`는 변경하지 않는다.

## 10. 유형 A: MCP 없는 고정답변 추가

### 10.1 manifest

```yaml
- code: "NEW_FIXED_GUIDANCE"
  name: "신규 고정 안내"
  description: "조회 없이 정해진 안내문을 반환"
  parameters: []
  recommended_questions: []
```

### 10.2 fixed response

`app/subagents/fixed_responses.py`:

```python
(
    "RP",
    "NEW_FIXED_GUIDANCE",
): SubagentFixedResponse(
    default_message="공통 안내 문구입니다.",
    messages_by_recruitment_org_type={
        "11": "일반 모집인 안내 문구입니다.",
        "12": "제휴 모집인 안내 문구입니다.",
        "13": "복합 모집인 안내 문구입니다.",
    },
),
```

### 10.3 자동 처리되는 부분

- `_after_subagent`: 모든 match가 fixed면 MCP 노드 생략
- `_call_mcp`: 복합 match에서 fixed 항목만 건너뜀
- `DefaultAnswerService.prepare`: 조직구분별 문구 선택
- API: OUTPUT 가드레일과 token/messages SSE 전송

고정답변을 위해 registry, MCP handler, output handler를 만들지 않는다.

## 11. 유형 B: 일반 MCP 조회 detail 추가

### 11.1 agent 파일에 handler 작성

`app/mcp/scenarios/<agent>.py`:

```python
async def new_service_detail(
    context: ScenarioMcpHandlerContext,
) -> McpExecutionResult:
    target_code = str(context.parameters.get("target_code") or "").strip()

    arguments = {
        "target_code": target_code,
        "employee_id": context.employee_id,
        "session_id": context.session_id,
        "thread_id": context.thread_id,
        "access_token": str(context.request_context.get("access_token") or ""),
    }

    return await context.call(
        step_code="NEW_SERVICE_LOOKUP",
        tool_name="new_service_tool",
        arguments=arguments,
    )
```

payload는 해당 detail 함수 안에서 직접 보이도록 유지한다. 변동성이 큰 필드를 공통
정책이나 YAML에 숨기지 않는다.

`request_context`에서 일반적으로 꺼낼 수 있는 값은 다음과 같다.

- `access_token`
- `endpoint`
- `recruitment_org_type_code`
- `user.id`
- `user.deptcode`
- `user.deptname`

### 11.2 output handler 작성

```python
def new_service_detail_output(
    context: ScenarioMcpOutputContext,
) -> ScenarioMcpOutput:
    data = extract_required_rows(context.execution)
    table_rows = [
        (
            str(row.get("target_code") or ""),
            str(row.get("status") or ""),
        )
        for row in data
    ]

    return ScenarioMcpOutput(
        data=data,
        answer=ScenarioAnswer(
            text="신규 서비스 조회 결과입니다.",
            renderables=[
                create_table_renderable(
                    code="new-service-result-table",
                    title="신규 서비스 조회 결과",
                    format="markdown",
                    columns=("대상코드", "처리상태"),
                    rows=table_rows,
                )
            ],
        ),
        metadata={"rowCount": len(table_rows)},
    )
```

필요 import도 함께 추가한다.

```python
from app.renderables import ScenarioAnswer, create_table_renderable
from app.mcp.scenarios.contracts import ScenarioMcpOutput, ScenarioMcpOutputContext
```

실제 프로젝트의 계약 클래스와 helper 이름은
`app/mcp/scenarios/contracts.py`, `helpers.py`, 기존 agent output 함수를 기준으로
선택한다. 결과 parsing은 실제 MCP fixture를 먼저 확보하고 작성한다.

### 11.3 registry 연결

`app/mcp/scenarios/registry.py`:

```python
(
    "RP",
    "RP_NEW_SERVICE_DETAIL",
): _spec(
    "rp.new_service_detail.v1",
    rp.new_service_detail,
    output_handler=rp.new_service_detail_output,
    output_handler_code="rp.new_service_detail_output.v1",
),
```

manifest detail 코드와 registry tuple의 문자열이 정확히 같아야 한다.

## 12. 유형 C: Databricks RAG detail 추가

RAG는 단순 조회 detail과 달리 동일 agent의 여러 RAG matches를 한 번의 최종 답변으로
묶는다.

### 12.1 manifest parameters

```yaml
parameters:
  - "rag_query"
  - "keywords"
```

`rag_query`는 마스터 보정 질문에서 해당 detail의 독립 요청만 분리한 검색 질문이고,
`keywords`는 하이브리드 검색 키워드 배열이다. 사용자 Action으로 검색어를 다시
받지 않는다.

### 12.2 agent 파일의 mapping

RP 예시:

```python
RP_DOCUMENT_NUMBER_BY_DETAIL_CODE = {
    # 기존 항목...
    "RP_NEW_SERVICE_GUIDE": "문서번호",
}

RP_RAG_DETAIL_CODES = frozenset(RP_DOCUMENT_NUMBER_BY_DETAIL_CODE)
```

Qualification도 해당 파일의 detail-document mapping에 추가한다.

### 12.3 1차 검색 handler

handler는 `context.rag_search_pass`를 보고 1차/2차의 `output_type`,
`num_results`를 명시적으로 나눈다. 정책을 공통 YAML에 숨기지 않고 해당 agent
파일에서 확인 가능하게 둔다.

```python
if context.rag_search_pass == "SECOND":
    output_type = "...2차 형식..."
    num_results = 30
else:
    output_type = "...1차 형식..."
    num_results = 10
```

문서 filter는 Python dict를 JSON 문자열로 직렬화하는 방식을 권장한다.

```python
document_filter = json.dumps(
    {"blsm_doc_id": [document_number]},
    ensure_ascii=False,
)
```

로그에서 `\"`가 보이는 것은 JSON 문자열의 표현일 수 있으므로 실제 전송 payload와
로그 표현을 구분한다.

### 12.4 registry RAG 연결

```python
(
    "RP",
    "RP_NEW_SERVICE_GUIDE",
): _spec(
    "rp.new_service_guide.v1",
    rp.rp_document_search,
    rag_output_handler=rp.rp_document_rag_output,
    rag_output_handler_code="rp.rp_document_rag_output.v1",
),
```

같은 agent의 RAG detail들은 현재 AnswerService에서 동일한 batch output handler를
사용해야 한다. 서로 다른 RAG handler를 섞으면 명시적인 `ValueError`가 발생한다.

### 12.5 검색 이후

현재 처리 순서는 다음과 같다.

```text
detail별 1차 문서 검색
-> 모든 detail 결과 전처리 글자 수 합산
-> 전체 임계값 초과 시 모든 대상 detail 2차 검색
-> 2차 결과만 최종 후보로 사용
-> 설정에 따라 reranking
-> 문서 없음 고정답변
-> answerability LLM 판정
-> 질문 + 관련 history + 문서로 최종 답변 하나 생성
```

detail별 문서번호, 임계점수, 글자수 임계값, 2차 결과 수, reranking on/off와
고정답변은 해당 agent의 Python 파일에서 관리한다.

## 13. 유형 D: Action/HITL 입력이 필요한 detail

### 13.1 action 정의

`app/mcp/scenarios/<agent>.py`에서 정의하고 모듈 import 시 등록한다.

```python
NEW_TARGET_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="RP",
        detail_scenario_code="RP_NEW_SERVICE_DETAIL",
        action_code="INPUT_TARGET",
        message="조회할 대상 코드를 입력해 주세요.",
        inputs=(
            ScenarioActionInput(
                parameter_name="target_code",
                input_code="INPUT_TARGET",
                label="대상 코드",
                pattern=r"^[A-Za-z0-9_-]+$",
                validation_message="올바른 대상 코드를 입력해 주세요.",
                guardrail_enabled=False,
            ),
        ),
    )
)
```

조회키가 가드레일 마스킹으로 변경되면 MCP가 실패할 수 있는 필드만
`guardrail_enabled=False`로 설정한다. 일반 자유문장은 가드레일을 유지한다.

### 13.2 handler에서 require

```python
values = NEW_TARGET_ACTION.require(context.parameters)
target_code = values["target_code"]
```

값이 없거나 검증에 실패하면 `ScenarioActionRequired`가 발생하고 Graph가 다음을
자동 처리한다.

- action SSE 생성
- 필요한 최소 상태 Redis 저장
- HTTP 요청 종료
- 같은 `thread_id`의 `humanInput` 재진입 검증
- 완료된 이전 MCP step 재사용

### 13.3 결과 기반 action

MCP 결과가 NO_DATA여서 조건을 다시 받아야 하면 `ACTION.request()`를 사용한다.
선행 검색조건이 바뀌어 이전 checkpoint를 버려야 하면
`invalidate_step_codes`를 정확히 지정한다.

## 14. 다중 세부 시나리오 설계

서브 LLM 출력은 `matches` 배열이며 하나의 질문에서 여러 detail이 나올 수 있다.

```json
{
  "matches": [
    {
      "scenario_code": "PERFORMANCE_SUMMARY",
      "detail_scenario_code": "PERFORMANCE_SUMMARY_TOTAL",
      "parameters": {}
    },
    {
      "scenario_code": "FEE_DETAILS",
      "detail_scenario_code": "FEE_ITEM_DETAILS",
      "parameters": {}
    }
  ]
}
```

신규 detail 추가 시 다음을 결정한다.

- 독립 요청과 함께 복수 선택 가능한가
- 특정 detail과는 배타적인가
- 특정 표현이면 반드시 같이 선택되어야 하는가

LLM 누락을 결정적으로 보완해야 할 때만 manifest `required_match_rules`를 사용한다.

```yaml
required_match_rules:
  - name: "신규 업무 요약과 상세 동시 조회"
    all_terms: ["신규업무", "요약", "상세"]
    detail_codes:
      - "RP_NEW_SERVICE_SUMMARY"
      - "RP_NEW_SERVICE_DETAIL"
```

주의: required match rule은 같은 서브에이전트 manifest 안의 detail만 참조할 수 있다.
서로 다른 agent의 detail을 한 서브 결과에 섞을 수 없다.

고정답변과 MCP detail이 함께 선택되면 fixed detail은 MCP 결과 수를 소비하지 않고
최종 답변에서 원래 match 순서에 삽입된다.

## 15. 추천질문 추가

### 일반 추천질문

```yaml
recommended_questions:
  - "지난달 결과도 보여줘"
```

사용자가 누르면 질문 문자열을 새 `/chat` 요청으로 보낸다.

클릭한 문장은 일반 분류를 거치며 별도의 확인형 추천질문은 없다.

## 16. 최종 답변과 renderable

일반 조회 output 함수는 자유롭게 다음을 반환할 수 있다.

- `answer_text`: 최종 token 스트리밍 본문
- `renderables`: 표, 카드, 파일 등 확장 데이터 0~N개
- `metadata`: 행 개수, 기준일 등 운영용 메타데이터

Markdown 표를 본문에 넣거나 renderable table을 별도로 보낼 수 있다. 프론트와
계약이 없다면 본문 Markdown이 단순하지만, 표를 별도 렌더링하려면
`create_table_renderable()`과 `messages[].metadata.renderables` 경로를 사용한다.

RAG output은 source documents와 최종 비동기 답변 스트림을 반환하며, 여러 RAG
details는 한 답변으로 합친다.

## 17. 오류·무데이터 정책

신규 MCP detail은 최소 다음 경우를 정의한다.

| 상황 | 권장 처리 |
|---|---|
| MCP 전송/파싱 오류 | 안전 고정답변, 상세 원인은 로그 |
| 업무 코드 `1001` | detail별 무데이터 고정답변, 미등록 시 공통 문구 |
| 여러 호출이 모두 `1001` | 집계 결과 전체 NO_DATA |
| 일부 성공·일부 오류 | 현재 공통 정책은 전체 안전 오류 |
| RAG 문서 0건 | agent별 문서 없음 고정답변 |
| 문서와 질문 연계 불가 | answerability 고정답변 |

빈 table과 정상 문구를 오류처럼 보내지 않는다. output handler가 예외를 내면
Graph가 안전한 오류 결과로 바꾸고 AnswerService가 table/source를 제거한다.

## 18. 테스트 전략

### 18.1 manifest/schema

- bundle이 로드되는가
- 새 detail이 enum 또는 flat enum에 포함되는가
- parameters에 허용 키만 남는가
- 기본값과 pattern이 적용되는가

### 18.2 고정답변

- default 문구
- 조직구분 `11/12/13`
- unknown code fallback
- MCP 호출 생략
- 조회형 detail은 `SUBAGENT_NO_DATA_RESPONSES`의 1001 문구

### 18.3 MCP handler

- 정확한 tool name
- arguments의 사번/session/thread/token/업무 파라미터
- 빈 문자열과 `None` 변환
- 단건·N건·페이지 호출 수
- NO_DATA/ERROR

### 18.4 output

- 실제 `structuredContent` fixture parsing
- 원하는 컬럼만 추출
- 여러 row 정렬·합계
- 답변 본문과 renderables
- 여러 detail 답변 순서

### 18.5 Action

- 최초 입력 누락
- action code/message/field/order
- 유효 입력 재개
- 잘못된 입력 재요청
- guardrail enabled/disabled
- checkpoint 재사용/무효화

### 18.6 RAG

- detail-document mapping 집합
- detail별 1차 검색
- 전체 전처리 글자 수 합산
- 임계값 이하 1차 결과 유지
- 임계값 초과 전체 2차 검색
- reranking on/off
- 문서 없음 및 answerability 불가
- 여러 detail 문서로 최종 답변 하나

## 19. 개발 로그에서 확인할 순서

```text
요청 도착
사용자 질문 INPUT 가드레일
Redis 이력 조회
마스터 독립 의도분류
에이전트 코드 비교
사용자 질문 Redis 저장
서브에이전트 구조화 결과
세부 시나리오 matches
MCP 필요 여부
handler code / parameter key
MCP 호출 성공 여부 / 결과 건수
output 또는 RAG batch 완료
최종 OUTPUT 가드레일
SSE token/messages/recommendedQuestions/end
assistant Redis 저장
```

원본 MCP 데이터와 전체 문서를 로그에 남기지 말고 tool, 결과 코드, 건수, 글자수,
시간만 확인한다.

## 20. 배포 순서

1. 신규 코드를 확정하고 전체 참조 표를 만든다.
2. 새 prompt version을 준비한다.
3. manifest + scenario/system prompt를 작성한다.
4. fixed response 또는 Python handler/registry를 같은 변경에 추가한다.
5. master 경계가 필요한 경우 같은 배포에 포함한다.
6. 단위 테스트와 실제 MCP fixture 테스트를 통과한다.
7. 로컬 tester에서 분류·payload·output을 확인한다.
8. 개발 환경에서 실제 GenOS LLM/MCP를 확인한다.
9. active version을 전환하고 서버를 재시작한다.
10. 시작 로그의 prompt/registry 개수를 확인한다.
11. 대표 질문, 반례, 복합 질문, agent mismatch를 확인한다.
12. 문제 발생 시 active prompt와 Python 코드를 함께 롤백한다.

## 21. 유형별 최소 수정 파일

### 고정답변

```text
subagent manifest
scenario/system Markdown
fixed_responses.py
필요 시 master agent prompt
tests
```

### 일반 MCP

```text
subagent manifest
scenario/system Markdown
mcp/scenarios/<agent>.py handler/output
mcp/scenarios/registry.py
필요 시 master agent prompt
tests
```

### RAG

```text
subagent manifest
scenario/system Markdown
agent.py document/detail/threshold mapping
검색 handler + batch RAG output
registry RAG entry
필요 시 master agent prompt
tests
```

### Action + MCP

```text
일반 MCP 수정 세트
+ agent.py ScenarioActionDefinition 등록
+ handler require/request
+ HITL 재개 테스트
```

## 22. 최종 체크리스트

```text
[ ] agent/scenario/detail/handler/output/step/action/tool 코드를 확정했다.
[ ] active prompt와 새 버전 전략을 결정했다.
[ ] parameter definitions에 실제 필요한 값만 등록했다.
[ ] detail.parameters가 선언된 값만 참조한다.
[ ] 선택 예시와 인접 detail 반례를 작성했다.
[ ] system.md에 최종 우선순위를 작성했다.
[ ] manifest.prompt_files에 새 파일을 명시했다.
[ ] master agent 경계 변경 필요성을 검토했다.
[ ] fixed 또는 registry 중 정확히 한 실행 경로를 등록했다.
[ ] MCP payload를 agent handler에서 직접 확인할 수 있다.
[ ] access token과 user context 사용 위치가 명확하다.
[ ] output handler가 실제 MCP fixture를 처리한다.
[ ] 오류·1001·빈 데이터 정책을 테스트했다.
[ ] RAG라면 문서번호·임계점수·재검색·reranking을 등록했다.
[ ] Action이면 code/message/input/order/guardrail을 등록했다.
[ ] 다중 matches에서 MCP 결과와 답변 순서가 맞다.
[ ] 추천질문 대상 detail이 실제 존재한다.
[ ] OUTPUT 가드레일과 SSE 결과를 확인했다.
[ ] Redis history/HITL 동작을 확인했다.
[ ] 전체 테스트가 통과했다.
[ ] 서버 재시작 후 prompt/registry 시작 검증을 통과했다.
```
