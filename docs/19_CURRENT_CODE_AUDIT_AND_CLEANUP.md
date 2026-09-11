# 현재 코드 기능 감사·정리 결과

> 추천질문 계약 변경: 아래의 affirmative 후속 연결 및 recommendation_id 관련
> 설명은 이전 구현 기록이다. 현재는 해당 기능을 제거했으며 클릭한 문장을
> 일반 질문으로 처리한다. [현재 추천질문 계약](09_RECOMMENDED_QUESTIONS_CUSTOMIZATION.md)을 참고한다.


## 1. 감사 기준

이 문서는 2026-09-03 현재 작업 폴더를 기준으로 다음을 대조한 결과다.

- FastAPI 실제 route
- 활성 prompt `active.yaml`과 subagent registry
- `SCENARIO_HANDLER_REGISTRY` 등록 detail
- Python action registry와 HITL 재개 경로
- MCP transport·결과 adapter·output handler
- Redis history/HITL protocol과 구현
- 목업 화면과 테스트가 참조하는 파일
- import 및 symbol 참조

코드가 import되지 않는다는 이유만으로 평가 도구나 실행 진입점을 삭제하지 않았다.
동적 registry, FastAPI decorator, prompt 파일, notebook처럼 일반 import 분석에 나타나지
않는 사용처를 별도로 확인했다.

## 2. 현재 외부 기능

### HTTP route

| method/path | 용도 |
|---|---|
| `POST /chat` | 유일한 채팅·action 재개 SSE API |
| `GET /health` | 설정·backend 상태 확인 |
| `GET /tester` | 개발 추적 콘솔 |
| `GET /chatting` | 채팅·개발 추적 통합 목업 |

`/v1/chat`, `/v1/chat/stream`, `/chat/stream`, history/HITL 조회·삭제 API는 없다.

### 활성 업무 에이전트

| agent | subagent | Python MCP handler |
|---|---:|---:|
| `PERFORMANCE_FEE` | 있음 | 있음 |
| `RP` | 있음 | 있음 |
| `QUALIFICATION` | 있음 | 있음 |

실행 구현이 없던 `FEE_POLICY`, `PRODUCT_GUIDE`, `TABLET` 마스터 분류 항목과 prompt
파일은 제거했다. 이로써 master가 실행할 수 없는 agent를 선택해 기본 무결과 답변을
만드는 경로가 없어졌다. 나중에 다시 추가하려면 master agent 파일만 추가하지 말고
subagent manifest/router, handler 또는 fixed/RAG output과 E2E 테스트까지 함께
구현해야 한다.

## 3. 새로 추가·확정된 기능

| 기능 | 구현 위치 |
|---|---|
| 누락/빈 문자열/null session·thread·endpoint 허용 | `app/models.py`, `app/api.py` |
| bytes/문자열 `input` envelope 파싱 | `app/models.py` |
| 모든 Origin 개발 CORS | `app/api.py` |
| 단일 `/chat` SSE | `app/api.py`, `app/streaming.py` |
| INPUT/OUTPUT AI guardrail | `app/guardrail.py`, `app/api.py` |
| 문장 단위 token guardrail과 Markdown table guardrail | `app/api.py` |
| Python 함수형 action | `app/scenario_actions.py` |
| 잘못된 action 입력 재표시 | `app/graph.py` |
| MCP 결과 기반 강제 action | `ScenarioActionDefinition.request()` |
| action 이전 MCP 체크포인트 저장·재사용 | `app/mcp/scenario_runtime.py`, `app/graph.py` |
| 재입력 영향 step만 무효화 | `invalidate_step_codes` |
| action 선택에 따른 후속 MCP 분기 | scenario handler의 일반 Python 분기 |
| MCP 단건/N건/페이지/순차 호출 | `ScenarioMcpHandlerContext` |
| detail별 자유 전처리·표·답변 | `app/mcp/scenarios/*_output()` |
| 운영계 structuredContent 단일 파싱 | `app/mcp/client.py` |
| 추천질문 0~N개 및 affirmative follow-up | manifest + `app/recommended_questions.py` |
| 상세 개발 trace | `app/observability.py`, `app/csv_trace.py`, tester |

## 4. 제거한 항목

| 제거 대상 | 이유 |
|---|---|
| `FEE_POLICY`, `PRODUCT_GUIDE`, `TABLET` master prompt 항목 | 실행 subagent/output이 없음 |
| 세 agent prompt 파일 | manifest에서 제거되어 더 이상 로드되지 않음 |
| `ScenarioMcpHandlerOutcome.new_results` | 생산 코드·테스트에서 읽지 않음 |
| registry spec의 `result_formatter` 필드 | 함수형 registry에서 설정·사용되지 않음 |
| 중앙 `filter_mcp_data()` | 활성 output 함수가 사용하지 않는 고정 columns 호환 helper |
| YAML `interaction`/`mcp_workflow` 모델과 parser | 활성 manifest가 사용하지 않고 Python action/handler와 역할이 중복됨 |
| `app/mcp/request_builder.py` | 함수형 handler의 인자를 다시 synthetic workflow로 감싸는 중복 계층 |
| `app/mcp/workflow.py` | 제거된 선언형 fan-out 경로 전용 집계 코드 |
| `app/mcp/workflow_handlers.py` | 제거된 mapper/formatter registry 전용 코드 |
| `tests/test_mcp_workflow.py` | 삭제된 선언형 호환 계층만 검증하던 테스트 |
| Python bytecode와 Ruff cache | 생성물이며 소스가 아님 |

