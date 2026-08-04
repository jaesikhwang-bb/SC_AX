# 스트리밍 채팅 API 연동 명세서

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| API 이름 | Master Agent Streaming Chat API |
| 애플리케이션 버전 | `0.3.0` |
| 문서 기준일 | 2026-08-04 |
| HTTP 메서드 | `POST` |
| 사용자 정의 경로 | `/v1/chat/stream` |
| 요청 형식 | `application/json; charset=utf-8` |
| 정상 응답 형식 | `text/event-stream; charset=utf-8` |
| 스트리밍 방식 | HTTP POST + SSE(Server-Sent Events) |

이 API는 하나의 경로에서 다음 두 요청을 모두 처리한다.

1. 신규 사용자 질문
2. 이전 `action` 이벤트에 대한 HITL(Human In The Loop) 후속 입력

기존 `POST /v1/chat`은 전체 처리가 끝난 후 JSON을 한 번 반환하는 레거시
API다. 신규 프론트는 이 문서의 `POST /v1/chat/stream`을 사용한다.

---

## 2. 호출 URL

### 2.1 로컬 개발 환경

```text
http://127.0.0.1:8080/v1/chat/stream
```

### 2.2 GenOS 코드 서빙 환경

```text
https://genos.genon.ai/api/gateway/code_serving/{serving_id}/v1/chat/stream
```

예를 들어 `serving_id`가 `295`라면 다음과 같다.

```text
https://genos.genon.ai/api/gateway/code_serving/295/v1/chat/stream
```

---

## 3. 요청 헤더

| 헤더 | 타입 | 외부 연동 필수 | 설명 |
|---|---|---:|---|
| `Authorization` | string | Y | `Bearer {AccessToken}` 형식의 GenOS 접근 토큰 |
| `Content-Type` | string | Y | `application/json` |
| `Accept` | string | 권장 | `text/event-stream` |

```http
Authorization: Bearer {AccessToken}
Content-Type: application/json
Accept: text/event-stream
```

현재 FastAPI 애플리케이션은 GenOS Gateway가 `Authorization`을 검증한다고
가정한다. 따라서 애플리케이션에 직접 접속하는 로컬 테스트에서는 토큰이 없어도
호출된다. 운영 외부 규격에서는 반드시 헤더를 전달해야 하며, 토큰을 URL 쿼리
문자열에 넣으면 안 된다.

---

## 4. 요청 본문 전체 규격

### 4.1 필드 정의

| 필드명 | JSON 타입 | 신규 질문 | HITL 재진입 | 제약조건 | 설명 |
|---|---|---:|---:|---|---|
| `message` | string 또는 null | 필수 | 사용 불가 | 1~10,000자 | 사용자가 입력한 원본 질문 |
| `session_id` | string | 필수 | 필수 | 1~200자 | 멀티턴 대화이력 범위. 기존 `conversation_id`와 같은 역할 |
| `thread_id` | string 또는 null | 생략/null | 필수 | 1~200자 | 한 번의 질문과 이어지는 HITL 입력을 연결하는 ID |
| `endpoint` | string | 필수 | 필수 | 1~100자, 영문·숫자·`_`·`-` | 서비스 별칭. 현재 허용값은 `acqsc` |
| `agent_code` | string 또는 null | 선택 | 생략 권장 | 최대 100자 | 프론트에서 사용자가 선택한 서브에이전트 코드 |
| `employee_id` | string | 필수 | 필수 | 1~100자, 영문·숫자·`_`·`-` | 로그인한 모집인의 사원번호 또는 고유 식별자 |
| `humanInput` | array<object> | `[]` 또는 생략 | 1건 이상 필수 | 동일 `code` 중복 불가 | `action.inputs`에 대응하는 사용자 입력 목록 |
| `humanInput[].code` | string | 해당 없음 | 필수 | 1~200자 | `action.inputs[].code`를 그대로 사용 |
| `humanInput[].input` | JSON 값 | 해당 없음 | 필수 | 문자열·숫자·불리언·null·객체·배열 | 사용자가 입력하거나 승인한 값 |

요청 모델은 정의되지 않은 추가 필드를 허용하지 않는다. 오탈자가 있는 필드는
무시되지 않고 HTTP 422 오류가 발생한다.

### 4.2 현재 허용되는 agent_code

현재 활성 마스터 프롬프트 기준 코드는 다음과 같다. 서버에서는 입력값을 대문자로
변환한 후 비교하며, 실제 목록은 `GET /v1/metadata`의 `agent_codes`로 조회하는
방식을 권장한다.

| 코드 | 에이전트 |
|---|---|
| `PERFORMANCE_FEE` | 실적·수수료 에이전트 |
| `QUALIFICATION` | 자격기준 에이전트 |
| `RP` | RP 에이전트 |
| `FEE_POLICY` | 수수료기준 에이전트 |
| `PRODUCT_GUIDE` | 상품안내 에이전트 |
| `TABLET` | 태블릿 에이전트 |

