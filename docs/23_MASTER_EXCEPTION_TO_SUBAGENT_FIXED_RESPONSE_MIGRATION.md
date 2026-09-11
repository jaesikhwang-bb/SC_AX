# 마스터 EXCEPTION을 서브에이전트 고정답변으로 이전하는 완전 가이드

> 추천질문 계약 변경: 아래의 affirmative 후속 연결 및 recommendation_id 관련
> 설명은 이전 구현 기록이다. 현재는 해당 기능을 제거했으며 클릭한 문장을
> 일반 질문으로 처리한다. [현재 추천질문 계약](09_RECOMMENDED_QUESTIONS_CUSTOMIZATION.md)을 참고한다.


## 1. 문서 목적과 적용 범위

이 문서는 2026-09-08 현재 SC_AX 코드에서 마스터 에이전트의 일부 예외를
제거하고, 해당 업무를 특정 서브에이전트의 세부 시나리오로 분류한 뒤 MCP 없이
고정답변을 반환하도록 변경하는 절차를 설명한다.

이 문서가 다루는 변경은 다음과 같다.

```text
변경 전
사용자 질문
  -> 마스터 EXCEPTION
  -> agent_code=null
  -> 서브에이전트/MCP 생략
  -> prompts/answer-generation의 exception_answers

변경 후
사용자 질문
  -> 마스터 AGENT + 대상 agent_code
  -> 해당 서브에이전트의 고정답변 detail
  -> MCP 생략
  -> app/subagents/fixed_responses.py의 문구
```

2026-09-09 현재 `OTHER_RECRUITER_DATA_REQUEST`와 고객 요청 목적별
`CUSTOMER_DISPOSAL_REASON_REQUEST`, `CUSTOMER_SALES_PERFORMANCE_REQUEST`,
`CUSTOMER_PERFORMANCE_DETAIL_REQUEST`는 `PERFORMANCE_FEE`의
`ACCESS_RESTRICTION` 고정답변 detail로 이전되었고,
`PROVISIONAL_DISPOSITION_INQUIRY`는 `QUALIFICATION`의
`PROVISIONAL_DISPOSITION_GUIDANCE` 고정답변 detail로 이전되었다. 다른 예외를
옮기거나 다른 agent에 같은 구조를 적용할 때도 아래 절차를 사용한다.

## 2. 현재 마스터 분류 유형

현재 최상위 분류 유형은 `app/domain.py`의 `ClassificationType`에 있다.

| 코드 | 성격 | 현재 최종 노출 여부 | 현재 처리 |
|---|---|---:|---|
| `AGENT` | 정상 업무 | 예 | 서브에이전트로 이동 |
| `CONTEXT_REQUIRED` | 내부 문맥 재분류 신호 | 아니오 | 같은 agent 범위 Redis 이력으로 2차 분류 |
| `EMPTY_QUERY` | 질문 불완전 | 예 | 마스터 예외 고정답변 |
| `OUT_OF_SCOPE` | 전체 업무 범위 밖 | 예 | 마스터 예외 고정답변 |

다음 코드는 마스터 분류 유형에서 제거되어 현재 `PERFORMANCE_FEE` 세부 시나리오로
처리된다.

| 코드 | 현재 agent | 현재 처리 |
|---|---|---|
| `OTHER_RECRUITER_DATA_REQUEST` | `PERFORMANCE_FEE` | 서브 고정답변, MCP 생략 |
| `CUSTOMER_DISPOSAL_REASON_REQUEST` | `PERFORMANCE_FEE` | 고객 폐기 여부·사유 고정답변, MCP 생략 |
| `CUSTOMER_SALES_PERFORMANCE_REQUEST` | `PERFORMANCE_FEE` | 고객 매출실적 고정답변, MCP 생략 |
| `CUSTOMER_PERFORMANCE_DETAIL_REQUEST` | `PERFORMANCE_FEE` | 기타 고객 실적 상세 고정답변, MCP 생략 |
| `PROVISIONAL_DISPOSITION_GUIDANCE` | `QUALIFICATION` | 서브 고정답변, MCP·문서 검색 생략 |