과거 `/v1/chat*` route와 외부 Redis 관리 API는 이미 제거된 상태임을 재확인했다.

## 5. 실행 경로 밖이지만 유지한 파일과 이유

다음은 FastAPI import 그래프 밖에 있어도 별도 진입점이나 개발 도구이므로 유지한다.

| 코드 | 유지 이유 |
|---|---|
| evaluation 모듈 | 서버 route가 아니라 오프라인 평가 도구 체인에서 사용 |
| `app/service.py` | 외부 실행·배포 호환을 위해 유지해야 하는 프로젝트 파일 |
| `notebooks/scenario_pipeline_playground.ipynb` | 시나리오 단위 입력→MCP→output 실험 도구 |

함수형 `ScenarioMcpHandlerContext`가 도구명과 arguments를 실행기에 직접 전달하고,
`GenosMcpToolExecutor`가 JSON-RPC 봉투를 생성한다. 다중·페이지 호출 집계와 HITL
체크포인트는 `scenario_runtime.py` 하나에서 관리한다.

## 6. 현재 의도적인 설계 경계

- YAML은 분류 metadata, parameter 정의, 추천질문을 담당한다.
- action code/message/input/검증은 Python scenario 파일이 담당한다.
- MCP tool/arguments/순서/반복/페이지는 Python handler가 담당한다.
- MCP 결과 컬럼 선택·평탄화·집계·문장·renderable은 detail output 함수가 담당한다.
- 공통 runtime은 HTTP, 인증, 안전한 오류, 원장, trace, 체크포인트만 담당한다.
- 외부 action output은 whitelist 방식이며 내부 context/MCP 원본을 노출하지 않는다.
- Redis 관리 API는 제공하지 않고 내부 실행만 Redis를 사용한다.

## 7. 현재 없는 기능

| 기능 | 현재 상태 | 필요 시 구현 위치 |
|---|---|---|
| HITL 동시 재개 분산 lock | 없음 | `HitlStateStore`에 claim/release 계약 추가 |
| Redis application-level 값 암호화 | 없음 | `hitl_store.py` save/get 직렬화 경계 |
| history 즉시 삭제 API | 의도적으로 없음 | 별도 관리 서버 |
| HITL 조회·삭제 외부 API | 의도적으로 없음 | 별도 관리 서버 |
| 팩스 실제 MCP | disabled placeholder | `performance_fee.py` tool 설정과 arguments |
| `test_tool` 실제 MCP | 개발 예시 placeholder | `performance_fee.py` 복합환산 handler |
| MCP 자동 재시도 | 기본 0회 | `MCP_MAX_RETRIES` |
| graph node 자동 재시도 | 없음 | `MasterIntentGraph.__init__` RetryPolicy |
| 운영 CORS allowlist | 현재 모든 Origin 허용 | `app/api.py` CORSMiddleware |
| 운영 debug 화면 접근통제 | 앱 내부 없음 | reverse proxy/인증 middleware |

부작용 MCP가 있는 운영 환경에서는 HITL 동시 재개보다 먼저 MCP 멱등성을
보장해야 한다. 동일 `thread_id`와 업무 요청 ID를 재전송해도 한 번만 처리하도록
MCP 서버가 설계되어야 한다.

## 8. 남은 파일의 역할

### API·흐름

- `main.py`: ASGI 진입점
- `app/api.py`: route, CORS, guardrail, SSE, fallback
- `app/graph.py`: 분류·HITL·subagent·MCP node/edge
- `app/models.py`: 외부 request 정규화
- `app/streaming.py`: SSE/action 직렬화
- `app/service.py`: 외부 호환 서비스 모듈

### 업무 커스터마이징

- `prompts/intent-classification`: master 분류
- `prompts/subagents`: detail 분류·parameter·추천질문
- `app/mcp/scenarios`: 실제 tool/arguments/결과 분기/output
- `app/scenario_actions.py`: action 공통 계약
- `app/recommended_questions.py`: 추천질문 후속 연결

### 외부 연계·상태

- `app/mcp/client.py`: GenOS MCP JSON-RPC
- `app/guardrail.py`: Bastion Guardian
- `app/history.py`: 대화 history
- `app/hitl_store.py`: HITL state
- `app/reranking.py`, `app/answerability.py`: RAG 보조 호출

### 품질·개발

- `app/observability.py`, `app/csv_trace.py`: 추적
- `app/evaluation_*`, `app/pipeline_evaluator.py`: 오프라인 평가
- `static`: 두 개발 화면
- `tests`: 실행 계약 회귀 테스트
- `notebooks`: scenario 실험

## 9. 변경 후 필수 검증

```text
python -m ruff check app tests
python -m compileall -q app tests
python -m unittest discover -s tests -q
```

추가로 `tests/test_chat_route_contract.py`가 POST route가 `/chat` 하나인지,
`tests/test_scenario_actions.py`가 네 가지 result/action 정책을 검증한다.
