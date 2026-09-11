# 프론트 입력·출력 계약

이 문서는 현재 코드에 실제로 존재하는 HTTP 계약만 설명한다. 외부 Redis
조회·삭제 API와 과거 `/v1/chat`, `/v1/chat/stream` 비스트리밍·구버전 경로는
제거되었으며, Redis는 대화 이력과 HITL 상태를 위한 내부 구현으로만 사용한다.

## 1. 현재 엔드포인트

| Method | Path | 용도 | 인증 | 응답 |
|---|---|---|---|---|
| GET | `/tester` | SSE·action 진단 목업 | 없음 | HTML |
| GET | `/chatting` | `/chat` 단순 채팅 목업 | 없음 | HTML |
| GET | `/health` | 배포 상태 확인 | 없음 | JSON |
| POST | `/chat` | 운영 프론트용 단일 채팅 API | Bearer 필수 | SSE |

라우트는 [`create_app()`](../app/api.py) 안에서 선언한다. `/tester`에 필요한
agent 목록과 backend 정보는 별도 metadata API를 호출하지 않고, 서버가 HTML의
`__SC_AX_TESTER_BOOTSTRAP__` 자리에 JSON을 주입한다.

## 2. `/chat` 입력

검증 모델은 [`StreamingChatRequest`](../app/models.py)다.

| JSON 키 | 타입 | 키 생략 | null | 의미 |
|---|---|---:|---:|---|
| `message` | string | 불가 | 불가 | 1~10,000자 사용자 문장 |
| `session_id` | string | 가능 | 가능 | 대화 이력 범위; 누락/null/빈 문자열/공백이면 서버 생성 |
| `thread_id` | string | 가능 | 가능 | 현재 요청/HITL 식별자; 누락/null/빈 문자열/공백이면 서버 생성 |
| `endpoint` | string | 가능 | 가능 | 요청 서비스 별칭; 누락/null/빈 값이면 `PROJECT_CODE`. Redis history/HITL에는 실제 입력 별칭 사용 |
| `agent_code` | string | 가능 | 가능 | 프론트 선택값; 누락/null/빈 값이면 마스터 자동 분류 |
| `humanInput` | array | 가능 | 가능 | 누락/null/빈 배열은 신규 질문, 항목이 있으면 HITL 재진입 |
| `user` | object | 가능 | 가능 | 사용자 범위와 MCP context; 내부 필드는 모두 선택적 |

추천 버튼 클릭 시 `question` 문장을 그대로 `message`로 전송한다.

개발 단계 요청 모델은 정의되지 않은 최상위·`user`·`humanInput` 필드를 오류 없이
무시한다. `user` 객체의 아래 세 키도 모두 선택적이며 빈 값은 null로 정규화한다.

```json
{
  "id": "K3003980",
  "deptcode": "D001",
  "deptname": "테스트부서"
}
```

`humanInput[]`은 action이 요구한 값을 되돌리는 배열이다.

```json
[
  {"code": "INPUT_FAX", "input": "02", "order": 1},
  {"code": "INPUT_FAX", "input": "1234", "order": 2},
  {"code": "INPUT_FAX", "input": "5678", "order": 3}
]
```

- 요청 모델 자체는 누락 code, null input, 중복 code를 형식 오류로 차단하지 않는다.
- 실제 HITL 처리 단계에서는 `code`가 action 배열의 `inputCode`와 일치해야 한다.
- 같은 `code`의 입력칸이 여러 개면 action과 humanInput의 `order`를 함께 보낸다.
- 실제 재진입을 성공시키려면 이전 action에서 받은 `thread_id`를 보내야 한다.
- `thread_id`만 있고 `humanInput`이 비어 있으면 신규 질문이다.

### 2.1 신규 질문 예시

```json
{
  "message": "원천징수 내역을 조회해줘",
  "session_id": "conversation-001",
  "thread_id": null,
  "endpoint": "acqsc",
  "agent_code": null,
  "humanInput": [],
  "user": {
    "id": "K3003980",
    "deptcode": "D001",
    "deptname": "테스트부서"
  }
}
```

### 2.2 추천질문 클릭 예시

서버가 아래 항목을 보냈다고 가정한다.