빈 문자열 또는 공백만 있는 `agent_code`는 미선택으로 처리한다. 등록되지 않은
코드는 스트림을 열기 전에 HTTP 422로 거절한다.

### 4.3 신규 질문 판정 규칙

`thread_id`가 없거나 null이면 신규 질문으로 판단한다.

- `message`: 반드시 있어야 한다.
- `humanInput`: 생략하거나 빈 배열이어야 한다.
- `thread_id`: 서버에서 생성하므로 보내지 않는 것을 권장한다.
- `agent_code`: 사용자가 에이전트를 선택하지 않았다면 생략하거나 null로 보낸다.

```json
{
  "message": "내 실적과 수수료를 알려줘",
  "session_id": "session-20260804-001",
  "endpoint": "acqsc",
  "agent_code": "PERFORMANCE_FEE",
  "employee_id": "EMP001",
  "humanInput": []
}
```

프론트 공통 규격 때문에 `thread_id` 키 자체가 반드시 필요하다면 신규 질문에서는
다음처럼 null로 보낼 수 있다.

```json
"thread_id": null
```

신규 질문에 임의의 문자열 `thread_id`를 넣으면 안 된다. 현재 서버는
`thread_id`가 존재하는 모든 요청을 HITL 재진입으로 해석하므로, `message`와
문자열 `thread_id`를 함께 보내면 HTTP 422가 발생한다.

### 4.4 HITL 재진입 판정 규칙

`thread_id`가 문자열이면 이전 `action`에 대한 후속 입력으로 판단한다.

- `thread_id`: 직전 `action.data.thread_id`를 그대로 사용한다.
- `message`: 생략하거나 null이어야 한다.
- `humanInput`: 한 건 이상 필요하다.
- `session_id`: 최초 요청과 동일해야 한다.
- `employee_id`: 최초 요청과 동일해야 한다.
- `agent_code`: 재분류에 사용하지 않으므로 생략을 권장한다.

```json
{
  "session_id": "session-20260804-001",
  "thread_id": "52b0d0c1-5a6b-4b6f-9381-b794a43a50e1",
  "endpoint": "acqsc",
  "employee_id": "EMP001",
  "humanInput": [
    {
      "code": "signal",
      "input": "OK"
    }
  ]
}
```

다른 `employee_id` 또는 다른 `session_id`로 같은 `thread_id`를 재개하면 상태
존재 여부를 노출하지 않기 위해 `HITL_STATE_NOT_FOUND`와 동일하게 처리한다.

---

## 5. SSE 전송 형식

### 5.1 공통 envelope

모든 이벤트는 SSE의 별도 `event:` 행을 사용하지 않고, `data:`에 다음 JSON
envelope를 담는다.

```text
data: {"event":"token","data":"답변 일부"}

```

JSON 객체 뒤에는 반드시 빈 줄을 의미하는 `\n\n`이 붙는다. 프론트는 수신된
바이트를 UTF-8로 디코딩한 뒤 빈 줄 기준으로 이벤트를 분리하고, 각 `data:`의
JSON을 파싱해야 한다.

공통 TypeScript 표현은 다음과 같다.

```ts
type StreamEnvelope<T = unknown> = {
  event: string;
  data: T;
};
```

### 5.2 HTTP 응답 헤더

정상적으로 스트림이 시작되면 다음 헤더를 사용한다.

