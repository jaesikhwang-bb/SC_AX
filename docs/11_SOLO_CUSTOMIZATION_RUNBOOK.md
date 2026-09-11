# 혼자 수정하는 End-to-End 런북

## 질문 하나를 추가·변경하는 순서

1. master agent가 맞는지 확인
2. subagent manifest의 scenario/detail을 확인
3. scenario prompt에서 선택 조건과 parameter를 수정
4. 실행 유형을 결정
   - fixed response
   - RAG document search
   - MCP 조회/명령
5. MCP이면 scenario handler에서 tool/arguments/호출 순서를 작성
6. 추가 입력이면 Python action을 등록하고 handler에 `require/request` 배치
7. 성공 output에서 데이터 정제·문장·renderable 작성
8. recommended questions 추가 또는 빈 배열 유지
9. tester에서 state, MCP request, raw result, output handler를 확인
10. 단위 테스트와 실제 `/chat` SSE를 검증

## 실행 흐름

```text
/chat request
→ INPUT guardrail
→ history 조회
→ master 분류/질문 보정
→ agent mismatch 확인 action(필요 시)
→ subagent detail/parameter 추출
→ fixed 또는 RAG 또는 Python MCP handler
→ result 조건 action(필요 시)
→ detail output
→ OUTPUT guardrail
→ token/messages/renderables/recommendations
→ assistant history 저장
```

## 가장 자주 수정하는 파일

| 작업 | 파일 |
|---|---|
| 요청 필드/기본값 | `app/models.py`, `app/api.py` |
| master 분류 | `prompts/intent-classification` |
| detail/parameter/recommendation | `prompts/subagents` |
| MCP/action/output | `app/mcp/scenarios` |
| handler 연결 | `app/mcp/scenarios/registry.py` |
| 최종 답변 조합 | `app/answers.py` |
| 프론트 action/SSE | `app/streaming.py`, `app/api.py` |
| Redis/HITL | `app/history.py`, `app/hitl_store.py`, `app/graph.py` |

## action 개발 순서

1. `ScenarioActionInput`으로 code/label/type/검증 정의
2. `ScenarioActionDefinition`으로 agent/detail/action code 연결
3. module import 때 `register_scenario_action()` 호출
4. 값이 필요하기 전 `ACTION.require(parameters)` 배치
5. MCP 결과 때문에 다시 받을 때 `ACTION.request()` 배치
6. 새 값으로 재조회할 step은 `invalidate_step_codes` 지정
7. 프론트가 같은 session/thread/endpoint를 보내는지 확인
8. invalid input → 같은 action + errors를 테스트

## MCP 개발 순서

1. 실제 운영계 `structuredContent` sample 확보
2. stable `step_code` 결정
3. `context.call/call_many/paginate` 중 선택
4. request context와 parameters로 arguments 작성
5. 결과에 따른 중단/action/후속 tool 분기 작성
6. `*_output()`에서 raw/page/step 결과 전처리
7. registry에 handler/output 연결
8. tester에서 호출 횟수, arguments, outcome, raw result 확인

## 배포 전 점검

- POST route가 `/chat` 하나인지
- CORS를 운영 도메인으로 제한했는지
- debug 화면/trace 접근을 제한했는지
- 두 Redis backend가 운영에서 redis인지
- Redis TTL과 Secret/ACL 정책이 맞는지
- guardrail key와 fail-open 정책이 맞는지
- MCP/LLM timeout과 retry가 부작용 정책에 맞는지
- 실제 팩스/등록 MCP에 멱등성이 있는지
- master agent마다 실제 subagent/output이 있는지
- action 입력 오류, Redis 만료, MCP NO_DATA/ERROR fallback을 테스트했는지

## 검증 명령

```text
python -m ruff check app tests
python -m compileall -q app tests
python -m unittest discover -s tests -q
```

상세 정책은 다음 문서를 사용한다.

- [결과/action 정책](17_SCENARIO_RESULT_AND_ACTION_POLICY.md)
- [Redis/HITL 정책](18_REDIS_HITL_STORAGE_AND_DELETION.md)
- [현재 코드 감사](19_CURRENT_CODE_AUDIT_AND_CLEANUP.md)
