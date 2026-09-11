# 프롬프트·에이전트·세부 시나리오 커스터마이징

## 현재 활성 에이전트

| code | 역할 | subagent 구현 |
|---|---|---:|
| `PERFORMANCE_FEE` | 실적·수수료·원천징수 조회 | 있음 |
| `RP` | RP 문서·아파트·공통 복합환산 | 있음 |
| `QUALIFICATION` | 입회 자격·증빙 문서 | 있음 |

마스터와 실행 registry가 다른 agent 집합을 가지지 않도록 유지한다. 현재 실행이
없던 `FEE_POLICY`, `PRODUCT_GUIDE`, `TABLET`은 제거됐다.

## 프롬프트 버전 구조

```text
prompts/intent-classification/
  active.yaml
  v1/
    manifest.yaml
    router/system.md
    router/agents.md
    agents/*.md

prompts/subagents/
  registry.yaml
  <agent-directory>/
    active.yaml
    <version>/manifest.yaml
    <version>/system.md
    <version>/scenarios/*.md
```

운영 버전을 바꿀 때 기존 version 폴더를 직접 덮기보다 새 폴더를 만들고
`active.yaml`을 전환한다.

## master agent 경계 수정

1. `prompts/intent-classification/v1/agents/<agent>.md`에서 선택/제외 조건 수정
2. `router/system.md`에서 예외 우선순위와 공통 경계 수정
3. `manifest.yaml.agent_code`가 실제 파일과 subagent registry에 맞는지 확인
4. classifier structured output 허용 code가 prompt bundle에서 생성되는지 테스트

프론트 `agent_code`는 강제가 아니라 힌트다. 실제 분류와 다르면 graph가
`AGENT_CODE_MISMATCH` action으로 확인을 요청한다.

## detail 분류 수정

각 agent manifest의 detail은 다음만 관리한다.

```yaml
parameter_definitions:
  search_query:
    description: "검색 의미를 보존한 자연어 검색문"

scenarios:
  - code: "DOCUMENTS"
    name: "업무 문서"
    details:
      - code: "DOCUMENT_SEARCH"
        name: "문서 조회"
        description: "어떤 질문을 선택하는지"
        parameters:
          - "search_query"
        recommended_questions: []
```

YAML에 action, tool, arguments, next key, output columns를 새로 넣지 않는다.

- detail 구분과 parameter 추출 품질: scenario `.md`, `system.md`
- 정규식/허용값 metadata: manifest `parameter_definitions`
- 실제 action 입력 검증: Python `ScenarioActionInput`
- MCP와 output: Python scenario handler/output

LLM parameter 추출은 한 subagent 호출에서 선택된 모든 match에 대해 수행한다.
정확도를 높이려면 각 detail의 `parameters` 목록을 최소화하고 scenario 문서에
“언제 값이 null인지”와 형식을 명시한다. action으로 확정해야 할 업무 입력은 LLM
추출값을 신뢰하지 말고 `ACTION.require()`로 다시 검증한다.

## 신규 detail 추가 전체 절차

1. agent manifest에 scenario/detail/parameters/recommended questions 추가
2. 해당 scenario `.md`에 선택·제외 기준과 parameter 추출 규칙 추가
3. 조회 또는 명령이면 `app/mcp/scenarios/<agent>.py`에 async handler 작성
4. 입력이 필요하면 같은 파일에 `ScenarioActionDefinition` 등록
5. 성공 결과 전처리가 필요하면 같은 파일에 `*_output()` 작성
6. `app/mcp/scenarios/registry.py`에 handler와 output 연결
7. RAG detail이면 같은 agent 파일에 문서 전처리·품질 정책·전용 RAG output 작성
8. MCP 없는 고정 답변이면 `app/subagents/fixed_responses.py` 등록
9. 추천질문은 클릭 후 그대로 전송할 완전한 사용자 요청 문장으로 작성
10. 분류, handler, action, output, SSE 회귀 테스트 추가

## 신규 agent 추가 전체 절차

1. master agent `.md` 생성
2. intent manifest `agent_code` 추가
3. `prompts/subagents/registry.yaml`에 directory 등록
4. subagent `active.yaml`, version manifest, system/scenario prompt 생성
5. 최소 한 detail의 실행 방식을 구현
   - Python MCP handler/output, 또는
   - agent 전용 RAG handler/output, 또는
   - fixed response
6. `prompts/answer-generation/v1/manifest.yaml`의 기본 답변 모드 검토
7. master→subagent→answer E2E 테스트 추가

master 분류만 추가하고 실행 구현을 빼면 안 된다. 미구현 agent가 선택되어
“조회 결과 없음”으로 끝나는 것을 방지하기 위해 배포 전 registry 대조 테스트를
추가한다.

## 추천질문

- 없음: 키 생략 또는 `recommended_questions: []`
- 문자열: 일반 후속 질문
- 클릭한 추천질문도 일반 보정·분류를 거치며 특정 detail을 강제 선택하지 않는다.

자연어 `네`는 직전 assistant metadata의 실행 추천이 정확히 하나일 때만 자동
연결한다. 여러 실행 후보면 버튼을 다시 보여 주고 선택을 요구한다. 상세는
[09번 문서](09_RECOMMENDED_QUESTIONS_CUSTOMIZATION.md)를 따른다.

## 현재 파일별 책임

| 변경 | 위치 |
|---|---|
| master 선택/예외 | `prompts/intent-classification` |
| detail 선택/parameter 추출 | `prompts/subagents/<agent>` |
| 필수 match 보정 | subagent manifest `required_match_rules` |
| fixed response | `app/subagents/fixed_responses.py` |
| MCP/action/일반 output | `app/mcp/scenarios/<agent>.py` |
| RAG 검색·정책·전처리·최종 output | `app/mcp/scenarios/<agent>.py` |
| registry 검증 | `app/subagents/prompt_loader.py`, `app/mcp/scenarios/registry.py` |
