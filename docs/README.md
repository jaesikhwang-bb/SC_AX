# SC_AX 현재 코드 커스터마이징 문서

이 문서 세트는 2026-09-09 현재 코드와 활성 prompt를 기준으로 한다. 운영 API는
단일 `POST /chat` SSE다. 마스터는 6개 업무를 분류하며, MCP/RAG 또는 고정답변까지
실행 가능한 서브에이전트는 `PERFORMANCE_FEE`, `RP`, `QUALIFICATION` 세 개다.

## 먼저 읽을 문서

1. [아키텍처와 실행 흐름](01_ARCHITECTURE_AND_EXECUTION_FLOW.md)
2. [프론트 input/output 계약](02_FRONTEND_INPUT_OUTPUT_CONTRACT.md)
3. [프롬프트·에이전트·시나리오 추가](04_PROMPT_AGENT_SCENARIO_CUSTOMIZATION.md)
4. [함수 중심 MCP input/output](15_FUNCTION_FIRST_MCP_INPUT_OUTPUT.md)
5. [무결과·오류·action·후속 MCP 정책](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)
6. [Redis/HITL 저장·복원·삭제](18_REDIS_HITL_STORAGE_AND_DELETION.md)
7. [현재 코드 감사·정리 결과](19_CURRENT_CODE_AUDIT_AND_CLEANUP.md)
8. [RP 세부 시나리오 추가 완전 가이드](20_RP_DETAIL_SCENARIO_ADDITION_GUIDE.md)
9. [Databricks RAG 자동 검색·Reranking·대화 이력 답변](21_RAG_AUTOMATIC_SEARCH_PIPELINE.md)
10. [Langfuse 4.14.0 관측 운영 가이드](22_LANGFUSE_4_14_OBSERVABILITY.md)
11. [마스터 EXCEPTION을 서브 고정답변으로 이전](23_MASTER_EXCEPTION_TO_SUBAGENT_FIXED_RESPONSE_MIGRATION.md)
12. [서브에이전트 세부 시나리오 제거](24_SUBAGENT_DETAIL_REMOVAL_GUIDE.md)
13. [서브에이전트 세부 시나리오 추가](25_SUBAGENT_DETAIL_ADDITION_GUIDE.md)
14. [질문 보정 분리·미지원 기능·핵심 FLOW 로그](26_QUERY_REFINEMENT_UNSUPPORTED_AND_FLOW_LOGGING.md)

## 목적별 문서

| 목적 | 문서 |
|---|---|
| 요청 형식, bytes envelope, SSE 이벤트, action output | [02](02_FRONTEND_INPUT_OUTPUT_CONTRACT.md) |
| tool 이름, arguments, access token, 운영계 MCP 파싱 | [03](03_MCP_PAYLOAD_AND_OUTPUT_CUSTOMIZATION.md) |
| master/subagent/detail/parameter/recommendation 추가 | [04](04_PROMPT_AGENT_SCENARIO_CUSTOMIZATION.md) |
| 환경변수, CORS, Redis, 운영 설정 | [05](05_CONFIGURATION_STATE_AND_OPERATIONS.md) |
| 테스트·평가 | [06](06_EVALUATION_AND_VERIFICATION_GUIDE.md) |
| 변경 목적에서 파일 찾기 | [07](07_CUSTOMIZATION_TRACEABILITY_MATRIX.md) |
| 답변·표·action 실전 패턴 | [08](08_FRONTEND_AND_SCENARIO_OUTPUT_CUSTOMIZATION_COOKBOOK.md) |
| 추천질문 0~N개와 `네` 후속 연결 | [09](09_RECOMMENDED_QUESTIONS_CUSTOMIZATION.md) |
| 여러 action과 MCP 왕복 | [10](10_MULTI_STEP_ACTION_AND_MCP_WORKFLOW.md) |
| 혼자 수정하는 순서 | [11](11_SOLO_CUSTOMIZATION_RUNBOOK.md) |
| AI guardrail | [12](12_AI_GUARDRAIL_INTEGRATION.md) |
| tester/chatting 개발 trace | [13](13_DEVELOPMENT_TRACE_CONSOLE.md) |
| 단건/N건/페이지/순차 MCP | [14](14_SEQUENTIAL_MCP_WORKFLOW.md) |
| 원본 결과·grid·페이지별 자유 전처리 | [15](15_FUNCTION_FIRST_MCP_INPUT_OUTPUT.md) |
| MCP 뒤 action과 체크포인트 재개 | [16](16_RESULT_DRIVEN_ACTION_RESUME.md) |
| 네 가지 결과/action 정책 상세 | [17](17_SCENARIO_RESULT_AND_ACTION_POLICY.md) |
| Redis/HITL key·TTL·DEL·장애 정책 | [18](18_REDIS_HITL_STORAGE_AND_DELETION.md) |
| 사용·미사용 코드와 현재 없는 기능 | [19](19_CURRENT_CODE_AUDIT_AND_CLEANUP.md) |
| RP에 신규 detail을 처음부터 끝까지 추가 | [20](20_RP_DETAIL_SCENARIO_ADDITION_GUIDE.md) |
| Action 없는 Databricks RAG 검색과 답변 | [21](21_RAG_AUTOMATIC_SEARCH_PIPELINE.md) |
| 요청 trace·단계 span·LLM 토큰/시간 관측 | [22](22_LANGFUSE_4_14_OBSERVABILITY.md) |
| master 예외를 특정 agent의 fixed detail로 이전 | [23](23_MASTER_EXCEPTION_TO_SUBAGENT_FIXED_RESPONSE_MIGRATION.md) |
| 필요 없는 detail과 연결 코드 안전 제거 | [24](24_SUBAGENT_DETAIL_REMOVAL_GUIDE.md) |
| fixed/MCP/RAG/Action detail 신규 추가 | [25](25_SUBAGENT_DETAIL_ADDITION_GUIDE.md) |
| 과도한 질문 보정 방지, 미지원 detail, 간결한 단계 로그 | [26](26_QUERY_REFINEMENT_UNSUPPORTED_AND_FLOW_LOGGING.md) |