```json
{
  "id": "PERFORMANCE_FEE:WITHHOLDING_TAX:1",
  "question": "원천징수 내역을 팩스로 보내줘",
  "interactionType": "prompt"
}
```

버튼 표시와 실제 message에 같은 `question` 문장을 사용한다.

```json
{
  "message": "원천징수 내역 팩스 전송을 진행해 주세요.",
  "session_id": "conversation-001",
  "thread_id": null,
  "endpoint": "acqsc",
  "agent_code": null,
  "humanInput": [],
  "user": {
    "id": "K3003980",
    "deptcode": "D001",
    "deptname": "테스트부서"
  }
}
```

### 2.3 action 재진입 예시

```json
{
  "message": "요청한 추가 입력값을 전달합니다.",
  "session_id": "conversation-001",
  "thread_id": "action에서 받은 thread_id",
  "endpoint": "acqsc",
  "agent_code": null,
  "humanInput": [
    {"code": "INPUT_FAX", "input": "02", "order": 1},
    {"code": "INPUT_FAX", "input": "1234", "order": 2},
    {"code": "INPUT_FAX", "input": "5678", "order": 3}
  ],
  "user": {
    "id": "K3003980",
    "deptcode": "D001",
    "deptname": "테스트부서"
  }
}
```

## 3. bytes처럼 보이는 입력의 정규화

입력 변환의 단일 위치는 [`normalize_json_request_body()`](../app/models.py)다.
다음 형태를 모두 같은 JSON object로 복원한다.

- 일반 JSON object
- 본문 루트의 JSON 문자열
- 본문 루트의 Python bytes 표현 문자열 `"b'{...}'"`
- `{"input": {...}}`
- `{"input": "{...}"}`
- `{"input": "b'{...}'"}`

HTTP JSON에는 실제 Python `bytes` 타입이 없다. Postman에서 `b'...'`를 raw
body에 쓰면 bytes가 아니라 문자열이다. 이 서버는 외부 gateway가 그런 문자열을
만드는 경우까지 허용하지만, 프론트가 직접 호출할 때는 일반 JSON object가 가장
안전하다.

루트 bytes 표현 문자열을 Postman으로 시험할 때는 Body → raw → JSON에서 문자열
전체를 JSON 문자열로 보내야 한다.

```json
"b'{\"message\":\"조회해줘\",\"session_id\":\"s1\",\"thread_id\":null,\"endpoint\":\"acqsc\",\"agent_code\":null,\"humanInput\":[],\"user\":null}'"
```

`{"input": "b'...'"}`도 가능하다. 반면 최상위에 정상 요청 필드가 이미 있으면
그 객체를 직접 요청으로 보므로 업무용 `input` 필드와 envelope가 충돌하지 않는다.

## 4. SSE wire format

헤더는 다음과 같다.

```http
Authorization: Bearer <token>
Content-Type: application/json
Accept: text/event-stream
```

개발 목업은 여기에 `X-Debug-Trace: true`를 추가한다. 이 헤더는 아래 표의 선택적
`trace` 이벤트를 활성화하며 일반 운영 프론트에서는 보내지 않는다.

각 프레임은 표준 SSE의 `event:` 줄 대신 아래 한 줄 JSON envelope를 사용한다.

```text
data: {"event":"token","data":"답변 일부"}

```

직렬화 위치는 [`encode_sse()`](../app/streaming.py)다. 프론트는 빈 줄을 기준으로
frame을 나눈 뒤 JSON을 파싱하고 `event` 값으로 분기한다.

## 5. 이벤트별 output 유입 위치

| event | data | 값이 만들어지는 곳 | 프론트 용도 |
|---|---|---|---|
| `request_id` | string | `app/api.py` | 서버 로그 검색 |
| `session_id` | string | `app/api.py` | 다음 질문에서 재사용 |
| `thread_id` | string | `app/api.py` | 같은 action 재진입 |
| `agent_code` | string | `app/api.py` | 자동분류 또는 승인된 전환 대상 화면 선택 |
| `trace` | developer trace object | `app/observability.py`, `app/api.py` | 실제 Python 단계·파일·함수·오류 표시(개발 헤더 전용) |
| `messages` | message array | `app/api.py` | 완성 말풍선·metadata |
| `sourceDocuments` | document array | `app/answers.py` | RAG 출처 표시 |
| `token` | string | `PreparedAnswer.tokens` | 실시간 답변 조립 |
| `recommendedQuestions` | question array | `app/recommended_questions.py` | 답변 아래 버튼 |
| `action` | action object array | `app/streaming.py` | 순서가 있는 추가 입력 UI |
| `duration` | object | `app/api.py` | 처리시간 |
| `end` | object | `app/api.py` | 요청 완료 |
| `error` | object | `app/api.py` | 관측/오류 안내 |

