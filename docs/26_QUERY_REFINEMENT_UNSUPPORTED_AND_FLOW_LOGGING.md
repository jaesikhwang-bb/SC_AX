# 질문 보정 분리·미지원 기능·핵심 FLOW 로그

## 2026-09-10: 문맥 보정을 최종 분류보다 먼저 실행

- 같은 서비스·사원·세션·에이전트 범위의 이력이 있으면 첫 호출부터
  `HISTORY_ALLOWED`로 보정하고, 보정 문장만 에이전트 분류기에 전달한다.
  기존의 CURRENT_ONLY 선판정이 후속 질문을 AGENT로 확정하면서 기간을
  누락하거나 OUT_OF_SCOPE로 종료하던 경로를 제거했다.
- `7월 실적 알려줘 → 수수료는?`는 `7월 수수료 알려줘`로 최소 복원한다.
  현재 질문에 명시한 기간과 목적이 우선이며, 완결 질문·주제 전환에는 이전
  조건을 붙이지 않는다. assistant 추천은 사용자 선택으로 간주하지 않는다.
- `context_classified`는 요청 내부 상태다. 이력 검토 후의 EMPTY_QUERY와
  OUT_OF_SCOPE는 최종 결과이며 같은 요청에서 다시 보정하지 않는다.
- 이력이 없으면 CURRENT_ONLY로 보정한다. 두 모드 모두 보정 1회 → 최종 분류
  1회이며 EMPTY_QUERY/OUT_OF_SCOPE를 정상 업무로 바꾸려고 재호출하지 않는다.
- 에이전트가 미선택이면 다른 에이전트의 최근 이력으로 대체하지 않는다.
  추천질문 클릭은 일반 질문으로 처리하며, 이력 조회에서 추천질문 metadata를 제외한다.
- 미확정 보정 결과 또는 이력 없이 used_history=true인 결과는 원문으로 되돌린다.
  COMPLETE는 지원 여부가 아니라 의미 확정 여부다. 날씨·권한 우회 요청은
  업무 질문으로 변환하지 않고 분류기가 원래 의미대로 예외를 판단한다.
- LLM 연결·스키마 오류는 업무 분류인 EMPTY_QUERY와 구분하며, 이력으로
  숨기거나 성공으로 바꾸지 않는다.

동일 범위 이력의 존재 여부만 모드를 결정한다. 질문 보정 정책은 아래 파일에서 관리한다.


이 문서는 과도한 질문 보정을 방지하기 위해 2026-09-08에 적용한 구조와 운영 시
수정 지점을 설명한다.

## 1. 변경된 전체 흐름

```text
INPUT 가드레일을 통과한 원본 질문
  → 같은 agent 범위 Redis 이력 조회 (미선택이면 이력 없음)
  → 의미 보존 질문 보정 LLM
  → 미확정 보정값이면 원문 유지
  → 마스터 에이전트 분류 LLM (AGENT / EMPTY_QUERY / OUT_OF_SCOPE)
  → 프론트 선택 agent와 비교 및 필요 시 SWITCH_AGENT
  → 선택된 서브에이전트의 세부 시나리오 분류 LLM
  → MATCHED: 고정답변 또는 MCP/RAG
  → UNSUPPORTED: "아직은 지원하지 않는 기능입니다."
  → OUTPUT 가드레일
  → SSE 응답
```

질문 보정과 에이전트 분류는 같은 LLM 모델을 사용할 수 있지만 서로 다른 프롬프트,
Structured Output, 호출 단계다. 분류 LLM이 보정 문장을 다시 쓰더라도 그 값을
버리고 보정기 결과만 최종 `refined_query`로 사용한다.

## 2. 질문 보정 단계

### 파일과 계약

| 역할 | 파일 |
|---|---|
| 보정 프롬프트 | `prompts/intent-classification/v1/router/refinement.md` |
| 활성 파일 선언 | `prompts/intent-classification/v1/manifest.yaml` |
| 보정 결과 모델 | `app/domain.py`의 `QueryRefinement` |
| 실제 LLM 호출 | `app/classifier.py`의 `GenOSIntentClassifier.classify()` |

보정 결과는 다음 세 상태 중 하나다.

- `COMPLETE`: 현재 질문 자체 또는 직접 관련된 이력으로 의미가 확정됨
- `CONTEXT_REQUIRED`: `CURRENT_ONLY`에서 생략 대상을 확정할 수 없음
- `UNRESOLVED`: `HISTORY_ALLOWED`에서도 관련 이력이 없거나 해석이 하나가 아님

`CONTEXT_REQUIRED`와 `UNRESOLVED`에서는 추측해 문장을 완성하지 않고
`refined_query`에 원문을 유지한다. `used_history=true`는 실제 관련 이력으로 생략
대상을 하나로 확정한 `COMPLETE`에서만 허용한다.

### 반드시 지켜야 할 보정 경계

