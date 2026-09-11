# 현재 코드 커스터마이징 추적 매트릭스

| 바꾸려는 것 | 첫 수정 위치 | 함께 확인할 곳 |
|---|---|---|
| `/chat` request 필드 | `app/models.py:StreamingChatRequest` | `app/api.py:stream_chat()` |
| bytes/`input` envelope | `app/models.py:normalize_json_request_body()` | request validation tests |
| session/thread UUID | `app/api.py:stream_chat()` | SSE 식별자, Redis key |
| endpoint 기본값 | `app/api.py:stream_chat()` | history/HITL namespace |
| CORS | `app/api.py:CORSMiddleware` | reverse proxy 정책 |
| SSE frame 형식 | `app/streaming.py:encode_sse()` | 두 static 화면 |
| action 외부 필드 | `app/streaming.py:build_action_event()` | frontend form |
| INPUT/OUTPUT guardrail | `app/guardrail.py`, `app/api.py` | guardrail tests |
| master agent 경계 | `prompts/intent-classification` | classifier schema |
| detail 선택 | `prompts/subagents/<agent>` | subagent router tests |
| LLM 추출 parameter | subagent manifest + scenario `.md` | Python action 검증 |
| 추천질문 | subagent manifest | `recommended_questions.py` |
| 추천질문 클릭 | `recommended_questions.py`, `graph.py` | question 문장을 일반 질문으로 처리 |
| scenario→handler | `app/mcp/scenarios/registry.py` | active manifest detail code |
| MCP tool/arguments | scenario async handler | MCP trace/request sample |
| access token/user 전달 | handler의 `context.request_context` | Redis 저장 제외 정책 |
| 단건/N건/페이지 | `app/mcp/scenario_runtime.py` 호출 API | detail handler |
| MCP 순차 연결 | 같은 detail handler | stable step code |
| NO_DATA 답변 | handler가 결과 그대로 반환 | `MCP_NO_DATA_MESSAGE` |
| NO_DATA/ERROR action | handler의 `ACTION.request()` | `invalidate_step_codes` |
| action input/검증 | `ScenarioActionDefinition/Input` | graph 재검증 |
| action 값별 MCP 분기 | `ACTION.require()` 다음 Python `if` | checkpoint 정책 |
| MCP 원본 파싱 | `app/mcp/client.py` | compatibility test |
| grid/페이지 전처리 | detail `*_output()` | `ScenarioMcpOutputContext` |
| 답변 문구·표·카드 | detail `*_output()` | `app/renderables.py` |
| 복수 detail 답변 조합 | `app/answers.py` | result 순서 |
| history key/TTL | `app/history.py` | `.env` |
| HITL key/TTL/DEL | `app/hitl_store.py` | `app/graph.py` |
| HITL 저장 필드 | `app/graph.py:_save_hitl_state()` | 민감정보 정책 |
| HITL 재개 소유자 | `app/graph.py:resume()` | session/endpoint 전달 |
| 개발 trace 단계 | `app/observability.py`, `app/csv_trace.py` | static tester |
| 환경변수 | `app/config.py`, `.env.example` | `/health` |

## 핵심 데이터 이동

| 값 | 생성/입력 | 내부 사용 | 저장/출력 |
|---|---|---|---|
| `session_id` | 입력 또는 UUID | history 범위, MCP 문맥 | SSE + history/HITL key |
| `thread_id` | 입력 또는 UUID | 질문/HITL 흐름 | SSE + HITL key |
| `endpoint` | 입력 또는 PROJECT_CODE | request context | Redis namespace |
| `user.id` | 입력 | employee ID | history/HITL scope |
| Authorization token | header | LLM/MCP request context | Redis 저장 안 함 |
| detail parameters | LLM + action | MCP arguments | HITL 중간 state 가능 |
| MCP workflow results | handler runtime | 다음 step/output | action 대기 중 HITL state |
| renderables | detail output | guardrail/table 처리 | assistant messages metadata |
| recommendedQuestions | manifest/registry | 후속 연결 | assistant history metadata |

## 문서 연결

- 결과/action 분기: [17](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)
- Redis/HITL: [18](18_REDIS_HITL_STORAGE_AND_DELETION.md)
- 현재 사용·미사용 코드: [19](19_CURRENT_CODE_AUDIT_AND_CLEANUP.md)
