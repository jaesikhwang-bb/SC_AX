# 설정·상태·운영 가이드

실제 환경변수 로딩은 [`Settings.from_env()`](../app/config.py), 배포 예시는
[`.env.example`](../.env.example)이 기준이다.

## 핵심 설정 그룹

| 그룹 | 환경변수 |
|---|---|
| GenOS LLM | `GENOS_URL`, `GENOS_SERVING_ID`, `GENOS_MODEL`, `GENOS_BEARER_TOKEN` |
| prompt | `INTENT_PROMPT_VERSION` |
| LLM 상한 | `LLM_TIMEOUT_SECONDS`, `LLM_MAX_TOKENS`, `LLM_MAX_RETRIES` |
| project | `PROJECT_CODE` |
| history | `CHAT_HISTORY_BACKEND`, `CHAT_HISTORY_LIMIT`, Redis history prefix/TTL |
| HITL | `HITL_STATE_BACKEND`, Redis HITL prefix/TTL |
| MCP | `MCP_BACKEND`, `MCP_ID`, `MCP_BEARER_TOKEN`, timeout/retry |
| RAG | `RERANKING_*` |
| guardrail | `AI_GUARDRAIL_*` |
| trace | `CSV_TRACE_ENABLED`, `CSV_TRACE_DIR` |

`MCP_BACKEND=http`이면 `MCP_ID`가 필수다. 비밀값은 `.env.example`이나 Git에
기록하지 않는다.

## 개발 설정

```text
CHAT_HISTORY_BACKEND=memory
HITL_STATE_BACKEND=memory
MCP_BACKEND=mock
AI_GUARDRAIL_API_KEY=
```

- memory 저장소는 서버 재시작 시 사라진다.
- guardrail key가 없으면 INPUT/OUTPUT은 네트워크 호출 없이 PASS한다.
- mock MCP는 단위 테스트용이다.

## 운영 설정

```text
CHAT_HISTORY_BACKEND=redis
HITL_STATE_BACKEND=redis
MCP_BACKEND=http
AI_GUARDRAIL_FAIL_OPEN=false
```

- 여러 worker/Pod가 같은 Redis와 prefix/PROJECT_CODE를 사용해야 한다.
- Bearer/guardrail/MCP token은 Secret으로 주입한다.
- debug 화면과 `X-Debug-Trace`는 reverse proxy에서 접근 제한한다.
- 현재 모든 Origin CORS이므로 운영 도메인 확정 후 `app/api.py`의
  `CORSMiddleware`를 allowlist로 바꾼다.

## 상태 저장

- 대화 history: `app/history.py`
- action/HITL: `app/hitl_store.py`
- graph 저장 허용 목록: `app/graph.py:_save_hitl_state()`
- 저장·복원·삭제 전체 정책: [18번 문서](18_REDIS_HITL_STORAGE_AND_DELETION.md)

## 장애 정책

| 장애 | 사용자 처리 |
|---|---|
| history 조회/저장 | 빈 이력 또는 저장 생략 후 계속 |
| HITL 없음/만료 | 안전한 재시작 안내 답변 |
| HITL Redis 장애 | 안전 fallback 답변 |
| MCP NO_DATA | 시나리오 정책에 따라 무결과 답변 또는 action |
| MCP ERROR | 안전 문구 또는 시나리오 재입력 action |
| output/stream 오류 | 내부 원문을 숨긴 고정 답변 |
| guardrail BLOCK | 고정 차단 답변 |

## 자동 재시도

기본값은 LLM과 MCP 모두 0회다. graph node RetryPolicy도 설정하지 않는다. 재시도는
중복 과금·부작용·팩스 중복 전송을 고려한 뒤 적용한다.

| 대상 | 위치 |
|---|---|
| LLM client retry | `LLM_MAX_RETRIES` |
| MCP HTTP retry | `MCP_MAX_RETRIES` |
| graph node retry | `MasterIntentGraph.__init__` |

## 관측성

- 요청 문맥 로그: `app/observability.py`
- 단계/state CSV: `app/csv_trace.py`
- 브라우저 trace: `X-Debug-Trace: true`
- 목업: `/tester`, `/chatting`

운영 SSE에는 debug header가 없으면 trace event가 없다. trace에 token 원문과
sensitive action parameter를 노출하지 않도록 기존 redaction을 유지한다.
