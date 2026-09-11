# 서브에이전트 세부 시나리오 안전 제거 가이드

> 추천질문 계약 변경: 아래의 affirmative 후속 연결 및 recommendation_id 관련
> 설명은 이전 구현 기록이다. 현재는 해당 기능을 제거했으며 클릭한 문장을
> 일반 질문으로 처리한다. [현재 추천질문 계약](09_RECOMMENDED_QUESTIONS_CUSTOMIZATION.md)을 참고한다.


## 1. 목적

이 문서는 현재 활성 서브에이전트에서 필요 없는 세부 시나리오를 제거할 때
manifest 한 줄만 삭제해서 생기는 시작 오류, 잘못된 LLM 분류, 오래된 HITL 복원
실패와 사용되지 않는 Python 코드를 함께 방지하기 위한 절차다.

현재 활성 서브에이전트는 다음 세 개다.

- `PERFORMANCE_FEE`
- `QUALIFICATION`
- `RP`

## 2. detail 하나가 연결될 수 있는 모든 위치

`detail_scenario_code`는 다음 위치에 나타날 수 있다.

```text
master agent 경계 prompt
subagent manifest scenarios[].details[]
subagent scenario Markdown
subagent system.md 우선순위/예시
required_match_rules
recommended_questions.affirmative_followup
fixed_responses.py
mcp/scenarios/registry.py
mcp/scenarios/<agent>.py handler/output/RAG 상수
scenario action registry
문서번호·필터 mapping
mock result
tests/evaluation CSV/docs
Redis history의 추천질문 metadata
Redis HITL의 대기 중 detail/action 상태
```

따라서 제거 전에 반드시 다음 명령으로 참조 목록을 만든다.

```bash
rg -n "DELETE_DETAIL_CODE" app prompts tests docs data
```

대소문자나 사람이 읽는 이름으로만 참조된 프롬프트도 있으므로 업무명도 검색한다.

```bash
rg -n "삭제할 세부 시나리오 이름|대표 질문 표현" prompts tests docs
```

## 3. 먼저 실행 방식부터 판별

제거 대상은 세 종류 중 하나다.

| 유형 | 판별 위치 | 제거할 실행 코드 |
|---|---|---|
| 고정답변 | `get_subagent_fixed_response()` 결과 존재 | `fixed_responses.py` |
| 조회형 MCP | registry에 `output_handler` 존재 | registry + agent handler/output |
| RAG | registry에 `rag_output_handler` 존재 | registry + agent 검색/RAG mapping |

Action이 연결된 MCP detail이면 네 번째로 action 정의와 등록도 제거해야 한다.

## 4. 활성 버전과 새 버전 전략

먼저 다음 파일을 확인한다.

```text
prompts/subagents/<agent>/active.yaml
```

운영에서는 활성 버전 폴더를 바로 지우기보다 다음 절차를 권장한다.

1. 현재 버전을 새 버전 폴더로 복사한다.
2. 새 `manifest.yaml.version`, `released_at`을 변경한다.
3. 새 버전에서 detail을 제거한다.
4. 테스트 후 `active.yaml`을 새 버전으로 전환한다.
5. 문제가 있으면 active version만 되돌린다.

단, Python registry와 handler는 prompt 버전과 독립적이다. 구버전 prompt로 빠르게
롤백할 가능성이 있다면 Python handler를 한 릴리스 더 유지하거나 코드와 prompt를
함께 롤백할 수 있어야 한다.

## 5. 공통 제거 순서

### 5.1 다른 detail이 참조하는 연결부터 제거

manifest의 다음 항목을 먼저 확인한다.

#### `required_match_rules`

```yaml
required_match_rules:
  - detail_codes:
      - "DELETE_DETAIL_CODE"
```

삭제한 detail을 이 배열에 남기면 서버 시작 시 loader가 알 수 없는 detail 참조로
실패한다. `skip_if_selected_detail_codes`도 함께 검색한다.

#### 추천질문 긍정 후속 연결

```yaml
recommended_questions:
  - question: "계속 진행할까요?"
    affirmative_followup:
      detail_scenario_code: "DELETE_DETAIL_CODE"
```

이 연결도 제거하거나 다른 유효 detail로 교체한다.