정상 답변의 일반 순서는 다음과 같다.

```text
request_id → session_id → thread_id → messages(빈 assistant)
→ agent_code(분류 완료 시)
→ sourceDocuments(선택) → token(1회 이상)
→ messages(완성 assistant) → recommendedQuestions(선택)
→ duration → end
```

개발 trace가 활성화되면 위 업무 이벤트 사이에 `trace`가 실시간 삽입된다. 업무
이벤트끼리의 상대 순서는 유지되며 프론트 운영 reducer는 알 수 없는 이벤트를
무시할 수 있어야 한다.

추가 입력이 필요한 경우에는 답변 token 대신 `action`이 온 뒤 `duration`, `end`로
끝난다. 검증/내부 처리 오류도 가능한 한 HTTP 200 SSE 안전 답변으로 변환하지만,
Authorization 누락은 HTTP 401이다.

## 6. `messages`와 화면 확장 데이터

완성 assistant 메시지는 다음 구조다.

```json
{
  "role": "assistant",
  "id": "message-id",
  "parentMessageId": "user-message-id",
  "content": "최종 답변",
  "metadata": {
    "renderables": [],
    "recommendedQuestions": []
  }
}
```

- 답변 본문은 `content`이자 누적한 `token` 전체다.
- 표·차트·카드는 `metadata.renderables`에 들어간다.
- 추천질문은 metadata에도 넣고 별도 이벤트로도 한 번 보낸다.
- 프론트는 같은 assistant `id`의 빈 메시지를 완성 메시지로 교체해야 한다.

`renderables`의 공통 필드는 [`create_renderable()`](../app/renderables.py)이 만든다.

```json
{
  "code": "PERFORMANCE_FEE:DETAIL:table",
  "type": "table",
  "format": "structured",
  "title": "조회 결과",
  "data": {"columns": ["항목", "값"], "rows": [["A", "1"]]},
  "metadata": {}
}
```

실제 표 데이터는 [`app/mcp/result_adapters.py`](../app/mcp/result_adapters.py)의
detail별 formatter에서 생성한다. 프론트 분기는 목업의
`renderMessageRenderables()`와 `renderTableRenderable()`을 참고한다.

## 7. 추천질문 클릭 처리

모든 추천질문은 `interactionType=prompt`다. 클릭한 `question` 문장을 일반
질문으로 보내며, 별도 ID 선택이나 '네' 자동 연결은 없다. 서버는 보정과 의도분류를
통해 처리한다. 추천질문 metadata는 화면 복원용으로만 보관한다.

## 8. `action` 출력과 재진입

[`build_action_event()`](../app/streaming.py)이 내부 interrupt를 프론트 구조로
변환한다.

```json
{
  "event": "action",
  "data": [
    {
      "code": "INPUT_FAX",
      "thread_id": "...",
      "message": "팩스 번호를 입력해 주세요.",
      "inputCode": "INPUT_FAX",
      "order": 1,
      "label": "지역번호",
      "type": "tel",
      "required": true,
      "sensitive": true
    },
    {
      "code": "INPUT_FAX",
      "thread_id": "...",
      "message": "팩스 번호를 입력해 주세요.",
      "inputCode": "INPUT_FAX",
      "order": 2,
      "label": "가운데 번호",
      "type": "tel",
      "required": true,
      "sensitive": true
    },
    {
      "code": "INPUT_FAX",
      "thread_id": "...",
      "message": "팩스 번호를 입력해 주세요.",
      "inputCode": "INPUT_FAX",
      "order": 3,
      "label": "마지막 번호",
      "type": "tel",
      "required": true,
      "sensitive": true
    }
  ]
}
```

`error`와 validation 필드는 값이 있을 때만 포함한다. 내부 Redis/MCP/handler
context는 운영 action output에 포함하지 않는다.