`CONTEXT_REQUIRED`는 삭제 대상인 일반 예외가 아니다. 현재 질문만으로 완결되지
않았음을 표시하는 LangGraph 내부 라우팅 코드이며, 이력이 없거나 복원에 실패하면
`EMPTY_QUERY`로 정규화된다.

## 3. 어떤 예외를 옮기는 것이 적합한가

### 3.1 마스터에 유지하는 것이 일반적으로 좋은 유형

다음 유형은 특정 agent가 아니라 전체 서비스 입구에서 판단해야 하므로 마스터에
유지하는 편이 안전하다.

- `EMPTY_QUERY`: 질문 자체가 없어 대상 agent를 정할 수 없다.
- `OUT_OF_SCOPE`: 어떤 agent에도 속하지 않는 질문이다.
- `CONTEXT_REQUIRED`: 최종 예외가 아니라 2단계 문맥 분류 제어 코드다.
- 모든 agent에 동일하게 적용되는 보안·개인정보 차단 규칙.

특히 다른 모집인 데이터와 고객 개인정보 차단은 정책상 전역 가드에 가깝다. 이를
서브 detail로 옮기면 마스터가 먼저 agent를 결정해야 하고, 프론트 선택 agent와
다를 때 `SWITCH_AGENT` 확인까지 거친 뒤 차단 답변이 나갈 수 있다. 보안 정책상
즉시 차단이 필요하다면 마스터에 남긴다.

### 3.2 서브에이전트로 옮기기 좋은 유형

다음 특성이 있으면 서브 고정답변 detail이 더 자연스럽다.

- 질문이 명확히 특정 agent의 업무 범위다.
- 답변 문구가 agent 또는 세부 업무에 따라 달라진다.
- 동일한 표현이라도 유치조직구분코드 `11/12/13`별 안내가 다르다.
- 향후 고정답변에서 MCP 조회나 RAG로 발전할 수 있다.
- 마스터에서 전역 예외로 막기보다 정상 업무 이력으로 저장하고 싶다.

현재 코드에서 가장 단순한 이전 예시는
`PROVISIONAL_DISPOSITION_INQUIRY -> QUALIFICATION` 고정답변 detail이다. 다만 실제
업무 소유자가 자격기준 agent가 맞는지는 업무 담당자가 먼저 확정해야 한다.

### 3.3 이전 전에 반드시 결정할 네 가지

1. 대상 서브에이전트 코드
2. 새 `scenario_code`와 `detail_scenario_code`
3. 최종 상태를 `PASS`로 바꿔도 되는지
4. 해당 질문과 답변을 Redis 대화 이력에 저장해도 되는지

3번과 4번은 단순 프롬프트 변경이 아니라 외부 동작 차이다.

## 4. 이전하면 달라지는 외부 동작

| 항목 | 마스터 EXCEPTION | 서브 고정답변 detail |
|---|---|---|
| `classification_type` | 예외 코드 | `AGENT` |
| `agent_code` | `null` | 대상 agent 코드 |
| 그래프 상태 | `EXCEPTION` | `PASS` |
| 프론트 agent 불일치 | 비교하지 않음 | `SWITCH_AGENT` 가능 |
| 서브 LLM | 호출 안 함 | 호출함 |
| MCP | 호출 안 함 | 호출 안 함 |
| 고정답변 위치 | answer manifest | `fixed_responses.py` |
| user 이력 저장 | 안 함 | 현재 구조에서는 저장함 |
| assistant 이력 저장 | 안 함 | 현재 구조에서는 저장함 |
| 추천질문 | 보통 없음 | detail manifest에서 가능 |
| 모집인 구분별 문구 | 별도 구현 필요 | 기본 지원 |