```http
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

`X-Accel-Buffering: no`는 Nginx 계열 프록시의 응답 버퍼링 방지를 위한 값이다.
GenOS Gateway 또는 별도 프록시가 자체 버퍼링을 한다면 해당 구간에서도 SSE
버퍼링을 비활성화해야 한다.

---

## 6. 출력 이벤트 요약

| 이벤트명 | `data` 타입 | 발생 조건 | 프론트 처리 |
|---|---|---|---|
| `request_id` | string | 수락된 모든 스트림의 첫 이벤트 | 장애·서버 로그 추적 키로 보관 |
| `session_id` | string | 수락된 모든 스트림 | 현재 멀티턴 세션 값 갱신·검증 |
| `thread_id` | string | 수락된 모든 스트림 | 현재 질문/HITL 진행 ID 보관 |
| `message_id` | object | 정상 스트림 초기에 2회 | 사용자·assistant 메시지 객체 생성 |
| `messages` | array<object> | 초기 메시지, 처리 metadata, 최종 답변 | 메시지 트리와 진단 정보 동기화 |
| `sourceDocuments` | array<object> | RAG 모드이고 성공한 MCP 문서가 있을 때 | 출처 영역 표시 |
| `token` | string | 고정답변 또는 LLM 답변이 있을 때 여러 번 | 수신 순서대로 assistant 내용 뒤에 추가 |
| `action` | object | `INPUT_REQUIRED`일 때 | 팝업 또는 입력 폼 생성 |
| `duration` | object | 정상 종료 또는 처리된 스트림 오류 | 성능 로그 표시 |
| `end` | object | 정상 처리 완료 또는 HITL 대기 진입 | 로딩 종료, 입력창 활성화 |
| `error` | object | 스트림을 연 뒤 처리 오류 발생 | 안전한 오류 표시, 입력창 복구 |
| `stopped` | object | 현재 서버에서는 미발행 | 향후 서버 측 중단/별도 중단 API용 예약 이벤트 |

---

## 7. 이벤트별 상세 명세

### 7.1 request_id

한 번의 HTTP 요청을 추적하는 서버 생성 문자열이다.

```text
data: {"event":"request_id","data":"acqsc:0ba42f14-38d8-48c3-8389-72ad95251f08"}
```

| 항목 | 내용 |
|---|---|
| 타입 | string |
| 형식 | `{project_code}:{UUID}` |
| 생명주기 | HTTP 요청 한 번 |
| 용도 | 서버 로그, 장애 추적, 문의 접수 |

HITL 재진입은 새로운 HTTP 요청이므로 새로운 `request_id`가 생성된다.

### 7.2 session_id

요청에서 전달한 멀티턴 대화 식별자를 그대로 반환한다.

```text
data: {"event":"session_id","data":"session-20260804-001"}
```

동일한 사원의 연속 질문에서 같은 `session_id`를 사용해야 Redis 대화이력이
보정 질문에 사용된다. 저장 범위는 기본적으로
`employee_id + session_id + agent_code`다.

### 7.3 thread_id

한 번의 질문 처리와 그 질문에서 발생한 HITL 후속 입력을 연결한다.

```text
data: {"event":"thread_id","data":"52b0d0c1-5a6b-4b6f-9381-b794a43a50e1"}
```

| 요청 종류 | 동작 |
|---|---|
| 신규 질문 | 서버가 UUID를 생성하여 반환 |
| HITL 재진입 | 프론트가 보낸 기존 값을 그대로 반환 |

`thread_id`는 `request_id`가 아니다. HITL 재진입 시 HTTP 요청과
`request_id`는 바뀌지만 `thread_id`는 유지된다.

### 7.4 message_id

초기 스트림에서 사용자와 assistant용으로 각각 한 번씩, 총 두 번 전송한다.

```text
data: {"event":"message_id","data":{"role":"user","id":"32cd..."}}

data: {"event":"message_id","data":{"role":"assistant","id":"d72e..."}}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `role` | string | `user` 또는 `assistant` |
| `id` | string | 서버가 해당 요청에서 생성한 UUID |

프론트는 assistant ID에 빈 답변 버블을 만든 뒤 이후 `token`을 같은 버블에
추가한다.

### 7.5 messages

`messages`는 용도에 따라 같은 요청에서 최대 세 단계로 전달된다.

#### A. 초기 사용자 메시지

신규 질문에서는 `content`가 문자열이다.

```json
[
  {
    "role": "user",
    "id": "32cd...",
    "content": "내 실적과 수수료를 알려줘"
  }
]
```

HITL 재진입에서는 `content`가 요청의 `humanInput` 배열이다.

```json
[
  {
    "role": "user",
    "id": "32cd...",
    "content": [
      {"code": "signal", "input": "OK"}
    ]
  }
]
```

#### B. 처리 결과 metadata

마스터 분류, 서브에이전트 시나리오, MCP 호출 결과를 진단하기 위한 메시지다.
`content`는 빈 문자열이고 실제 처리 정보는 `metadata`에 있다.

```json
[
  {
    "role": "assistant",
    "id": "d72e...",
    "content": "",
    "metadata": {
      "status": "PASS",
      "classification": {
        "refined_query": "내 실적과 수수료를 조회해줘",
        "classification_type": "AGENT",
        "agent_code": "PERFORMANCE_FEE"
      },
      "subagent": {
        "agent_code": "PERFORMANCE_FEE",
        "prompt_version": "v1",
        "scenario_code": "PERFORMANCE_SUMMARY",
        "scenario_name": "실적 종합조회",
        "detail_scenario_code": "PERFORMANCE_SUMMARY_TOTAL",
        "detail_scenario_name": "실적 종합 조회",
        "parameters": {
          "closing_year_month": "202608",
          "reference_date": null
        },
        "matches": [
          {
            "scenario_code": "PERFORMANCE_SUMMARY",
            "scenario_name": "실적 종합조회",
            "detail_scenario_code": "PERFORMANCE_SUMMARY_TOTAL",
            "detail_scenario_name": "실적 종합 조회",
            "parameters": {
              "closing_year_month": "202608",
              "reference_date": null
            }
          }
        ]
      },
      "mcp_results": [
        {
          "backend": "genos",
          "tool_name": "test_tool",
          "request_id": "MCP 추적 ID",
          "arguments": {},
          "succeeded": true,
          "result": {"key": "value"},
          "error": null
        }
      ]
    }
  }
]
```