### 5.2 manifest에서 detail 제거

`scenarios[].details[]`에서 대상 블록 전체를 제거한다.

제거 후 상위 scenario의 `details`가 빈 배열이면 상위 scenario도 제거한다. 빈
scenario를 유지할 이유가 없고 LLM 프롬프트 설명과 schema가 어긋날 수 있다.

### 5.3 사용하지 않는 파라미터 정의 정리

대상 detail만 사용하던 파라미터가 있으면 `parameter_definitions`에서도 제거한다.
다른 detail이 같은 파라미터를 쓰는지 먼저 검색한다.

```bash
rg -n '"parameter_name"|parameter_name:' \
  prompts/subagents/<agent>/<version>
```

사용되지 않는 parameter definition은 loader가 즉시 실패시키지는 않더라도 flat
schema와 운영 문서를 불필요하게 복잡하게 만든다.

### 5.4 서브 프롬프트 정리

다음 두 곳을 모두 수정한다.

- `scenarios/*.md`: detail 절, 선택 예시, 제외 예시, 파라미터 규칙
- `system.md`: 최종 우선순위, 강제 선택 규칙, 복합 질문 규칙

중요: `SubagentPromptLoader`는 `manifest.prompt_files`에 선언하지 않은 Markdown도
찾아서 뒤에 자동 결합한다. 따라서 `prompt_files`에서만 빼고 파일을 남겨두면
삭제한 규칙이 계속 LLM에 전달된다.

완전히 제거하려면 다음 중 하나를 적용한다.

- Markdown 파일에서 해당 detail 절을 삭제
- 파일 전체가 대상 detail 전용이면 실제 파일 삭제
- 보관용 파일은 활성 version 폴더 밖으로 이동

확인 코드는 다음과 같다.

```python
bundle = SubagentPromptLoader().load_one(directory="rp")
print(bundle.files)
print("DELETE_DETAIL_CODE" in bundle.system_prompt)
```

## 6. 고정답변 detail 제거

`app/subagents/fixed_responses.py`에서 다음 키를 제거한다.

```python
(
    "AGENT_CODE",
    "DELETE_DETAIL_CODE",
): SubagentFixedResponse(...),
```

manifest detail을 먼저 제거하고 fixed response만 남기면 서버는 뜰 수 있지만 죽은
설정이 남는다. 반대로 manifest는 남기고 fixed response부터 제거하면 registry에
MCP handler가 없는 한 서버 시작이 실패한다. 같은 변경 단위에서 둘 다 제거한다.

고정답변 detail 제거에서는 일반적으로 다음을 수정하지 않는다.

- `app/mcp/scenarios/registry.py`
- `app/mcp/scenarios/<agent>.py`
- `app/graph.py`
- `app/answers.py`

단, 해당 detail을 다른 코드에서 직접 비교했다면 그 참조는 별도로 제거한다.

## 7. 조회형 MCP detail 제거

### 7.1 registry entry 제거

`app/mcp/scenarios/registry.py`에서 정확한 키를 제거한다.

```python
(
    "AGENT_CODE",
    "DELETE_DETAIL_CODE",
): _spec(
    "agent.delete_detail.v1",
    agent_module.delete_detail,
    output_handler=agent_module.delete_detail_output,
    output_handler_code="agent.delete_detail_output.v1",
),
```

manifest만 제거하고 registry를 남기면 즉시 장애가 나지는 않지만 호출되지 않는 죽은
코드가 된다. registry를 먼저 제거하고 manifest를 남기면 prompt loader 시작 검증이
실패한다.

### 7.2 agent Python 함수 제거

`app/mcp/scenarios/<agent>.py`에서 다음 항목을 찾는다.

- `async def <detail_handler>()`
- `def <detail_output>()`
- detail 전용 컬럼 목록
- tool name과 step code 상수
- 결과 파싱 helper
- NO_DATA/ERROR 전용 문구
- 해당 함수만 사용하는 import

공통 helper는 다른 handler 사용 여부를 `rg`로 확인한 뒤 제거한다.

### 7.3 mock 결과 제거

전용 mock 데이터가 `app/mcp/client.py` 또는 테스트 fixture에 있다면 함께 정리한다.
공용 `test_tool` mock을 여러 detail이 사용하면 삭제하지 않는다.