현재 Graph는 `persist_user_message -> run_subagent` 순서다. 즉 서브 detail이 무엇인지
알기 전에 정상 `AGENT` 질문을 Redis에 저장한다. 또한 API는 최종 응답 상태가
`PASS`이고 agent가 있으면 assistant 답변도 저장한다. 따라서 고정답변 detail로
옮기면서 종전의 “예외는 이력에 저장하지 않음” 동작을 자동으로 유지할 수 없다.

이력을 저장하면 안 되는 정책이라면 다음 별도 설계가 필요하다.

- `SubagentFixedResponse`에 `persist_history: bool` 같은 정책 필드 추가
- user 저장을 서브 분류 이후 노드로 지연
- state와 `MasterResult`에 최종 저장 허용 여부 전달
- `app/api.py`의 assistant 저장 조건에도 동일 플래그 적용

이 변경은 단순 예외 이전과 분리하여 설계·테스트하는 것을 권장한다.

## 5. 전체 수정 지점 지도

| 순서 | 목적 | 파일 | 필수 여부 |
|---:|---|---|---:|
| 1 | 대상 agent 업무 범위 추가 | `prompts/intent-classification/v1/agents/<agent>.md` | 필수 |
| 2 | 마스터 예외 규칙 제거 | `prompts/intent-classification/v1/router/system.md` | 필수 |
| 3 | 새 고정답변 detail 정의 | `prompts/subagents/<agent>/<version>/manifest.yaml` | 필수 |
| 4 | detail 선택·제외 예시 | 해당 `scenarios/*.md` | 필수 권장 |
| 5 | 최종 서브 분류 우선순위 | 해당 `system.md` | 필수 권장 |
| 6 | 고정답변 등록 | `app/subagents/fixed_responses.py` | 필수 |
| 7 | 마스터 enum 정리 | `app/domain.py` | 최종 정리 시 필수 |
| 8 | 사용하지 않는 예외 문구 제거 | `prompts/answer-generation/v1/manifest.yaml` | 권장 |
| 9 | 테스트 기대값 변경 | `tests/` 및 평가 CSV | 필수 |
| 10 | 문서·운영 명세 갱신 | `docs/` | 필수 권장 |

일반적인 고정답변 이전에서는 다음 파일을 수정하지 않는다.

- `app/graph.py`: 고정답변 detail을 자동으로 MCP 생략 경로에 태운다.
- `app/answers.py`: 등록된 고정답변을 match 순서대로 자동 조합한다.
- `app/api.py`: 최종 답변 가드레일과 SSE를 그대로 수행한다.
- `app/mcp/scenarios/registry.py`: 고정답변 detail은 MCP handler를 등록하지 않는다.

## 6. 예시: 가처분 문의를 QUALIFICATION 고정답변으로 이전

이하 예시는 다음 코드를 사용한다.

```text
agent_code           = QUALIFICATION
scenario_code        = QUALIFICATION_FIXED_GUIDANCE
detail_scenario_code = PROVISIONAL_DISPOSITION_GUIDANCE
```

### 6.1 먼저 서브 manifest에 detail 추가

`prompts/subagents/qualification/v1/manifest.yaml`에 다음 상위 시나리오와 detail을
추가한다. 파라미터와 MCP가 없으므로 `parameters: []`다.

```yaml
scenarios:
  # 기존 시나리오들...

  - code: "QUALIFICATION_FIXED_GUIDANCE"
    name: "자격기준 관련 고정 안내"
    description: "문서 또는 데이터 조회 없이 담당 경로를 안내하는 문의"
    details:
      - code: "PROVISIONAL_DISPOSITION_GUIDANCE"
        name: "가처분·압류 문의 안내"
        description: >
          가처분, 압류 또는 이에 준하는 법적 제한 조치 문의에 정해진 안내문을 반환
        parameters: []
        recommended_questions: []
```

중요한 시작 검증 규칙은 다음과 같다.

```text
manifest에 존재하는 모든 detail
  -> fixed_responses에 등록되어 있거나
  -> app/mcp/scenarios/registry.py에 handler가 있어야 함
```

