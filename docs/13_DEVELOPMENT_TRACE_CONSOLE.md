# 개발 추적 화면 사용 및 커스터마이징 가이드

## 1. 화면 주소와 목적

| 주소 | 용도 |
|---|---|
| `GET /tester` | 요청 필드, HITL, 추천질문, renderable, 원본 SSE까지 확인하는 주 개발 콘솔 |
| `GET /chatting` | 실제 채팅 UX와 Python 실행 추적을 좌우로 함께 확인하는 통합 화면 |

두 화면은 `POST /chat` 요청에 `X-Debug-Trace: true`를 자동으로 넣는다. 서버는
이 헤더가 있는 요청에만 `trace` SSE 이벤트를 추가한다. 일반 프론트가 헤더를
보내지 않으면 기존 운영 이벤트만 수신하므로 내부 파일명과 함수명이 노출되지
않는다.

## 2. 입력부터 출력까지 추적되는 경계

1. `app/models.py`의 `StreamingChatRequest`: JSON/`input`/bytes 정규화와 필드 검증
2. `app/api.py`의 `create_app.stream_chat`: 인증, ID, endpoint, agent_code 보정
3. `app/guardrail.py`의 `BastionGuardianClient.process_text`: INPUT PASS/MASK/BLOCK
4. `app/graph.py`의 `MasterIntentGraph.start` 또는 `resume`: 신규/HITL 진입
5. `app/graph.py`의 LangGraph 노드: 이력, 분류, 검증, HITL, 서브에이전트, MCP
6. `app/classifier.py`와 `app/subagents/router.py`: 마스터·세부 시나리오 LLM 분류
7. `app/graph.py`와 `app/mcp/client.py`: 1~N 순차 MCP 실행, 운영계 `structuredContent` 응답 파싱, 업무 오류 변환
8. `app/answers.py`의 `DefaultAnswerService.prepare`: 답변·출처·renderable 구성
9. `app/api.py` 출력 처리 함수: Markdown 표와 온점 단위 문장 OUTPUT 가드레일
10. `app/api.py`의 `event_stream`: 모든 SSE 전송과 assistant 대화이력 저장

`@timed`가 붙은 실제 함수는 `app/observability.py`에서 자동으로 STARTED,
COMPLETED, FAILED 또는 STOPPED trace를 만든다. 새 처리 함수를 추가할 때
`@timed("화면에 표시할 단계명")`을 붙이면 목업 타임라인에도 자동 표시된다.

## 3. trace 이벤트 형식

```json
{
  "event": "trace",
  "data": {
    "sequence": 12,
    "offsetMs": 241.82,
    "kind": "python_function",
    "stageCode": "PYTHON::app.graph::MasterIntentGraph._classify_intent",
    "stage": "마스터 에이전트 1차 의도분류",
    "phase": "COMPLETED",
    "source": {
      "file": "app/graph.py",
      "function": "MasterIntentGraph._classify_intent",
      "line": 386
    },
    "durationMs": 183.27
  }
}
```

수동 checkpoint에는 `details`가 추가될 수 있다. graph 결과 checkpoint는 마스터
분류, 서브에이전트 시나리오와 정제된 MCP 결과를 표시한다. state transition은
개발 검증을 위해 대화이력과 업무 입력값도 표시하지만 인증·비밀 키는 마스킹한다.

### LangGraph state transition 이벤트

그래프의 `TraceRecorder.record(...)`가 호출되는 지점마다 `kind`가
`state_transition`인 이벤트도 생성된다. 이 이벤트는 CSV 추적 사용 여부와
관계없이 개발 추적 헤더가 있을 때만 SSE에 포함된다.

Tester의 `LangGraph state · 보정 질문 · 대화이력 · MCP 데이터` 영역은 이
이벤트를 다음 여섯 부분으로 재구성한다.

1. 프론트가 보낸 원본 질문과 마스터 분류기가 만든 `refined_query`
2. `MasterIntentGraph._load_history`가 조회해 실제 분류에 전달한 `history`
3. 서브에이전트가 선택한 세부 시나리오와 추출한 `parameters`
4. registry가 선택한 `mcpHandlerTrace.handlerCode`와 함수 위치
5. 모든 MCP 호출·페이지·집계의 code/순번/도구/request ID/arguments/raw 결과/정제 결과/오류와 시나리오별 terminal 결과
6. 각 기록 지점의 전체 graph state snapshot과 state key 목록

`mcpWorkflowResults`는 중간 호출·페이지·집계를 포함한 실행 원장이고 `mcpResults`는 답변에 쓰는 detail별 terminal 결과다. 2-step 업무에서 첫 조회 결과가 두 번째 호출에 제대로 연결됐는지는 첫 카드의 `raw result`와 다음 카드의 `arguments`를 나란히 비교한다. 함수형 단건은 `FUNCTION`, N회 호출은 `FUNCTION ITEM x/N`, 페이지는 `PAGE x`, 마지막 합성은 `FAN-IN 집계`로 표시된다. `MCP함수페이지판정완료`의 `hasNext/stopReason/nextArgumentKeys`와 handler code를 보면 어느 도구 함수에서 왜 멈췄는지 확인할 수 있다.