## 8. RAG detail 제거

RAG detail은 manifest/registry 외에 문서 mapping과 집합 상수를 반드시 확인한다.

### 8.1 RP

현재 `app/mcp/scenarios/rp.py`에서 대표적으로 확인할 값은 다음과 같다.

- `RP_RAG_DETAIL_CODES`
- `RP_DOCUMENT_NUMBER_BY_DETAIL_CODE`
- detail별 검색 임계값 mapping
- detail별 reranking 임계값 mapping
- detail별 문서 없음 고정답변 정책
- 통합 RAG output 내부의 유효 detail 검증

`tests/test_current_configuration.py`는 RP 문서 detail 개수와 문서번호 mapping의
집합 일치를 검사한다. detail 하나를 제거하면 manifest, 두 상수/mapping, 기대 개수를
같이 바꾼다.

### 8.2 QUALIFICATION

`app/mcp/scenarios/qualification.py`에서 다음을 확인한다.

- detail -> 문서번호 mapping
- RAG detail 집합
- 검색·재검색용 arguments 분기
- 문서 전처리와 detail metadata
- 통합 answerability/답변 입력 구성

여러 detail이 동일한 batch RAG output handler를 공유하더라도 registry의 대상
detail entry는 각각 존재한다. 삭제한 detail entry만 제거하고 공용 batch 함수는
다른 detail이 사용하면 유지한다.

### 8.3 문서 재검색과 reranking

Graph의 `_apply_rag_batch_second_search`는 registry에서 RAG output handler가 있는
match만 묶는다. manifest와 registry에서 detail을 제거하면 Graph 공통 코드는
수정하지 않는다. agent 파일의 detail별 임계값과 mapping만 정리한다.

## 9. Action/HITL이 있는 detail 제거

`app/mcp/scenarios/<agent>.py`에서 다음 구조를 제거한다.

```python
DETAIL_ACTION = register_scenario_action(
    ScenarioActionDefinition(
        agent_code="AGENT_CODE",
        detail_scenario_code="DELETE_DETAIL_CODE",
        action_code="ACTION_CODE",
        ...
    )
)
```

그리고 handler의 다음 호출도 함께 제거된다.

```python
values = DETAIL_ACTION.require(context.parameters)
```

`register_scenario_action()`은 모듈 import 시 전역 registry에 등록한다. handler만
지우고 action 등록을 남기면 사용되지 않는 guardrail 정책과 action key가 남는다.

### 배포 중 대기 HITL 처리

Redis HITL 상태에는 다음이 남아 있을 수 있다.

- agent code
- detail scenario code
- action code
- 추출 parameters
- 완료된 MCP 결과와 재개 index

새 코드에서 action registry를 제거한 뒤 사용자가 옛 `thread_id`로 재개하면 action을
복원하지 못할 수 있다. 배포 전 다음을 결정한다.

1. HITL TTL 만료 후 배포
2. 관련 endpoint의 대상 HITL만 삭제
3. 한 릴리스 동안 이전 action 정의를 호환용으로 유지

전체 Redis 삭제는 다른 agent/session에 영향을 주므로 피한다.

## 10. 마스터 경계 정리

세부 시나리오 제거로 agent 자체의 업무 범위가 줄어들면
`prompts/intent-classification/v1/agents/<agent>.md`의 선택 기준에서도 제거한다.

그렇지 않으면 master는 여전히 해당 agent를 선택하지만 subagent는 남은 detail 중
엉뚱한 하나를 강제로 선택해야 한다. 현재 서브 구조화 출력은 최소 한 개 match를
요구하며, `ScenarioSubagent.classify()`도 빈 matches를 오류로 처리한다.

업무가 완전히 지원되지 않게 된다면 master의 다음 내용을 함께 검토한다.

- 대상 agent 선택 기준에서 삭제
- 다른 agent로 이전했다면 새 담당 agent 규칙에 추가
- 서비스 전체 미지원이면 `OUT_OF_SCOPE` 경계에 포함할지 판단
- 비슷한 키워드의 반례를 각 agent prompt에 추가

## 11. 추천질문과 오래된 Redis 이력