- 오타·띄어쓰기·조사·어순은 수정할 수 있다.
- 원문과 이력에 없는 업무명, 상품명, 사람, 날짜, 금액, 조건은 추가하지 않는다.
- `이 사람 신규야?`를 `이 사람 신규입회 자격기준 알려줘`로 바꾸지 않는다.
- 설명 질문을 데이터 조회로, 상태 확인을 자격기준 질문으로 바꾸지 않는다.
- 현재 질문이 완결되면 이전 이력을 사용하지 않는다.
- 후속 표현이 있을 때만 직접 관련된 가장 가까운 이력 하나로 생략 대상을 복원한다.

보정 정책을 바꿀 때는 `refinement.md`만 수정한다. 에이전트 경계는 이 파일에 넣지
않고 각 `agents/*.md`에서 관리한다.

## 3. 마스터 에이전트 분류 단계

마스터 분류 프롬프트는
`prompts/intent-classification/v1/router/system.md`와 `router/agents.md`,
`agents/*.md`를 결합한다. 이 단계에는 확정된 보정 질문 또는 미확정 시 원문이 전달된다.

- 분류 LLM은 `AGENT`, `EMPTY_QUERY`, `OUT_OF_SCOPE`를 선택한다.
- `refined_query`는 입력 문장을 그대로 복사한다.
- 질문이 업무 관련이지만 세부 기능 지원 여부가 불확실하면 마스터에서 가장
  비슷한 세부 시나리오를 만들지 않는다. 관련 에이전트까지만 선택하고 실제 지원
  여부는 서브에이전트가 확인한다.
- 프론트의 선택 agent는 LLM에 전달하지 않는다. 분류 후 `app/graph.py`의
  `_verify_selection()`이 비교한다.

`CONTEXT_REQUIRED`는 기존 분류기와의 호환용으로 남아 있으며 최종 결과로 오면
그래프가 `EMPTY_QUERY`로 정규화한다. 추가 재분류는 하지 않는다.

## 4. 서브에이전트의 미지원 판정

### Structured Output

`app/subagents/router.py`가 모든 활성 서브에이전트에 공통으로 다음 필드를 요구한다.

```json
{
  "status": "MATCHED",
  "matches": []
}
```

- 실제 세부 시나리오가 있으면 `status=MATCHED`이고 `matches`는 1개 이상이다.
- 등록된 모든 detail을 검토해도 실제 기능이 없으면
  `status=UNSUPPORTED`, `matches=[]`다.
- 다른 agent의 detail이나 가장 비슷한 detail을 대신 선택하면 안 된다.

### LangGraph 분기

`app/graph.py`의 `_run_subagent()`가 `UNSUPPORTED`를 받으면 다음 상태를 만든다.

```text
status = EXCEPTION
approved = false
interrupt = null
direct_answer = 아직은 지원하지 않는 기능입니다.
```

이어지는 `_after_subagent()`는 MCP를 호출하지 않고 종료한다. 고정 문구의 단일
관리 위치는 `app/subagents/fixed_responses.py`의
`UNSUPPORTED_FEATURE_MESSAGE`다.

각 에이전트별 프롬프트에는 반드시 다음 취지의 마지막 판정 규칙을 둔다.

1. 모든 detail을 비교한다.
2. 실제 처리 기능이 있는 detail만 선택한다.
3. 없으면 강제 매칭하지 않고 `UNSUPPORTED`를 반환한다.

## 5. 터미널 기본 로그

`app/observability.py`의 `_CompactFlowInfoFilter`가 기존 `========` 상세 INFO를
숨기고 `FLOW` 경계만 출력한다. WARNING과 ERROR 실패 진단은 계속 출력한다.
모든 로그는 기존 `_LogGuardrailFilter`, `_RequestContextFilter`, 최종 formatter를
그대로 통과한다.

정상 요청에서 확인할 대표 순서는 다음과 같다.

```text
FLOW 요청 도착
FLOW 질문 보정 완료
FLOW LLM 호출 완료 | 단계=master.query_refinement | 토큰/시간
FLOW 마스터 분류 완료
FLOW LLM 호출 완료 | 단계=master.intent_classification | 토큰/시간
FLOW 에이전트 선택 비교
FLOW 세부 시나리오 분류 완료
FLOW LLM 호출 완료 | 단계=subagent... | 토큰/시간
FLOW 서브에이전트 분기
FLOW MCP 도구 조회
FLOW MCP 도구 결과
FLOW RAG 검색 분기                 # RAG인 경우
FLOW Reranking                     # 활성/해당 시
FLOW RAG 답변 가능성               # RAG인 경우
FLOW LLM 호출 완료                 # 최종 RAG 답변인 경우
FLOW 최종 답변 완료
FLOW 요청 처리 종료
```

`FLOW 질문 보정 완료`에는 INPUT 가드레일을 거친 원본과 보정 질문이 같이 나온다.
`FLOW MCP 도구 조회`는 비밀값과 action 민감 파라미터를 마스킹한 arguments의
100자 미리보기를, `FLOW MCP 도구 결과`는 정제된 안전 표현의 100자 미리보기와
건수·상태를 출력한다. 최종 답변 로그는 OUTPUT 가드레일 통과 여부·길이·SSE
이벤트 개수만 기록하고 본문은 기록하지 않는다. MCP 전체 데이터와 RAG 전체
문서도 출력하지 않는다.