대표 state 기록 지점과 코드 위치 매핑은 `app/csv_trace.py`의
`_STATE_TRACE_SOURCES`에서 관리한다. 새로운 그래프 단계에서 state를 보고 싶으면
해당 노드에서 기존 `self._trace_recorder.record("단계명", state)` 패턴을 호출하고,
`_STATE_TRACE_SOURCES`에 파일·함수·수정 안내를 추가한다. 화면 상단에서 별도로
강조할 값은 `_developer_state_focus`에 추가한다.

전체 state는 개발자가 비교할 수 있도록 유지하지만 `access_token`,
`authorization`, `api_key`, `password`, `secret` 계열 키는 값 대신
`***REDACTED***`로 강제 치환된다. 따라서 운영 프론트는 반드시 debug 헤더를
보내지 않아야 하고 개발 화면 접근도 제한해야 한다.

## 4. 오류를 찾는 순서

FAILED 또는 STOPPED 카드에서 다음 순서로 확인한다.

1. `error.code`: 화면과 로그에서 검색할 오류 코드
2. `source.file`: 실제 수정 대상 Python 파일
3. `source.function`: 실패한 함수
4. `error.type`, `error.message`: 직접 예외
5. `error.rootType`, `error.rootMessage`: 래핑 전 근본 예외
6. `customizationHint`: 대표 수정 위치
7. 화면의 `request_id`: 서버 터미널에서 같은 요청의 전체 스택 검색

별도 코드가 없는 예외는 CamelCase 클래스명을 대문자 snake case로 변환한다.
예를 들어 `ValueError`는 `VALUE_ERROR`다. 매핑은 `app/observability.py`의
`error_code_for_exception`에서 변경한다.

## 5. 입력 검증 자체가 실패한 경우

Pydantic 검증은 엔드포인트 함수 진입 전에 발생한다. debug 헤더가 있으면
`request_validation_error_handler`가 `REQUEST_VALIDATION_ERROR` trace를 fallback
SSE에 포함한다. `details.validationErrors[].location`을 `app/models.py` 필드와
비교한다.

Authorization 헤더 자체가 없으면 HTTP 401 JSON이 반환된다. 목업은 HTTP 상태와
JSON 오류도 별도로 개발 콘솔에 표시한다.

## 6. 새 단계와 중간 결과 추가 방법

### 함수 실행 자동 추적

```python
from app.observability import timed

@timed("고객 정보 변환")
async def transform_customer(...):
    ...
```

인수와 반환값은 개인정보 유출 방지를 위해 자동 trace에 포함되지 않는다.

### 안전한 중간 결과 표시

`app/api.py`의 `stream_chat` 내부 `trace_checkpoint(...)` 패턴을 사용한다.
`details`에는 비밀번호나 원문 개인정보 대신 코드·개수·길이·성공 여부와 이미
마스킹된 결과만 넣는다.

### 화면 표현 변경

| 대상 | 수정 위치 |
|---|---|
| 주 타임라인 카드 | `static/intent_tester.html`의 `renderTraceTimeline` |
| 실제 채팅 옆 콘솔 | `static/chatting.html`의 `renderDebug` |
| SSE 이벤트 코드 위치 | `static/intent_tester.html`의 `EVENT_CODE_LOCATIONS` |
| 전체 수정 지점 지도 | `static/intent_tester.html`의 `BACKEND_PIPELINE` |
| workflow state 가공 | `app/csv_trace.py`의 `_developer_state_focus` |
| detail→함수 연결 | `app/mcp/scenarios/registry.py`의 `SCENARIO_HANDLER_REGISTRY` |
| step argument/0~N 호출 결정 | `app/mcp/scenarios/*.py` handler와 `context.call/call_many` |
| next key·페이지 종료 | 해당 시나리오 파일의 callback과 `app/mcp/scenario_runtime.py` |
| detail 최종 전처리·답변 | `SCENARIO_HANDLER_REGISTRY`의 `output_handler`와 해당 `*_output()` |
| 함수형 N건/페이지 결과 병합 | `ScenarioMcpHandlerContext.call_many/paginate` |

## 7. 보안 및 운영 주의사항

- 일반 프론트에서는 `X-Debug-Trace`를 보내지 않는다.
- state trace에는 실제 대화이력·MCP 업무 파라미터가 포함될 수 있으므로 개발자만
  화면에 접근하게 한다. Bearer 토큰과 비밀 키 계열은 서버에서 강제 마스킹한다.
- 공개 환경에서는 프록시에서 `/tester`, `/chatting` 접근을 제한한다.
- trace sink 오류가 업무 답변을 깨지 않도록 `emit_developer_trace`는 진단 오류를
  로그만 남기고 무시한다.

## 8. 회귀 검증

```powershell
python -m unittest discover -s tests -v
python -m ruff check app tests
python -m compileall -q app main.py tests
```

`tests/test_developer_trace.py`가 함수 위치, 정상 phase, 오류 코드, workflow step
노출과 명시적 context가 없을 때 trace가 생성되지 않는 보안 경계를 검증한다.
`tests/test_mcp_function_handlers.py`는 함수형 단건·다중·페이지 호출, RAG 재검색, registry 누락, 고정 arguments/access token 유지, 집계와 handler 오류 fallback을 검증한다.