현재 추천질문은 assistant history metadata에 저장된다. 삭제한 detail을 가리키는
확인형 추천질문이 이전 세션에 남아 있을 수 있다.

새 요청에서 오래된 `recommendation_id`가 선택되면 다음이 일어날 수 있다.

1. history metadata에서 예전 affirmative followup을 찾는다.
2. Graph가 삭제된 detail로 `classify_for_detail()`을 시도한다.
3. 현재 manifest에서 detail을 찾지 못하면 일반 서브 LLM 분류로 fallback한다.

안전한 운영 방법은 다음과 같다.

- history TTL을 고려해 전환 시간 선택
- 삭제 전 추천질문 노출을 먼저 중단하는 2단계 배포
- 중요한 명령형 detail이면 이전 ID를 명시적으로 거부하는 호환 정책 추가

## 12. 테스트 정리

다음 테스트를 검색하고 기대값을 갱신한다.

```text
tests/test_current_configuration.py
tests/test_mcp_function_handlers.py
tests/test_scenario_actions.py
tests/test_fixed_responses.py
tests/test_mcp_response_compatibility.py
평가 CSV 및 fixture JSON
```

특히 확인할 항목은 다음과 같다.

- detail enum 개수
- manifest detail 집합
- RAG 문서 mapping 집합
- handler registry key
- action registry key
- 추천질문 대상 detail
- 다중 match 기대 순서
- MCP 결과 기대 개수

## 13. 제거 후 검증

```bash
python -m compileall -q app tests
python -m pytest -q -p no:cacheprovider
```

잔존 참조 검사는 반드시 0건이 되어야 하는 참조와 남아도 되는 이력·문서를
구분하여 판단한다.

```bash
rg -n "DELETE_DETAIL_CODE" app prompts tests docs
```

서버 시작 시 다음 로그를 확인한다.

```text
서브에이전트 프롬프트 결합 완료
서브에이전트 registry 로딩 완료
LangGraph 컴파일 완료
```

제거된 업무 대표 질문도 테스트한다. 중요한 것은 “삭제한 detail이 안 나오는가”뿐
아니라 “대신 어떤 결과가 나오는가”다.

- 다른 유효 detail로 가야 하는가
- 다른 agent로 전환해야 하는가
- `OUT_OF_SCOPE`가 되어야 하는가
- 안전한 고정답변이 필요하지만 누락된 것은 아닌가

## 14. 유형별 최소 제거 세트

### 고정답변

```text
manifest detail
scenario/system prompt
fixed_responses entry
required_match/recommendation 참조
tests/docs
```

### 일반 MCP

```text
manifest detail
scenario/system prompt
registry entry
agent handler/output
action/mock/helper 참조
tests/docs
```

### RAG

```text
manifest detail
scenario/system prompt
registry entry
document/detail mapping
threshold/RAG 집합
agent 검색·전처리 분기
tests/docs
```

## 15. 최종 체크리스트

```text
[ ] active prompt 버전을 확인했다.
[ ] detail 코드와 업무명으로 전체 rg를 수행했다.
[ ] required_match_rules 참조를 제거했다.
[ ] affirmative_followup 대상 참조를 제거했다.
[ ] manifest detail을 제거했다.
[ ] 빈 상위 scenario를 제거했다.
[ ] 다른 detail이 사용하지 않는 parameter definition을 제거했다.
[ ] prompt_files만이 아니라 실제 Markdown 내용도 제거했다.
[ ] system.md의 우선순위·예시를 제거했다.
[ ] 고정답변 entry 또는 MCP registry entry를 제거했다.
[ ] 전용 handler/output/helper/import를 제거했다.
[ ] RAG 문서번호·임계값·detail 집합을 정리했다.
[ ] action 등록과 guardrail policy 등록을 정리했다.
[ ] 대기 중 Redis HITL 호환 정책을 결정했다.
[ ] 이전 추천질문 history 호환 정책을 결정했다.
[ ] master agent 업무 경계를 줄였다.
[ ] 테스트의 enum 개수·mapping 집합·기대 결과를 변경했다.
[ ] 삭제한 질문이 앞으로 어떻게 처리되는지 E2E로 확인했다.
[ ] 전체 테스트와 서버 시작 검증을 통과했다.
```

