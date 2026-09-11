# 평가와 변경 검증 가이드

## 1. 현재 평가 구성

평가 관련 코드는 네 모듈로 분리되어 있다.

| 모듈 | 역할 | API에서 자동 호출 |
|---|---|---:|
| [`pipeline_evaluator.py`](../app/pipeline_evaluator.py#L1) | CSV 각 행으로 graph `start()` 실행, 기대/실제 비교 | 아니오 |
| [`evaluation_metrics.py`](../app/evaluation_metrics.py#L1) | 분류 accuracy/precision/recall/F1 및 latency 지표 | 아니오 |
| [`evaluation_reporting.py`](../app/evaluation_reporting.py#L1) | JSON, 누적 history.csv, SVG dashboard | 아니오 |
| [`evaluation_logging.py`](../app/evaluation_logging.py#L1) | 평가 진행 로그와 파이프라인 로그 분리 | 아니오 |

현재 저장소에는 이 함수를 호출하는 CLI/script가 없다. 즉 모듈은 library 형태이며, 실제 평가 runner/command를 별도 구성해야 한다. 기존 tests와 평가 스크립트 다수는 작업 트리에서 삭제 상태이므로 이 문서는 현재 남아 있는 코드 계약을 기준으로 한다.

## 2. CSV 파이프라인 평가 입력

진입 함수는 [`evaluate_pipeline_csv()`](../app/pipeline_evaluator.py#L199)다.

### 2.1 필수 컬럼

```text
message
expected_classification_type
expected_agent_code
```

### 2.2 선택 기대 컬럼

```text
expected_status
expected_scenario_code
expected_detail_scenario_code
expected_mcp_tool_name
expected_mcp_succeeded
```

그 외 `test_id`, `category` 같은 입력 컬럼은 결과에도 유지된다. `difficulty`는 출력에서 제외된다.

### 2.3 평가 stage

| stage | 비교 필드 |
|---|---|
| `master` | status, classification_type, agent_code |
| `subagent` | master 필드 + scenario_code + detail_scenario_code |
| `full` | subagent 필드 + mcp_tool_name + mcp_succeeded |

선택 기대값은 CSV에 값이 있을 때만 비교한다. stage에서 실행하지 않은 단계의 기대값은 판정에서 제외된다.

### 2.4 현재 다중 결과 제한

평가의 [`_actual_values()`](../app/pipeline_evaluator.py#L679)는 `actual.subagent`의 기존 단일 primary 필드와 `actual.mcp`의 첫 결과만 기록한다. `subagent.matches[1:]`와 `mcp_results[1:]`은 비교하지 않는다. 다중 시나리오 정확도를 평가하려면 결과 컬럼과 비교 로직을 배열/집합 또는 순서 있는 목록으로 확장해야 한다.

## 3. 행 실행 방식

각 행은 다음 방식으로 독립 실행된다.

- 새 UUID를 `thread_id`로 사용
- employee ID는 호출자가 공통 주입
- conversation/session ID는 graph `start()` signature를 검사해 `conversation_id` 또는 `session_id` 중 실제 인자로 전달
- `frontend_agent_code=None`을 전달해 agent mismatch HITL 없이 master 결과대로 진행
- `asyncio.wait_for()`로 행 제한시간 적용, 기본 180초

signature 호환 계획은 [`_plan_start_call()`](../app/pipeline_evaluator.py#L395)에 있다. 현재 `MasterIntentGraph.start()`는 `session_id`를 받으므로 해당 이름이 선택된다.

결과 CSV는 UTF-8 BOM으로 열고 행을 완료할 때마다 즉시 flush한다. 중간 중단에도 완료된 행은 남는다.

## 4. 평가 출력

입력 컬럼 뒤에 다음 실제 결과가 추가되고 한글 컬럼명으로 저장된다.

- 처리상태, master 유형/agent/refined query
- subagent agent/scenario/detail/parameter 이름
- MCP backend/tool/succeeded/result 존재 여부
- latency
- PASS/FAIL/ERROR
- failure reason/error message/tested_at

MCP 결과 본문은 개인정보 위험 때문에 평가 CSV에 저장하지 않고 존재 여부만 남긴다.

## 5. 지표

[`compute_classification_metrics()`](../app/evaluation_metrics.py#L72):

- accuracy
- label별 precision/recall/F1/support
- macro average
- weighted average
- sparse confusion matrix

[`compute_latency_metrics()`](../app/evaluation_metrics.py#L8):

- mean/min/p50/p90/p95/p99/max/stdev
- 행 latency 합
- wall time
- throughput QPS

[`compute_latency_breakdown()`](../app/evaluation_metrics.py#L38):

- label별 count/mean/p50/p95/max/total
- PASS/FAIL/ERROR별 동일 breakdown

## 6. 리포트 출력

[`save_evaluation_report()`](../app/evaluation_reporting.py#L61)은 다음을 만든다.

```text
<report_dir>/<dataset_name>/
  <dataset>_<stage>_<timestamp>.json
  <dataset>_<stage>_<timestamp>.svg
  history.csv
```

- JSON: 이번 실행 전체 지표와 경로
- `history.csv`: 실행 간 추세용 핵심 값 누적
- SVG: quality/label/confusion/latency/history panel

같은 데이터셋 폴더에서도 latency 추세는 같은 stage끼리만 비교한다. `history.csv` header가 현재 `HISTORY_COLUMNS`와 다르면 자동 migration하지 않고 오류를 낸다.

## 7. library 사용 예시

다음은 별도 평가 script에서 조립할 때의 개념 예시다. 운영 token/Secret은 환경변수로 주입한다.

```python
import asyncio
from pathlib import Path

from app.config import Settings
from app.evaluation_logging import configure_evaluation_logging
from app.graph import MasterIntentGraph
from app.pipeline_evaluator import evaluate_pipeline_csv
from app.prompt_loader import PromptBundleLoader

# create_app()과 동일한 classifier/store/router/executor 조립이 먼저 필요하다.
# runner는 start()를 가진 MasterIntentGraph 인스턴스다.

async def run(runner: MasterIntentGraph, settings: Settings):
    summary = await evaluate_pipeline_csv(
        input_path=Path("datasets/eval.csv"),
        output_path=Path("results/eval_result.csv"),
        runner=runner,
        employee_id="EVAL_USER",
        allowed_agent_codes=PromptBundleLoader().load(
            settings.prompt_version
        ).agent_codes,
        stage="full",
        overwrite=False,
    )
    print(summary)

configure_evaluation_logging(
    pipeline_level="WARNING",
    progress_level="INFO",
)
# asyncio.run(run(runner, Settings.from_env()))
```

현재 코드에는 전체 의존성을 평가용으로 조립해 주는 factory가 없다. `create_app()` 내부 조립과 중복하지 않으려면 후속 구현에서 dependency factory를 추출하거나 FastAPI lifespan의 `app.state.graph`를 안전하게 얻는 평가 harness를 만든다.

## 8. 변경 유형별 최소 검증

### 8.1 Master prompt 변경

- 완결 질문이 이전 history에 오염되지 않는지
- `지난달은?` 같은 생략 후속 질문이 보완되는지
- 5개 예외 유형이 agent보다 우선하는지
- 인접 agent 경계 양쪽 질문
- frontend agent 일치/불일치/미선택
- unknown agent가 structured output에서 불가능한지

### 8.2 Subagent prompt/manifest 변경

- 단일 detail 선택
- 서로 독립적인 복합 질문의 `matches` 순서/중복 제거
- 잘못된 상위 scenario code 자동 보정
- parameter null→빈 문자열
- pattern/allowed_values를 위반한 LLM 출력 실패 처리
- `required_match_rules` 적용/skip 조건

### 8.3 MCP payload 변경

- tool name과 arguments exact match
- 현재월/이전월/기준일/기준년도 기본값
- request context token과 조직코드/employee mapping
- 필수 파라미터 누락 action
- HITL 입력 후 대기 중 match부터 재개
- 완료된 이전 MCP를 재호출하지 않는지

### 8.4 Result formatter/renderables 변경

- list/wrapped/object 형태의 MCP data
- columns 허용목록과 `*`
- 같은 objId 반복
- data 없음/잘못된 item/잘못된 objId
- `answer_text` 비어 있음 오류
- table columns와 row 길이 일치
- HTML escape/Markdown pipe/newline 처리
- renderable code prefix/metadata 자동 추가
- 프론트에서 구조 데이터 렌더링

### 8.5 RAG 변경

- 검색 score 경계값 바로 아래/이상
- score 없음/문자열 문서 제외
- reranker index 중복/범위 오류
- reranking threshold와 top_n
- answerability true/false
- 문서 없음/답변불가 고정 문구
- sourceDocuments와 LLM 입력 문서가 같은지
- 문서 source/metadata에 민감정보가 없는지

### 8.6 SSE/프론트 계약 변경

- 모든 필드 키 존재/null 허용 범위
- HTTP 401 인증 오류와 HTTP 200 SSE fallback 답변을 별도로 처리
- frame이 network chunk 중간에서 잘려도 buffer parser가 복원
- 공통 초기 이벤트 순서
- token 0개/1개/여러 개
- 마지막 messages가 동일 assistant ID를 사용
- INPUT_REQUIRED에도 duration/end가 오는지
- 처리 예외에도 token/messages/duration/end가 오고 `fallbackUsed=true`인지 확인
- action hidden/text input과 validation errors
- renderables/history 재렌더링

## 9. 현재 코드에서 확인된 정합성 주의점

이 절은 코드 수정 없이 현재 상태를 정확히 기록한 것이다.

### 9.1 LLM 호출 상한 회귀 확인

`LLM_TIMEOUT_SECONDS`, `LLM_MAX_TOKENS`, `LLM_MAX_RETRIES`는
[`Settings.llm_client_options`](../app/config.py)를 통해 모든 ChatOpenAI 생성자에
공통 적용된다. 새 LLM 호출 경로를 추가할 때도 이 공통 options를 사용한다.

확인 파일:

- `app/config.py`
- `app/classifier.py`
- `app/subagents/router.py`
- `app/answers.py`
- `app/answerability.py`

### 9.2 평가 예외 유형 허용값

현재 마스터 최종 예외는 `EMPTY_QUERY`, `OUT_OF_SCOPE`이며
[`pipeline_evaluator.CLASSIFICATION_TYPES`](../app/pipeline_evaluator.py#L84)의
허용값과 일치한다.

`OTHER_RECRUITER_DATA_REQUEST`, `CUSTOMER_DISPOSAL_REASON_REQUEST`,
`CUSTOMER_SALES_PERFORMANCE_REQUEST`, `CUSTOMER_PERFORMANCE_DETAIL_REQUEST`는
master classification type이 아니라 `PERFORMANCE_FEE`의 세부 시나리오 코드다.
이 경우의 master 기대값은 `AGENT` / `PERFORMANCE_FEE`로 작성한다.

`PROVISIONAL_DISPOSITION_GUIDANCE`도 master classification type이 아니라
`QUALIFICATION`의 세부 시나리오 코드다. 가처분·압류 문의의 master 기대값은
`AGENT` / `QUALIFICATION`으로 작성한다.

도메인 회귀평가에 포함하려면 허용 set과 관련 테스트 데이터를 확장한다.

### 9.3 Master agent 6개, 구현 subagent 3개

현재 master는 향후 확장 대상 6개 코드를 분류할 수 있고, 실행 가능한 subagent
registry는 `PERFORMANCE_FEE`, `RP`, `QUALIFICATION` 3개다. `FEE_POLICY`,
`PRODUCT_GUIDE`, `TABLET`은 handler 또는 고정답변 구현 후 registry에서 활성화해야
한다. 신규 agent를 추가할 때 master 분류 평가만 통과시키지 말고 subagent 선택,
실제 handler/fixed/RAG 답변과 `/chat` E2E 기대값까지 함께 검증한다.

### 9.4 평가가 최종 답변/SSE를 검증하지 않음

`evaluate_pipeline_csv()`는 graph `MasterResult`까지만 본다. `AnswerService.prepare()`, token 순서, sourceDocuments, renderables, assistant history 저장을 검증하지 않는다. 프론트 output 커스터마이징에는 별도 SSE contract test가 필요하다.

### 9.5 `.env.example`만으로 startup 불충분

필수 GenOS token이 example에 없고 `.env` 자동 loader도 없다. CI/로컬 검증 harness가 명시적으로 환경을 주입해야 한다.

### 9.6 코드서빙 `/chat`의 history/HITL 제한

body `history`는 graph 입력에 직접 사용하지 않고 Redis history만 사용한다. HITL prompt가 나와도 thread/action schema가 응답되지 않아 재진입할 수 없다. `static/chatting.html`만 테스트해서는 운영 SSE 계약을 검증할 수 없다.

### 9.7 노트북 자격증명

하드코딩된 Bearer 값이 있던 `app/test.ipynb`는 제거했다. Git 이력의 값도
폐기/회전하고 이후 테스트는 Secret 주입을 사용한다.

### 9.8 테스트 파일 부재

현재 `tests/`에는 `/chat` route, 가드레일, 운영계 MCP 응답, 순차 MCP workflow, detail별 parameter schema와 개발 trace 계약 테스트가 있다. 신규 시나리오를 추가할 때는 실제 tool argument/응답 fixture와 formatter 예상값을 추가해 자동 회귀 범위를 계속 확장한다. 이 문서의 검증 체크리스트는 아직 fixture가 없는 실제 업무 MCP의 보완 범위를 정하는 기준으로 사용한다.

## 10. 권장 회귀 데이터 구조

Master CSV에는 최소 다음 category를 둔다.

- 각 agent 대표 positive/negative
- 모든 인접 agent 경계
- 프론트 선택 유지/교체
- 모든 예외 유형
- 멀티턴 보정/비보정
- 오타/공백/짧은 질문

Subagent CSV 또는 fixture에는 다음을 둔다.

- 모든 detail 최소 2~3개 positive
- 서로 헷갈리는 detail negative
- 날짜 표현별 parameter 기대값
- 다중 match 질문과 기대 순서
- required match rule 적용/skip

MCP fixture에는 실제 tool별 다음 원본을 보관하되 개인정보를 제거한다.

- success data
- code 1001 no data
- isError
- HTTP error
- JSON response
- SSE response
- columns 누락/반복/잘못된 shape

SSE golden contract에는 각 경로의 event 배열을 저장한다.

- PASS fixed_data
- PASS RAG
- EXCEPTION
- AGENT_CODE_MISMATCH action
- MCP_PARAMETER_REQUIRED action + resume success
- resume validation failure
- HITL not found
- answer stream failure

## 11. 문서 변경 추적

코드 커스터마이징 PR에는 다음을 함께 남기는 것이 좋다.

```text
변경 목적:
영향 agent/scenario/detail:
입력 계약 변경:
MCP tool/arguments 변경:
MCP response sample/version:
답변 text/renderable 변경:
SSE event 변경:
Redis/보안 영향:
prompt version:
평가 dataset/result:
문서 갱신 파일:
```

라인 번호는 변할 수 있으므로 PR 설명에는 파일 경로와 함수/상수/manifest key를 같이 기록한다.