따라서 manifest만 먼저 추가한 상태로 서버를 시작하면
“고정답변이 아닌 detail에 Python MCP handler가 없습니다” 오류가 발생한다.
manifest와 고정답변 등록은 같은 배포에 포함한다.

### 6.2 서브 시나리오 프롬프트 추가

예를 들어 다음 파일을 만든다.

```text
prompts/subagents/qualification/v1/scenarios/03_fixed_guidance.md
```

```markdown
# 자격기준 관련 고정 안내

## PROVISIONAL_DISPOSITION_GUIDANCE

다음 질문은 `PROVISIONAL_DISPOSITION_GUIDANCE`만 선택한다.

- 가처분 문의는 어디에서 확인해?
- 가처분이 설정된 고객은 어떻게 처리해?
- 압류 관련 업무 문의를 하고 싶어

이 detail은 문서를 검색하거나 개인정보를 조회하지 않는다.
파라미터는 빈 객체로 반환한다.

다음 질문은 선택하지 않는다.

- 외국인 회원의 입회 조건을 알려줘
  - `FOREIGNER_QUALIFICATION`
- 가족카드 발급 기준을 알려줘
  - `FAMILY_CARD_ISSUANCE_QUALIFICATION`
```

`manifest.yaml.prompt_files`에 새 파일을 명시한다. 현재 loader는 선언되지 않은
Markdown도 자동으로 결합하지만, 순서와 변경 추적을 위해 명시하는 편이 안전하다.

### 6.3 qualification system prompt 경계 수정

`prompts/subagents/qualification/v1/system.md`에 다음 규칙을 추가한다.

```markdown
- 가처분·압류 등 법적 제한 조치 자체의 문의는
  `PROVISIONAL_DISPOSITION_GUIDANCE`를 선택한다.
- 이 detail은 다른 자격기준 문서 detail과 함께 선택하지 않는다.
- 실제 개인정보나 고객 상세 데이터를 추출하지 않는다.
```

“함께 선택하지 않는다”는 정책이 실제 업무 요구와 다르면 제거할 수 있다. 현재
서브 출력은 복수 `matches`를 허용하므로 명시하지 않으면 복합 질문에서 RAG detail과
고정답변 detail이 동시에 나올 수 있다.

### 6.4 고정답변 등록

`app/subagents/fixed_responses.py`의 `SUBAGENT_FIXED_RESPONSES`에 등록한다.

```python
(
    "QUALIFICATION",
    "PROVISIONAL_DISPOSITION_GUIDANCE",
): SubagentFixedResponse(
    default_message=(
        "가처분 관련 문의는 관련 업무 담당 채널을 통해 확인해 주세요."
    ),
    messages_by_recruitment_org_type={
        "11": "일반 모집인용 가처분 문의 안내문",
        "12": "제휴 모집인용 가처분 문의 안내문",
        "13": "복합 모집인용 가처분 문의 안내문",
    },
),
```

유치조직구분코드가 없거나 `11/12/13` 외 값이면 `default_message`가 사용된다.
분기 문구가 모두 같다면 `messages_by_recruitment_org_type`을 생략할 수 있다.

### 6.5 마스터가 예외가 아니라 QUALIFICATION을 선택하도록 변경

`prompts/intent-classification/v1/router/system.md`에서 다음 항목을 모두 찾는다.

- `### [PROVISIONAL_DISPOSITION_INQUIRY]` 전체 절
- 예외 우선순위의 `PROVISIONAL_DISPOSITION_INQUIRY`
- 출력 JSON의 classification type 목록
- `classification_type` 필드 규칙
- 예외이면 `agent_code=null`이라는 설명 중 해당 코드

그다음 `prompts/intent-classification/v1/agents/qualification.md`에서 다음을
변경한다.