프론트 구현 규칙:

1. `data[]`를 `order` 오름차순으로 정렬해 UI를 만든다.
2. `hidden`은 `expectedValue`, 그 외는 사용자 입력을 사용한다.
3. 다음 요청에 동일한 `session_id`, `thread_id`, `user`를 유지한다.
4. `inputCode`와 `order`를 `humanInput[].code/order`로 그대로 돌려준다.
5. 새 action이 오면 이전 action UI를 교체한다.
6. action 완료 전 같은 버튼을 중복 전송하지 않는다.

### 8.1 에이전트 변경 확인

프론트 선택 코드와 마스터 분류 코드가 다르면 action 배열 1건을 보낸다.

```json
[
  {
    "code": "SWITCH_AGENT",
    "thread_id": "server-thread-id",
    "message": "현재 다른 업무를 진행 중입니다. '태블릿' 업무로 전환하시겠습니까?",
    "inputCode": "SWITCH_AGENT",
    "label": "에이전트 전환 여부",
    "type": "choice",
    "required": true,
    "allowedValues": ["yes", "no"]
  }
]
```

승인 요청은 `humanInput: [{"code":"SWITCH_AGENT","input":"yes"}]`이다.
서버는 저장한 분류 코드로 이어서 실행하고 `agent_code` SSE를 보낸다. 취소 요청은
`input:"no"`이며, 서브에이전트/MCP를 실행하거나 화면 코드를 바꾸지 않고
`전환을 취소했습니다. 이어서 도와드리겠습니다.`를 token으로 보낸다.

### 8.2 팩스번호 입력

팩스 action은 `INPUT_FAX` 배열 3건이다. 프론트는 `order=1,2,3` 순으로 입력칸을
노출하고 아래처럼 같은 code와 각 order를 되돌려 보낸다.

```json
{
  "thread_id": "action에서 받은 thread_id",
  "humanInput": [
    {"code": "INPUT_FAX", "input": "02", "order": 1},
    {"code": "INPUT_FAX", "input": "1234", "order": 2},
    {"code": "INPUT_FAX", "input": "5678", "order": 3}
  ]
}
```

## 9. 단일 `/chat` 계약

채팅 POST 경로는 `/chat` 하나뿐이다. 요청은 `StreamingChatRequest`, 응답은 SSE이며
`token`, 완성 `messages`, `renderables`, `sourceDocuments`, `recommendedQuestions`,
`action`을 모두 이 경로에서 처리한다. 과거 JSON `{code,data.text}` 응답과
`/v1/chat/stream` alias는 제공하지 않는다.

## 10. 프론트 필드나 이벤트를 추가하는 순서

입력 필드 추가:

1. [`StreamingChatRequest`](../app/models.py)에 타입과 null 정책 추가
2. `normalize_json_request_body()` 호출부의 `direct_fields`에 추가
3. [`app/api.py`](../app/api.py)에서 `request_context` 또는 graph state로 전달
4. [`MasterState`](../app/graph.py)와 `start()`에 필요한 경우만 추가
5. MCP가 사용하면 [`app/mcp/scenarios`](../app/mcp/scenarios)의 해당 detail handler에 명시적으로 매핑
6. 신규 질문과 action 재진입 request builder를 모두 수정
7. 일반 JSON, input envelope, bytes 표현 문자열을 회귀 확인

출력 이벤트 추가:

1. event 이름, data schema, 발생 순서를 먼저 고정
2. [`app/api.py`](../app/api.py)에서 `encode_sse()` 호출
3. [`static/intent_tester.html`](../static/intent_tester.html)의 event reducer 추가
4. 실제 프론트 reducer·renderer 추가
5. 빈 값일 때 event를 생략할지 빈 배열을 보낼지 문서화
6. fallback 경로에서도 해당 schema가 필요한지 결정

일반 외부 응답에는 분류 prompt, MCP 원본 전체, access token, 내부 예외 stack을
넣지 않는다. 개발 헤더가 있을 때만 마스킹된 분류·MCP 요약과 함수 위치를 trace로
전달한다. 자세한 사용법은
[`13_DEVELOPMENT_TRACE_CONSOLE.md`](13_DEVELOPMENT_TRACE_CONSOLE.md)를 따른다.