## 현재 구조의 기준

- YAML: 분류 metadata, parameter 정의, 추천질문
- `app/mcp/scenarios/*.py`: tool, arguments, 호출 순서, 결과 조건, output
- `app/scenario_actions.py`: action 입력·검증 공통 계약
- `app/mcp/scenario_runtime.py`: 단건/N건/페이지 호출과 체크포인트
- `app/graph.py`: 상태 흐름, HITL 저장·복원, action 재검증
- `app/mcp/client.py`: JSON-RPC, 인증, 운영계 `structuredContent` 파싱
- `app/answers.py`: detail별 terminal 결과를 최종 답변으로 조합
- `app/subagents/fixed_responses.py`: detail 고정답변, 1001 무데이터 문구,
  RAG 무문서·답변불가 문구
- `app/api.py`: 입력 기본값, CORS, guardrail, SSE, 안전 fallback

MCP tool·arguments·action은 YAML에 선언하지 않는다. 결과 전처리는 detail별
`*_output()` 함수에서 직접 한다.

## 현재 외부 route

| route | 역할 |
|---|---|
| `POST /chat` | 신규 채팅과 action 재개를 모두 처리하는 SSE API |
| `GET /health` | 실행 설정 상태 |
| `GET /tester` | 상세 개발 추적 화면 |
| `GET /chatting` | 채팅과 trace 통합 화면 |

Redis history/HITL 조회·삭제 API와 `/v1/chat*`, `/chat/stream`은 없다.

## 네 가지 시나리오 정책

- 무결과를 답변으로 끝냄: MCP 결과를 그대로 return
- 무결과/오류 후 재입력: 결과 조건에서 `ACTION.request()`
- action 입력 형식 오류: 동일 action에 `errors`를 붙여 재전송
- action 값에 따른 다른 MCP: `ACTION.require()` 반환값으로 handler에서 분기

코드 예제와 체크포인트 무효화 기준은 [17번 문서](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)에
있다.

## Redis 핵심 정책

- history: employee/session/agent별 List, 신규 메시지 저장 시 슬라이딩 TTL
- dedupe: employee/session/message ID별 String
- HITL: endpoint/thread별 JSON String
- HITL 완료: 즉시 `DEL`
- HITL 미완료: TTL 만료
- 입력 검증 실패/다음 action: 같은 key를 다시 저장하고 TTL 갱신
- access token/request context: HITL Redis에 저장하지 않음
- 외부 Redis 관리 API: 없음

정확한 key와 저장 필드는 [18번 문서](18_REDIS_HITL_STORAGE_AND_DELETION.md)를 따른다.

## 변경 후 검증

```text
python -m ruff check app tests
python -m compileall -q app tests
python -m unittest discover -s tests -q
```

목업 테스트에서는 `/tester` 또는 `/chatting`을 열고 같은 action에서 반환된
`thread_id`, 최초 요청과 같은 `session_id`·`endpoint`, 새 `humanInput`을 사용한다.