- “가처분은 master exception이므로 선택하지 않는다” 규칙 제거
- 가처분·압류 문의를 `QUALIFICATION` 선택 기준에 추가
- 개인정보 조회와 일반 고정 안내의 경계를 명시

마스터 프롬프트의 여러 파일은 모두 결합된다. 한 파일에서 예외를 제거했어도 다른
agent 설명에 “예외로 종료한다”가 남아 있으면 LLM 판단이 계속 충돌한다. 반드시
다음 명령으로 전체 잔존 참조를 확인한다.

```bash
rg -n "PROVISIONAL_DISPOSITION_INQUIRY|가처분|압류" \
  prompts/intent-classification prompts/subagents app tests docs
```

### 6.6 domain enum 정리

모든 활성 master prompt에서 해당 코드를 제거한 다음 `app/domain.py`에서
`ClassificationType.PROVISIONAL_DISPOSITION_INQUIRY`를 제거한다.

동적 Structured Output은 이 Enum을 사용하므로 별도의 JSON Schema 파일을 수정할
필요는 없다. 다만 enum부터 먼저 제거하고 프롬프트에 코드가 남아 있으면 LLM 지시와
Pydantic schema가 불일치한다. 항상 프롬프트와 코드를 같은 배포 단위로 수정한다.

### 6.7 사용하지 않는 master 고정답변 제거

`prompts/answer-generation/v1/manifest.yaml`의 다음 항목을 제거한다.

```yaml
exception_answers:
  PROVISIONAL_DISPOSITION_INQUIRY: "기존 문구"
```

남겨도 즉시 오류가 나지는 않지만 더 이상 도달하지 않는 설정이 되므로 제거하는
편이 운영자가 답변 위치를 혼동하지 않는다.

### 6.8 마스터 manifest에서 수정하지 않는 것

`prompts/intent-classification/v1/manifest.yaml.agent_code`에는 이미
`qualification`이 있으므로 바꾸지 않는다. 예외 하나를 detail로 옮긴다고 agent
코드를 추가하거나 제거하지 않는다.

## 7. 런타임에서 실제로 처리되는 과정

변경 후 요청 흐름은 다음과 같다.

```text
1. classifier.py
   classification_type=AGENT, agent_code=QUALIFICATION

2. graph.py::_verify_selection
   프론트 선택 agent와 비교

3. graph.py::_persist_user_message
   보정 질문을 QUALIFICATION 이력으로 저장

4. graph.py::_run_subagent
   PROVISIONAL_DISPOSITION_GUIDANCE 선택

5. graph.py::_after_subagent
   모든 match가 fixed response인지 검사
   전부 fixed이면 call_mcp를 건너뜀

6. answers.py::DefaultAnswerService.prepare
   fixed_responses에서 조직구분별 문구 선택

7. api.py
   OUTPUT 가드레일 -> SSE token/messages -> assistant 이력 저장
```

고정답변 detail과 MCP detail이 한 질문에서 동시에 선택되면 Graph는 MCP가 필요한
match만 호출한다. AnswerService는 원래 match 순서에 따라 고정답변과 MCP 답변을
조합한다. 고정답변 match는 `mcp_results` 커서를 소비하지 않는다.

## 8. 개인정보·정책 예외를 옮길 때의 추가 위험

`OTHER_RECRUITER_DATA_REQUEST` 또는 고객정보 조회 제한 코드를 서브 detail로
옮기려면 단순히 하나의 agent에 detail 하나를 추가해서는 부족할 수 있다.

예를 들어 다른 모집인의 데이터 요청은 다음 업무에 모두 나타날 수 있다.

- 실적·수수료
- RP 복합환산
- 자격기준 문서와 결합된 고객 질문

선택지는 세 가지다.

### 선택 A: 마스터 전역 예외 유지

가장 안전하고 중복이 없다. 개인정보 정책이면 이 방식을 우선 권장한다.

### 선택 B: agent마다 별도 고정답변 detail 생성