`metadata`의 상세 필드는 다음과 같다.

##### classification

| 필드 | 타입 | 설명 |
|---|---|---|
| `refined_query` | string | 오타와 관련 대화이력을 반영한 보정 질문 |
| `classification_type` | string | `AGENT`, `EMPTY_QUERY`, `OUT_OF_SCOPE`, `OTHER_RECRUITER_DATA_REQUEST`, `CUSTOMER_DETAIL_REQUEST` |
| `agent_code` | string/null | `AGENT`일 때 분류된 에이전트 코드, 예외면 null |

##### subagent

서브에이전트를 실행하지 않은 예외 또는 HITL 대기 시 null일 수 있다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `agent_code` | string | 실행한 서브에이전트 코드 |
| `prompt_version` | string | 서브에이전트 프롬프트 버전 |
| `scenario_code` | string | 첫 번째 대표 시나리오 코드 |
| `scenario_name` | string | 첫 번째 대표 시나리오 이름 |
| `detail_scenario_code` | string | 첫 번째 대표 세부 시나리오 코드 |
| `detail_scenario_name` | string | 첫 번째 대표 세부 시나리오 이름 |
| `parameters` | object | 첫 번째 대표 세부 시나리오의 추출 파라미터 |
| `matches` | array<object> | 질문에서 선택된 전체 시나리오 목록 |

`matches[]`의 각 객체는 `scenario_code`, `scenario_name`,
`detail_scenario_code`, `detail_scenario_name`, `parameters`를 가진다. 한 질문에서
복수 시나리오가 선택되면 `matches` 순서대로 각각 MCP가 호출된다.

##### mcp_results

| 필드 | 타입 | 설명 |
|---|---|---|
| `backend` | string | MCP 실행 백엔드 식별자 |
| `tool_name` | string | 호출한 MCP 도구명. 현재 테스트는 `test_tool` |
| `request_id` | string | MCP 호출 추적 ID |
| `arguments` | object | MCP에 전달한 최종 인자 |
| `succeeded` | boolean | 호출 성공 여부 |
| `result` | object/null | MCP JSON-RPC의 `result.structuredContent` dict |
| `error` | string/null | 호출 실패 시 안전하게 정리된 오류 내용 |

#### C. 최종 assistant 메시지

모든 `token` 전송이 끝나면 완성된 답변을 한 번 더 보낸다.

```json
[
  {
    "role": "assistant",
    "id": "d72e...",
    "content": "모든 token을 결합한 최종 답변"
  }
]
```

이 이벤트는 메시지 트리 동기화와 최종 무결성 확인 용도다. 프론트가 이미
`token`을 화면에 누적했다면 최종 `content`를 다시 뒤에 붙이지 말고, 기존
assistant 버블의 전체 내용을 이 값으로 교체하거나 동일 여부만 확인해야 한다.

### 7.6 token

사용자에게 표시할 답변 조각이다.

```text
data: {"event":"token","data":"테스트 고정답변"}

data: {"event":"token","data":"입니다.\n1. test"}
```

프론트 처리 원칙:

```ts
assistantMessage.content += envelope.data;
```

- 반드시 수신 순서를 유지한다.
- 각 조각을 독립된 메시지로 만들지 않는다.
- 고정답변은 완성 문자열을 16자 단위로 나눈 조각이다.
- RAG 답변은 GenOS `ChatOpenAI.astream()`에서 받은 생성 청크다.
- GenOS 스트리밍이 첫 청크 전에 실패하면 `ainvoke()` 전체 응답을 받은 뒤
  조각으로 나누는 대체 경로를 사용한다.
- SSE의 `token`은 모델 토크나이저의 정확한 1토큰과 반드시 일치하는 의미가
  아니라, 화면에 순서대로 추가할 텍스트 청크를 뜻한다.

### 7.7 sourceDocuments

`QUALIFICATION`처럼 답변 모드가 `rag`이고 성공한 MCP 결과가 있을 때 `token`
보다 먼저 전달된다.