오류는 `!!!!!!!! 실패 진단` 로그에서 단계, 코드 위치, 예외 유형, 추정 원인,
수정 위치를 확인한다. 정상 흐름을 찾기 위해 기존 수백 개의 세부 INFO를 읽을
필요가 없다.

## 6. 커스터마이징 체크리스트

### 보정이 여전히 과한 경우

1. `refinement.md`의 금지 규칙과 실패 사례를 추가한다.
2. 에이전트명이나 detail 코드를 보정 프롬프트에 추가하지 않는다.
3. 관련 없는 이력에서도 `used_history=true`가 나오는지 확인한다.
4. 로그의 원본질문과 보정질문을 같은 요청 ID로 비교한다.

### 엉뚱한 에이전트가 선택되는 경우

1. `router/system.md`의 공통 경계를 확인한다.
2. `agents/<agent>.md`의 선택/선택하지 않는 기준을 함께 수정한다.
3. 질문 보정 결과가 이미 변질됐는지 먼저 확인한다.

### 엉뚱한 세부 시나리오가 선택되는 경우

1. 해당 `prompts/subagents/<agent>/v1/system.md`를 확인한다.
2. manifest detail 설명과 scenario Markdown의 긍정·부정 경계를 확인한다.
3. 미지원 질문을 대표 detail에 넣지 말고 `UNSUPPORTED`가 되게 한다.

## 7. 검증 명령

```text
python -m compileall -q app tests
python -m pytest -q -p no:cacheprovider
```

핵심 회귀 테스트는 `tests/test_refinement_and_unsupported.py`에 있다. 분류 LLM이
보정 문장을 바꿔도 반영되지 않는지, 관련 이력 없이 문장을 발명하지 않는지,
모든 서브에이전트가 명시적 `UNSUPPORTED` 구조를 받는지, MCP 없이 고정답변으로
종료하는지를 검증한다.


### 실제 모델 회귀 평가

`tests/test_refinement_live.py`는 기간 상속/교체, 설명 질문 보존, 자격 대상 교체,
해지 방법 후속, 날씨/권한 우회 예외, 문맥 없는 지시어를 실제 GenOS로 평가한다.
일반 테스트에서는 건너뛰며 MCP와 Redis는 호출하지 않는다.
GenOS 접속 설정과 토큰을 환경변수로 지정한 뒤 PowerShell에서 실행한다.

```powershell
$env:RUN_LIVE_REFINEMENT = "1"
python -m unittest discover -s tests -p test_refinement_live.py
```

모의 LLM 테스트 통과는 호출 순서와 원문 보존 장치를 검증하는 것이며 실제 모델의
의미 보정 정확도를 보장하지 않는다. 실제 평가는 별도로 실행해야 한다.


## 활성 조회 월 유지

같은 에이전트의 최근 사용자 발화에서 단일 명시 월을 찾아 보정 LLM에 별도 후보로
전달한다(`app/query_period.py`). assistant 본문과 추천질문은 후보 근거가 아니다.
기간별 실제 값·누락·반영 내역 조회는 문장이 완결돼도 이전 월을 유지하며,
보정 결과의 `inherit_active_period`로 적용 여부를 명시한다. LLM이 월 삽입을
놓쳐도 true인 경우 코드는 이력에서 찾은 실제 월을 보정 문장에 넣는다.
미확정·계산 방법·기준·예외 등은 false이며 업무를 새로 만들어 기간을 붙이지 않는다.

현재 질문의 새 기간은 항상 우선한다. 여러 월, 날짜 범위, 전체 기간, 상대기간은
단일 월로 축약하지 않으며 새로운 기간 표현 뒤에 오래된 월을 대신 사용하지 않는다.
단일 월 후보의 코드 추출은 `7월`, `2026년 7월`, `202607` 형식을 지원한다.
다른 기간 형식의 의미 보정은 기존 LLM 규칙을 사용한다.

보정 질문은 기존 흐름대로 user 이력에 저장되므로 기간 없는 실제 조회가 이어져도
마지막 월이 다음 보정에 전달된다. 이력 범위·개수·만료 정책은 그대로이며 이력이
없거나 만료되면 이전 월을 복원하지 않는다.

서브에이전트에서는 보정 질문의 명시된 단일 월을 `closing_year_month`에 반영한다.
LLM이 null 또는 당월을 반환해도 명시된 월이 우선한다. 일자 파라미터가 설정된
조회와 여러 기간이 섞인 질문에는 이 단일 월 보완을 적용하지 않는다.

예: 2026-09-10 실행에서 `7월 점수반영 안된거 있는거같은데?`는 `202607`을
사용한다. 코드/이력저장/MCP 기본값 우선순위 회귀는 `tests/test_query_period.py`,
실제 모델의 연속 대화 평가는 `tests/test_refinement_live.py`에 있다.