각 agent가 자기 문맥에서 정확한 답변을 할 수 있지만 manifest·prompt·테스트가
반복된다. 마스터는 먼저 실제 업무 agent를 선택해야 한다.

### 선택 C: 정책 전용 agent 생성

정책 문의가 충분히 많고 독립적인 업무라면 가능하지만, master agent 목록,
subagent registry, 새 prompt 폴더와 실행 방식을 모두 추가해야 한다. 단순 고정답변
하나를 위해서는 과한 구조일 수 있다.

## 9. 상태·이력·HITL 호환성

### 9.1 프론트 agent 불일치

master exception은 agent 비교 전에 종료되지만, AGENT 분류는 `_verify_selection`을
통과한다. 사용자가 RP 화면에서 가처분 질문을 했고 master가 QUALIFICATION으로
분류하면 먼저 `SWITCH_AGENT` action이 나간다.

이 동작이 싫다면 다음 중 하나를 정책으로 정해야 한다.

- 프론트가 agent를 선택하지 않은 상태에서 질문하게 함
- 두 agent의 공통 업무 유지 규칙을 Graph에 명시적으로 추가
- 전역 master exception을 유지

고정답변을 빨리 보여주기 위해 공통업무 보존 함수를 무분별하게 넓히면 agent별
이력이 섞일 수 있으므로 권장하지 않는다.

### 9.2 배포 전에 남아 있는 HITL

예외 이전 자체는 기존 HITL detail을 제거하지 않으므로 영향이 적다. 하지만 같은
배포에서 detail 코드나 action 코드를 제거하면 Redis에 대기 중인 상태가 새 코드로
복원되지 않을 수 있다. 다음 중 하나를 선택한다.

- HITL TTL이 끝난 뒤 배포
- 프로젝트·환경 범위를 확인한 뒤 관련 HITL key만 정리
- 한 릴리스 동안 이전 action/detail compatibility 유지

### 9.3 이전 assistant 추천질문

이전 prompt 버전의 추천질문 metadata가 Redis history에 남아 있을 수 있다. 삭제한
detail을 가리키는 `affirmativeFollowup`은 새 registry에서 직접 찾지 못해 일반 서브
분류로 fallback할 수 있다. 중요한 전환이면 세션 만료 정책 또는 호환 detail을
검토한다.

## 10. 테스트 작성 지침

### 10.1 manifest 시작 검증

```python
bundle = SubagentPromptLoader().load_one(directory="qualification")
details = {
    detail["code"]
    for scenario in bundle.manifest["scenarios"]
    for detail in scenario["details"]
}
assert "PROVISIONAL_DISPOSITION_GUIDANCE" in details
```

이 테스트를 실행하는 것만으로도 고정답변 또는 handler 누락을 loader가 잡는다.

### 10.2 고정답변 분기

```python
response = get_subagent_fixed_response(
    "QUALIFICATION",
    "PROVISIONAL_DISPOSITION_GUIDANCE",
)
assert response is not None
assert response.message_for("11") == "일반 모집인용 가처분 문의 안내문"
assert response.message_for("99") == response.default_message
```

### 10.3 마스터 기대값

| 질문 | 기존 기대 | 변경 기대 |
|---|---|---|
| 가처분 문의는 어디서 해? | EXCEPTION + null | AGENT + QUALIFICATION |
| 압류 문의를 하고 싶어 | EXCEPTION + null | AGENT + QUALIFICATION |
| 날씨 알려줘 | OUT_OF_SCOPE | 변경 없음 |
| 다른 모집인 실적 보여줘 | 정책에 따라 유지 | 명시적으로 테스트 |

### 10.4 E2E 확인

확인할 로그 순서는 다음과 같다.

```text
마스터 의도분류 결과: AGENT / QUALIFICATION
에이전트 코드 비교
서브에이전트 실행 완료: PROVISIONAL_DISPOSITION_GUIDANCE
서브에이전트 이후 라우팅: MCP필요=False
서브에이전트 모집인 구분별 고정 답변 준비
최종 답변 OUTPUT 가드레일 통과
SSE 정상 종료: PASS
```