```json
[
  {
    "document_id": "mcp-request-id:document:1",
    "title": "test_tool 조회 문서",
    "source": "test_tool",
    "content": {
      "document_text": "신규회원 입회 자격기준 문서 내용"
    },
    "metadata": {
      "mcp_request_id": "mcp-request-id",
      "arguments": {
        "member_category": "NEW"
      }
    }
  }
]
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `document_id` | string | 프론트 출처 항목의 고유 ID |
| `title` | string | 화면에 표시할 문서명 |
| `source` | string | 문서를 제공한 MCP 도구명 |
| `content` | object | MCP가 반환한 원본 structuredContent dict |
| `metadata.mcp_request_id` | string | 원본 MCP 호출 추적 ID |
| `metadata.arguments` | object | 해당 문서 조회에 사용한 MCP 인자 |

현재 `PERFORMANCE_FEE`, `RP`는 `fixed_data`이므로 `sourceDocuments`를 보내지
않는다. `QUALIFICATION`은 `rag`이므로 출처 문서를 보내고 그 문서를 LLM 입력에도
사용한다.

### 7.8 action

서버가 사용자 확인이나 추가 파라미터 입력을 기다릴 때 발생한다. 현재 실제로
구현된 action 유형은 `AGENT_CODE_MISMATCH` 한 가지다.

```json
{
  "type": "AGENT_CODE_MISMATCH",
  "thread_id": "52b0d0c1-5a6b-4b6f-9381-b794a43a50e1",
  "message": "선택한 에이전트와 질문 의도가 다릅니다. 분류된 에이전트로 변경하시겠습니까?",
  "inputs": [
    {
      "code": "signal",
      "label": "변경 승인",
      "type": "hidden",
      "required": true,
      "expectedValue": "OK"
    }
  ],
  "context": {
    "frontend_agent_code": "RP",
    "classified_agent_code": "PERFORMANCE_FEE"
  },
  "errors": {}
}
```

#### action 최상위 필드

| 필드 | 타입 | 설명 |
|---|---|---|
| `type` | string/null | 입력 요청 종류. 현재 `AGENT_CODE_MISMATCH` |
| `thread_id` | string | 후속 `/v1/chat/stream` 요청에 넣을 진행 ID |
| `message` | string/null | 팝업 또는 입력 폼 상단 안내문 |
| `inputs` | array<object> | 프론트가 생성해야 할 입력 항목 목록 |
| `context` | object | 화면 안내에 필요한 현재 분류 정보. 재요청에 그대로 보내지 않음 |
| `errors` | object | `{입력코드: 오류문구}` 형식의 검증 오류 |

#### action.inputs[] 필드

| 필드 | 타입 | 설명 |
|---|---|---|
| `code` | string/null | 후속 `humanInput[].code`로 반환할 키 |
| `label` | string/null | 사용자에게 표시할 입력 항목명 |
| `type` | string | 프론트 입력 UI 유형. 누락 시 `text`, 현재 승인은 `hidden` |
| `required` | boolean | 필수 입력 여부 |
| `expectedValue` | JSON 값 | 현재 단계에서 기대하는 값. 승인 action은 `OK` |

`expectedValue`는 프론트 UI 편의를 위한 값이지 인증이나 보안 검증 수단이 아니다.
서버는 후속 입력을 다시 검증한다.

#### 현재 action 유형

| type | 발생 조건 | 입력 코드 | 입력값 | 처리 결과 |
|---|---|---|---|---|
| `AGENT_CODE_MISMATCH` | 프론트 선택 코드와 마스터 분류 코드가 다름 | `signal` | 문자열 `OK` | 분류된 에이전트로 진행 |

잘못된 값을 보내면 새로운 `action`이 다시 반환되고 `errors`에 오류가 포함된다.

```json
{
  "errors": {
    "signal": "OK 값을 입력해야 합니다."
  }
}
```

프론트는 새 action의 오류를 입력 항목별로 표시하고 동일 `thread_id`로 다시
제출한다.

향후 MCP 파라미터 입력을 추가할 때도 새로운 `type`과 `inputs`를 추가하는
방식으로 확장한다. 예를 들면 다음과 같은 모양을 사용할 수 있지만, 아래
`MCP_PARAMETER_REQUIRED`는 현재 구현된 유형이 아니라 확장 예시다.

```json
{
  "type": "MCP_PARAMETER_REQUIRED",
  "inputs": [
    {
      "code": "closing_year_month",
      "label": "마감작업년월",
      "type": "month",
      "required": true,
      "expectedValue": null
    }
  ],
  "errors": {}
}
```

### 7.9 duration

서버가 스트림 처리를 시작한 시점부터의 소요시간이다.

```json
{
  "seconds": 1.245,
  "formatted": "1.245초"
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `seconds` | number | 초 단위 숫자, 소수점 셋째 자리로 반올림 |
| `formatted` | string | 화면 또는 로그 표시용 문자열 |

### 7.10 end

정상적인 스트림 종료를 알린다.

```json
{
  "status": "PASS",
  "thread_id": "52b0d0c1-5a6b-4b6f-9381-b794a43a50e1"
}
```

| 필드 | 타입 | 값/설명 |
|---|---|---|
| `status` | string | `PASS`, `INPUT_REQUIRED`, `EXCEPTION` |
| `thread_id` | string | 현재 질문/HITL 진행 ID |

상태 의미:

| status | 의미 | 프론트 후속 처리 |
|---|---|---|
| `PASS` | 정상 분류·서브에이전트·MCP·답변 완료 | 로딩 종료, 다음 질문 허용 |
| `INPUT_REQUIRED` | action 입력을 기다리는 상태 | action UI 유지, 같은 thread로 입력 제출 |
| `EXCEPTION` | 업무 외·빈 질문·보호 대상 조회 등 고정 예외답변 완료 | 고정답변 표시 후 다음 질문 허용 |

`end`는 마지막 정상 이벤트다. `end`를 받은 후 현재 응답 스트림을 닫는다.

### 7.11 error

스트림을 연 뒤 발생한 오류는 HTTP 상태 코드를 변경할 수 없으므로 `error`
이벤트로 전달한다.

#### HITL 상태 없음

```json
{
  "code": "HITL_STATE_NOT_FOUND",
  "message": "이어 갈 입력 상태를 찾을 수 없습니다. 새 질문으로 다시 시작해 주세요.",
  "thread_id": "52b0d0c1-5a6b-4b6f-9381-b794a43a50e1"
}
```

발생 가능한 원인은 다음과 같다.

- 잘못된 `thread_id`
- TTL이 지나 Redis HITL 상태가 만료됨
- 이미 정상 처리되어 삭제된 상태를 다시 사용함
- 최초 요청과 다른 `employee_id` 또는 `session_id`로 재개함

#### 일반 스트림 처리 오류

```json
{
  "code": "STREAM_PROCESSING_ERROR",
  "message": "요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
  "request_id": "acqsc:0ba42f14-38d8-48c3-8389-72ad95251f08"
}
```

내부 URL, 토큰, 스택 트레이스는 응답으로 보내지 않고 서버 로그에만 기록한다.
현재 구현에서는 `error` 다음에 `duration`을 보내고 스트림을 종료하며 `end`는
보내지 않는다. 프론트는 `error` 자체를 종료 신호로 처리하여 로딩과 입력 잠금을
해제해야 한다.

### 7.12 stopped

현재 구현에는 클라이언트가 실제로 받을 수 있는 `stopped` 이벤트가 없다.
브라우저가 `AbortController.abort()`로 연결을 끊으면 이미 연결이 종료되었으므로
그 연결로 추가 이벤트를 보낼 수 없고, 서버는 중단 사실만 로그에 기록한다.

`stopped`는 향후 별도 중단 API 또는 서버 측 업무 중단 조건을 구현할 때 사용할
예약 이벤트다. 현재 프론트는 `stopped` 수신을 기다리지 말고, 자신이 abort를
실행한 즉시 로컬 화면을 중단 상태로 전환해야 한다.

---

## 8. 이벤트 순서

### 8.1 정상 PASS: 고정답변

```text
request_id
→ session_id
→ thread_id
→ message_id(user)
→ message_id(assistant)
→ messages(초기 user)
→ messages(분류·서브에이전트·MCP metadata)
→ token(여러 번)
→ messages(완성 assistant 답변)
→ duration
→ end(status=PASS)
```

`PERFORMANCE_FEE`, `RP`가 현재 이 흐름을 사용한다.

### 8.2 정상 PASS: RAG 답변

```text
request_id
→ session_id
→ thread_id
→ message_id(user)
→ message_id(assistant)
→ messages(초기 user)
→ messages(분류·서브에이전트·MCP metadata)
→ sourceDocuments
→ token(LLM 생성 청크 여러 번)
→ messages(완성 assistant 답변)
→ duration
→ end(status=PASS)
```

`QUALIFICATION`이 현재 이 흐름을 사용한다.

### 8.3 고정 EXCEPTION 답변

```text
request_id
→ session_id
→ thread_id
→ message_id(user)
→ message_id(assistant)
→ messages(초기 user)
→ messages(EXCEPTION classification metadata)
→ token(고정 예외답변 여러 번)
→ messages(완성 assistant 답변)
→ duration
→ end(status=EXCEPTION)
```

EXCEPTION 질문과 답변은 Redis 대화이력에 저장하지 않는다.

### 8.4 HITL 입력 대기

```text
request_id
→ session_id
→ thread_id
→ message_id(user)
→ message_id(assistant)
→ messages(초기 user)
→ messages(INPUT_REQUIRED metadata)
→ action
→ duration
→ end(status=INPUT_REQUIRED)
```

이 흐름에서는 사용자 최종답변 `token`이 없다.

### 8.5 스트림 내부 오류

```text
초기 식별 이벤트와 messages 일부
→ error
→ duration
→ 연결 종료(end 없음)
```

---

## 9. HTTP 상태 코드와 SSE 오류 구분

| 상황 | HTTP 상태 | Content-Type | 오류 전달 방식 |
|---|---:|---|---|
| 요청 검증 통과, 정상 또는 처리 중 오류 | 200 | `text/event-stream` | SSE `end` 또는 `error` |
| 필수 필드 누락·금지 조합·추가 필드 | 422 | `application/json` | FastAPI/Pydantic JSON 오류 |
| 잘못된 `endpoint` | 422 | `application/json` | `detail.code=INVALID_ENDPOINT` |
| 등록되지 않은 `agent_code` | 422 | `application/json` | `detail.code=INVALID_AGENT_CODE` |
| 스트림 시작 후 HITL 상태 없음 | 200 | `text/event-stream` | `error.code=HITL_STATE_NOT_FOUND` |
| 스트림 시작 후 내부 처리 실패 | 200 | `text/event-stream` | `error.code=STREAM_PROCESSING_ERROR` |

잘못된 endpoint 예시:

```json
{
  "detail": {
    "code": "INVALID_ENDPOINT",
    "message": "이 서비스에서 처리할 수 없는 endpoint입니다.",
    "allowed_endpoint": "acqsc"
  }
}
```

등록되지 않은 agent_code 예시:

```json
{
  "detail": {
    "code": "INVALID_AGENT_CODE",
    "message": "등록되지 않은 agent_code입니다.",
    "allowed_codes": [
      "FEE_POLICY",
      "PERFORMANCE_FEE",
      "PRODUCT_GUIDE",
      "QUALIFICATION",
      "RP",
      "TABLET"
    ]
  }
}
```

프론트는 `response.ok`만 확인해서는 안 된다. HTTP 200이어도 SSE 내부에
`error`가 올 수 있으므로 envelope의 `event`를 끝까지 확인해야 한다.

---

## 10. 전체 응답 예시

### 10.1 PERFORMANCE_FEE 고정답변

아래 예시는 읽기 쉽도록 일부 metadata를 축약했다. 실제 응답은 이벤트마다
한 줄 JSON과 빈 줄로 전송된다.

```text
data: {"event":"request_id","data":"acqsc:0ba42f14-38d8-48c3-8389-72ad95251f08"}

data: {"event":"session_id","data":"session-20260804-001"}

data: {"event":"thread_id","data":"52b0d0c1-5a6b-4b6f-9381-b794a43a50e1"}

data: {"event":"message_id","data":{"role":"user","id":"32cd..."}}

data: {"event":"message_id","data":{"role":"assistant","id":"d72e..."}}

data: {"event":"messages","data":[{"role":"user","id":"32cd...","content":"내 실적을 알려줘"}]}

data: {"event":"messages","data":[{"role":"assistant","id":"d72e...","content":"","metadata":{"status":"PASS","classification":{"refined_query":"내 실적을 조회해줘","classification_type":"AGENT","agent_code":"PERFORMANCE_FEE"},"subagent":{"agent_code":"PERFORMANCE_FEE"},"mcp_results":[{"tool_name":"test_tool","succeeded":true,"result":{"score":100}}]}}]}

data: {"event":"token","data":"테스트 고정답변입니다.\n"}

data: {"event":"token","data":"1. test_tool 조회 결과"}

data: {"event":"token","data":": {\"score\":100}"}

data: {"event":"messages","data":[{"role":"assistant","id":"d72e...","content":"테스트 고정답변입니다.\n1. test_tool 조회 결과: {\"score\":100}"}]}

data: {"event":"duration","data":{"seconds":1.245,"formatted":"1.245초"}}

data: {"event":"end","data":{"status":"PASS","thread_id":"52b0d0c1-5a6b-4b6f-9381-b794a43a50e1"}}

```

### 10.2 에이전트 불일치 action

```text
data: {"event":"action","data":{"type":"AGENT_CODE_MISMATCH","thread_id":"52b0d0c1-5a6b-4b6f-9381-b794a43a50e1","message":"선택한 에이전트와 질문 의도가 다릅니다. 분류된 에이전트로 변경하시겠습니까?","inputs":[{"code":"signal","label":"변경 승인","type":"hidden","required":true,"expectedValue":"OK"}],"context":{"frontend_agent_code":"RP","classified_agent_code":"PERFORMANCE_FEE"},"errors":{}}}

data: {"event":"duration","data":{"seconds":0.532,"formatted":"0.532초"}}

data: {"event":"end","data":{"status":"INPUT_REQUIRED","thread_id":"52b0d0c1-5a6b-4b6f-9381-b794a43a50e1"}}

```

---

## 11. 프론트 수신 구현 예시

이 API는 POST JSON 요청이므로 브라우저 기본 `EventSource`보다 `fetch()`와
`ReadableStream`을 사용하는 것이 적합하다.

```js
async function streamChat(payload, accessToken, onEvent) {
  const response = await fetch("/v1/chat/stream", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Content-Type": "application/json",
      "Accept": "text/event-stream"
    },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    const error = contentType.includes("application/json")
      ? await response.json()
      : {message: await response.text()};
    throw new Error(JSON.stringify(error));
  }

  if (!response.body) {
    throw new Error("스트리밍 응답 본문이 없습니다.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), {stream: !done});

    const blocks = buffer.replaceAll("\r\n", "\n").split("\n\n");
    buffer = blocks.pop() || "";

    for (const block of blocks) {
      const jsonText = block
        .split("\n")
        .filter(line => line.startsWith("data:"))
        .map(line => line.slice(5).trimStart())
        .join("\n");

      if (!jsonText) continue;
      const envelope = JSON.parse(jsonText);
      onEvent(envelope);
    }

    if (done) break;
  }
}
```

이벤트 처리 예시:

```js
function handleStreamEvent({event, data}) {
  switch (event) {
    case "request_id":
      currentRequestId = data;
      break;
    case "session_id":
      currentSessionId = data;
      break;
    case "thread_id":
      currentThreadId = data;
      break;
    case "message_id":
      createMessageIfAbsent(data.role, data.id);
      break;
    case "token":
      appendAssistantText(data);
      break;
    case "sourceDocuments":
      renderSources(data);
      break;
    case "action":
      renderHumanInputForm(data);
      break;
    case "messages":
      synchronizeMessages(data);
      break;
    case "duration":
      renderDuration(data.formatted);
      break;
    case "error":
      showError(data.message);
      unlockInput();
      break;
    case "end":
      finishRequest(data.status);
      unlockInput();
      break;
  }
}
```

---

## 12. curl 테스트

```bash
curl -N -X POST http://127.0.0.1:8080/v1/chat/stream \
  -H 'Authorization: Bearer test-token' \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{
    "message": "내 실적과 수수료를 알려줘",
    "session_id": "session-20260804-001",
    "endpoint": "acqsc",
    "agent_code": "PERFORMANCE_FEE",
    "employee_id": "EMP001",
    "humanInput": []
  }'
```

`-N`은 curl의 출력 버퍼링을 끄므로 각 SSE 이벤트가 도착하는 즉시 확인할 수
있다.

---

## 13. 대화이력과 HITL 상태 관리

| 식별자 | 범위 | 저장 목적 |
|---|---|---|
| `employee_id` | 로그인 사원 | 다른 모집인과 데이터·상태 격리 |
| `session_id` | 여러 질문으로 이어지는 대화 | 멀티턴 이력 조회 |
| `agent_code` | 서브에이전트 | 같은 에이전트 이력만 문맥으로 사용 |
| `thread_id` | 질문 1건과 후속 HITL 입력 | 대기 중인 입력 상태 복원 |
| `request_id` | HTTP 요청 1회 | 로그·장애 추적 |

정상 `PASS`에서는 보정된 사용자 질문과 최종 assistant 답변을
`employee_id + session_id + agent_code` 범위에 저장한다. `EXCEPTION`은 질문과
답변을 저장하지 않는다.

HITL 상태는 LangGraph Checkpointer가 아니라 일반 Redis에 필요한 값만 저장한다.
Redis를 사용할 때 상태는 최초 요청의 `employee_id`와 `session_id`에 묶여 있다.

---

## 14. 프론트 구현 체크리스트

- 신규 질문에서는 문자열 `thread_id`를 보내지 않는다.
- HITL 입력에서는 `action`의 `thread_id`를 그대로 사용한다.
- HITL 입력의 `session_id`와 `employee_id`는 최초 요청과 동일하게 유지한다.
- `humanInput[].code`는 `action.inputs[].code`를 그대로 사용한다.
- `token`은 수신 순서대로 동일 assistant 메시지에 이어 붙인다.
- 마지막 `messages`의 완성 답변을 token 뒤에 중복 추가하지 않는다.
- HTTP 422 JSON 오류와 HTTP 200 SSE `error`를 모두 처리한다.
- `end` 또는 `error`에서 로딩과 입력 잠금을 해제한다.
- RAG에서 `sourceDocuments`가 없을 수도 있다고 가정한다.
- 사용자 중단 시 `stopped`를 기다리지 않고 프론트가 즉시 로컬 상태를 갱신한다.
- POST SSE이므로 기본 `EventSource`가 아니라 `fetch` 스트림을 사용한다.
- 프록시가 SSE를 버퍼링하지 않는지 운영 배포 환경에서 확인한다.
- 재연결 시 자동 재호출은 MCP 중복 조회를 만들 수 있으므로 임의 재시도하지 않는다.