MCP 호출 로그가 나오면 고정답변 registry 키와 detail 코드가 다르거나 다른 detail이
함께 선택된 것이다.

## 11. 안전한 작업 순서

1. 이전 대상 예외와 담당 agent를 업무적으로 확정한다.
2. 외부 상태가 `EXCEPTION -> PASS`로 바뀌어도 되는지 프론트/WAS와 합의한다.
3. Redis 이력 저장 정책 변경을 허용할지 결정한다.
4. 새 prompt 버전 폴더를 복사해 작업한다.
5. 서브 manifest와 scenario/system prompt에 detail을 추가한다.
6. 같은 커밋에서 `fixed_responses.py`를 등록한다.
7. 서브 bundle 로딩 테스트를 먼저 실행한다.
8. master prompt에서 예외를 제거하고 대상 agent 선택 규칙을 추가한다.
9. `domain.py` enum과 answer manifest의 미사용 항목을 정리한다.
10. master/subagent/E2E 테스트 기대값을 변경한다.
11. 전체 `rg`로 삭제한 예외 코드의 남은 참조를 확인한다.
12. 서버 재시작 후 tester에서 LLM 분류와 MCP 생략 로그를 확인한다.
13. 프론트 agent 일치·불일치 두 경우를 모두 테스트한다.

## 12. 검증 명령

```bash
python -m compileall -q app tests
python -m pytest -q -p no:cacheprovider
```

삭제한 코드의 잔존 참조는 다음처럼 확인한다.

```bash
rg -n "삭제한_EXCEPTION_CODE|새_DETAIL_CODE" app prompts tests docs
```

## 13. 롤백 전략

롤백할 때는 순서를 반대로 적용한다.

1. master prompt에 예외 분류 규칙을 복구한다.
2. `ClassificationType` enum을 복구한다.
3. answer manifest의 exception 문구를 복구한다.
4. 새 고정답변 detail을 master가 더 이상 선택하지 않는지 확인한다.
5. 새 detail·fixed response는 즉시 지우지 않고 한 버전 유지해도 된다.
6. 이전 prompt active version으로 전환한다.

코드와 prompt 버전을 서로 다른 시점에 롤백하면 Structured Output 불일치 또는
manifest 시작 검증 오류가 발생할 수 있으므로 하나의 배포 단위로 다룬다.

## 14. 최종 체크리스트

```text
[ ] 이전할 예외가 전역 보안 정책이 아닌지 검토했다.
[ ] 대상 agent가 업무적으로 확정됐다.
[ ] EXCEPTION -> PASS 상태 변경을 합의했다.
[ ] Redis user/assistant 이력 저장 여부를 합의했다.
[ ] 새 scenario/detail 코드가 기존 코드와 중복되지 않는다.
[ ] detail.parameters는 [] 또는 실제 필요한 값만 가진다.
[ ] scenario Markdown에 선택·제외 반례가 있다.
[ ] subagent system prompt에 우선순위가 있다.
[ ] fixed_responses.py의 키가 manifest detail과 정확히 같다.
[ ] 모집인 구분별 문구와 default 문구를 확인했다.
[ ] master router의 예외 절·우선순위·출력 enum을 모두 정리했다.
[ ] 대상 agent master prompt의 선택/제외 기준을 바꿨다.
[ ] domain enum과 answer manifest 미사용 값을 정리했다.
[ ] 삭제한 예외 코드의 rg 결과를 확인했다.
[ ] 프론트 agent 미선택/일치/불일치를 모두 테스트했다.
[ ] 고정답변 단독 및 다른 MCP detail과의 복합 질문을 테스트했다.
[ ] MCP가 실제로 호출되지 않는 것을 로그로 확인했다.
[ ] OUTPUT 가드레일과 SSE messages/token/end를 확인했다.
[ ] 전체 테스트가 통과했다.
```
