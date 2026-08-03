# 세부 시나리오별 GenOS MCP 커스터마이징 가이드

## 1. 문서 목적

현재 모든 세부 시나리오는 다음 공통 테스트 도구를 호출한다.

```yaml
mcp:
  tool_name: "test_tool"
  arguments:
    param1:
      source: "literal"
      value: "data1"
    param2:
      source: "literal"
      value: "data2"
```

운영 단계에서는 세부 시나리오마다 `tool_name`과 도구가 요구하는 인자가
달라진다. 이 문서는 다음 시나리오 하나를 실제 운영 도구로 변경하는 과정을
처음부터 끝까지 설명한다.

```text
마스터 에이전트: RP
최상위 시나리오: APARTMENT_MANAGEMENT_FEE_RP_ELIGIBILITY
세부 시나리오: APARTMENT_RP_LIST
목적: 입력한 아파트명 또는 읍면동으로 관리비 RP 연결 가능 단지 조회
```

예시 운영 MCP 도구명은 다음과 같이 가정한다.

```text
search_apartment_management_fee_rp
```

실제 도구명이 다르면 문서의 예시 도구명만 GenOS에 등록된 이름으로 바꾼다.

---

## 2. 전체 실행 구조

한 요청은 다음 순서로 처리된다.

```text
POST /v1/chat
→ 마스터 LLM이 RP로 분류
→ 프론트 선택 코드와 비교 또는 HITL 승인
→ 마스터가 보정한 질문을 RP 서브에이전트에 전달
→ RP 서브에이전트가 APARTMENT_RP_LIST 선택
→ address 파라미터 추출
→ RP manifest에서 MCP tool_name과 arguments 조회
→ GenOS Gateway MCP tools/call
→ result.structuredContent dict를 API 응답에 그대로 포함
```

Python 라우팅 코드에는 `APARTMENT_RP_LIST`나 운영 도구명을 하드코딩하지
않는다. 다음 키로 manifest의 MCP 설정을 찾는다.

```text
(agent_code, detail_scenario_code)
= (RP, APARTMENT_RP_LIST)
```

따라서 기존에 지원하는 인자 매핑 방식만 사용한다면 일반적인 도구 변경은
manifest 수정만으로 끝난다.

---

## 3. 작업 전 GenOS MCP 도구 계약 확인

manifest를 먼저 수정하지 말고 GenOS에 배포된 도구의 정확한 계약을 확인한다.

반드시 확인할 항목은 다음과 같다.

1. MCP ID
2. MCP Bearer Token
3. 정확한 `tool_name`
4. 도구가 요구하는 argument 이름
5. 필수 argument와 선택 argument
6. 각 argument의 자료형
7. 정상 반환값이 `structuredContent` dict인지
8. 조회 결과가 없을 때의 반환 형식
9. 도구 오류 시 `isError` 또는 JSON-RPC `error` 반환 형식

이 문서에서는 도구 계약을 다음과 같이 가정한다.

| MCP argument | 자료형 | 필수 | 값의 출처 |
|---|---|---:|---|
| `employee_no` | string | 예 | 채팅 요청의 `employee_id` |
| `search_keyword` | string | 예 | 서브에이전트가 추출한 `address` |
| `request_channel` | string | 예 | 고정값 `CHAT` |

예상 반환값:

```json
{
  "apartments": [
    {
      "apartment_name": "잠실 예시 아파트",
      "address": "서울특별시 송파구 잠실동",
      "rp_available": true
    }
  ],
  "total_count": 1
}
```

이 객체는 GenOS 응답에서 다음 위치에 있어야 한다.

```text
JSON-RPC 응답
└── result
    └── structuredContent
        ├── apartments
        └── total_count
```

현재 애플리케이션은 `structuredContent`가 dict가 아니면 MCP 실패 결과로
처리한다.

---

## 4. 환경설정 확인

`.env`에는 실제 GenOS MCP 호출 설정이 있어야 한다.

```env
GENOS_URL=https://genos.genon.ai

MCP_BACKEND=http
MCP_ID=475
MCP_BEARER_TOKEN=<실제 MCP 토큰>
MCP_TIMEOUT_SECONDS=30
```

실제 endpoint는 코드에서 다음과 같이 조합한다.

```text
{GENOS_URL}/api/gateway/mcp/{MCP_ID}/mcp
```

예:

```text
https://genos.genon.ai/api/gateway/mcp/475/mcp
```

MCP 토큰은 로그에 출력하지 않는다. 운영에서는 `.env` 파일보다 Kubernetes
Secret 등 외부 보안 저장소에서 환경변수로 주입하는 것이 안전하다.

### MCP 서버가 모든 시나리오에서 동일한 경우

현재 구조를 그대로 사용한다.

- `MCP_ID`: 애플리케이션 공통
- `MCP_BEARER_TOKEN`: 애플리케이션 공통
- `tool_name`: 세부 시나리오 manifest별 설정

### 시나리오마다 MCP 서버 자체가 다른 경우

현재 코드는 하나의 MCP ID와 토큰을 공통 사용한다. 시나리오마다 MCP ID 또는
토큰까지 달라져야 한다면 `tool_name` 변경만으로는 부족하다. 이 경우 manifest에
MCP 연결 프로필을 추가하고 실행기에서 프로필별 클라이언트를 선택하는 별도
확장이 필요하다.

---

## 5. 프롬프트 버전 생성

운영 중인 `v1`을 직접 덮어쓰기보다 새 버전을 만드는 방식을 권장한다.

기존 구조:

```text
prompts/subagents/rp/
├── active.yaml
└── v1/
    ├── manifest.yaml
    ├── system.md
    └── scenarios/
```

변경 후 권장 구조:

```text
prompts/subagents/rp/
├── active.yaml
├── v1/
└── v2/
    ├── manifest.yaml
    ├── system.md
    └── scenarios/
```

작업 순서:

1. `v1` 디렉터리를 `v2`로 복사한다.
2. 먼저 `v2/manifest.yaml`을 수정한다.
3. `active.yaml`은 아직 `v1`로 유지한다.
4. `v2`를 지정한 로딩 테스트를 실행한다.
5. 테스트가 통과한 뒤 `active.yaml`을 `v2`로 변경한다.

`v2/manifest.yaml` 상단 예시:

```yaml
version: "2.0.0"
released_at: "2026-07-31"
agent_code: "RP"
```

활성화할 때 `active.yaml`:

```yaml
active_version: "v2"
```

문제가 발생하면 `active_version`을 다시 `v1`로 변경해 즉시 롤백할 수 있다.

---

## 6. 조회 파라미터 정의 확인

예시 시나리오는 사용자 질문에서 아파트명 또는 읍면동을 추출해야 한다.

RP manifest의 `parameter_definitions`에는 다음 정의가 있어야 한다.

```yaml
parameter_definitions:
  address:
    description: "아파트명 또는 읍면동 기반 검색값. 질문에 없으면 null"
```

이 선언으로 동적 Pydantic 모델에 다음 필드가 생성된다.

```text
parameters.address: string | null
```

RP 프롬프트에는 다음 추출 규칙도 있어야 한다.

```markdown
- `APARTMENT_RP_LIST`에서는 사용자가 입력한 아파트명 또는 읍면동 검색값을
  `address`로 추출한다.
```

현재 RP 프롬프트에는 이미 이 정의와 규칙이 있으므로 예시 작업에서는 추가로
수정할 필요가 없다.

### 새 파라미터가 필요한 경우

예를 들어 도구가 `city_code`를 요구하고 LLM이 질문에서 이를 추출해야 한다면
다음 세 부분을 함께 수정한다.

1. `parameter_definitions`에 `city_code` 추가
2. 시나리오 Markdown에 추출 기준 추가
3. 해당 세부 시나리오의 `parameter_rules`와 `mcp.arguments`에 연결

파라미터 정의만 추가하고 프롬프트에 추출 규칙을 쓰지 않으면 LLM이 안정적으로
값을 채우지 못할 수 있다.

---

## 7. 세부 시나리오 MCP 설정 변경

대상 파일:

```text
prompts/subagents/rp/v2/manifest.yaml
```

현재 테스트 설정:

```yaml
- code: "APARTMENT_RP_LIST"
  name: "아파트관리비 RP 연결 가능 목록 조회"
  description: "검색값이 포함된 아파트 중 관리비 자동납부 연결 가능 단지를 조회"
  mcp:
    tool_name: "test_tool"
    arguments:
      param1:
        source: "literal"
        value: "data1"
      param2:
        source: "literal"
        value: "data2"
  parameter_rules:
    address:
      default: "NONE"
```

운영 도구 설정:

```yaml
- code: "APARTMENT_RP_LIST"
  name: "아파트관리비 RP 연결 가능 목록 조회"
  description: "검색값이 포함된 아파트 중 관리비 자동납부 연결 가능 단지를 조회"
  mcp:
    tool_name: "search_apartment_management_fee_rp"
    arguments:
      employee_no:
        source: "context"
        name: "employee_id"
      search_keyword:
        source: "parameter"
        name: "address"
      request_channel:
        source: "literal"
        value: "CHAT"
  parameter_rules:
    address:
      default: "NONE"
```

핵심은 MCP argument 이름과 내부 값의 출처를 분리하는 것이다.

```text
MCP argument 이름: search_keyword
내부 파라미터 이름: address
```

두 이름이 달라도 `source: parameter`, `name: address`로 연결할 수 있다.

---

## 8. arguments source 사용법

현재 공통 실행기는 세 가지 source를 지원한다.

### 8.1 `literal`

항상 같은 값을 전달한다.

```yaml
request_channel:
  source: "literal"
  value: "CHAT"
```

생성 결과:

```json
{
  "request_channel": "CHAT"
}
```

### 8.2 `parameter`

서브에이전트가 추출하고 애플리케이션이 기본값 정책을 적용한 파라미터를
전달한다.

```yaml
search_keyword:
  source: "parameter"
  name: "address"
```

사용자 질문:

```text
잠실동에서 관리비 자동납부 가능한 아파트를 알려줘
```

서브에이전트 결과:

```json
{
  "parameters": {
    "address": "잠실동",
    "closing_year_month": null,
    "reference_date": null
  }
}
```

MCP argument:

```json
{
  "search_keyword": "잠실동"
}
```

`name`은 반드시 같은 manifest의 `parameter_definitions`에 존재해야 한다.
존재하지 않으면 애플리케이션 시작 시 manifest 검증 오류가 발생한다.

### 8.3 `context`

LLM이 추출하지 않고 API 및 그래프 실행 문맥에서 가져온다.

```yaml
employee_no:
  source: "context"
  name: "employee_id"
```

현재 사용할 수 있는 context 이름:

| context 이름 | 의미 |
|---|---|
| `project_code` | 프로젝트 코드. 현재 `acqsc` |
| `employee_id` | 요청을 보낸 로그인 사원번호 |
| `conversation_id` | 멀티턴 대화 식별자 |
| `thread_id` | 한 번의 채팅 또는 HITL 실행 식별자 |
| `agent_code` | 마스터가 확정한 서브에이전트 코드 |
| `scenario_code` | 서브에이전트가 선택한 최상위 시나리오 코드 |
| `detail_scenario_code` | 선택한 세부 시나리오 코드 |

카드 모집인 본인 조회에는 사용자 질문에서 사원번호를 추출하지 말고
`context.employee_id`를 사용하는 것이 안전하다.

---

## 9. 실제 생성되는 GenOS MCP 요청

다음 채팅 요청을 가정한다.

```json
{
  "message": "잠실동에서 관리비 자동납부 가능한 아파트를 알려줘",
  "employee_id": "EMP001",
  "conversation_id": "conversation-rp-001",
  "frontend_agent_code": "RP"
}
```

신규 질문에는 `thread_id`를 보내지 않는다. 서버가 새 `thread_id`를 생성한다.
`thread_id`가 있는 요청은 HITL 재진입 요청으로 해석되기 때문이다.

서브에이전트가 `APARTMENT_RP_LIST`와 `address=잠실동`을 반환하면 공통 MCP
실행기는 다음 JSON-RPC payload를 만든다.

```json
{
  "jsonrpc": "2.0",
  "id": "acqsc:EMP001:conversation-rp-001:생성된-thread-id",
  "method": "tools/call",
  "params": {
    "name": "search_apartment_management_fee_rp",
    "arguments": {
      "employee_no": "EMP001",
      "search_keyword": "잠실동",
      "request_channel": "CHAT"
    }
  }
}
```

요청 헤더:

```http
Authorization: Bearer <MCP_BEARER_TOKEN>
Accept: application/json, text/event-stream
Content-Type: application/json
```

JSON-RPC `id` 형식:

```text
{project_code}:{employee_id}:{conversation_id}:{thread_id}
```

이 ID로 애플리케이션 로그, 사용자 요청, GenOS Gateway 로그를 연결해 추적할 수
있다.

---

## 10. GenOS 응답과 API 반환값

GenOS가 SSE로 다음과 같이 응답한다고 가정한다.

```text
data: {
  "jsonrpc": "2.0",
  "id": "acqsc:EMP001:conversation-rp-001:생성된-thread-id",
  "result": {
    "structuredContent": {
      "apartments": [
        {
          "apartment_name": "잠실 예시 아파트",
          "address": "서울특별시 송파구 잠실동",
          "rp_available": true
        }
      ],
      "total_count": 1
    }
  }
}
```

애플리케이션은 `result.structuredContent`만 추출해 다음처럼 반환한다.

```json
{
  "status": "PASS",
  "thread_id": "생성된-thread-id",
  "classification": {
    "refined_query": "잠실동에서 관리비 자동납부 가능한 아파트를 알려줘",
    "classification_type": "AGENT",
    "agent_code": "RP"
  },
  "subagent": {
    "agent_code": "RP",
    "prompt_version": "v2",
    "scenario_code": "APARTMENT_MANAGEMENT_FEE_RP_ELIGIBILITY",
    "scenario_name": "아파트관리비 RP 연결 가능 단지 조회",
    "detail_scenario_code": "APARTMENT_RP_LIST",
    "detail_scenario_name": "아파트관리비 RP 연결 가능 목록 조회",
    "parameters": {
      "address": "잠실동",
      "closing_year_month": null,
      "reference_date": null
    }
  },
  "mcp": {
    "backend": "http",
    "tool_name": "search_apartment_management_fee_rp",
    "request_id": "acqsc:EMP001:conversation-rp-001:생성된-thread-id",
    "arguments": {
      "employee_no": "EMP001",
      "search_keyword": "잠실동",
      "request_channel": "CHAT"
    },
    "succeeded": true,
    "result": {
      "apartments": [
        {
          "apartment_name": "잠실 예시 아파트",
          "address": "서울특별시 송파구 잠실동",
          "rp_available": true
        }
      ],
      "total_count": 1
    },
    "error": null
  },
  "interrupt": null
}
```

별도의 답변 생성 LLM은 호출하지 않는다. MCP가 반환한 dict는 `mcp.result`에
그대로 들어간다.

---

## 11. 코드 변경이 필요 없는 경우

다음 조건을 모두 만족하면 Python 코드를 수정하지 않는다.

- 기존 GenOS MCP endpoint와 토큰을 그대로 사용
- 세부 시나리오별 `tool_name`만 변경
- argument 값이 `literal`, `parameter`, `context` 중 하나
- MCP 정상 결과가 `result.structuredContent` dict
- 필수 파라미터 누락 시 도구가 빈 결과 또는 업무 오류 dict를 반환할 수 있음

수정 대상은 보통 다음 파일뿐이다.

```text
prompts/subagents/{agent-directory}/{version}/manifest.yaml
```

파라미터 분류 기준이 달라지면 해당 버전의 `system.md` 또는
`scenarios/*.md`도 함께 수정한다.

---

## 12. Python 코드 변경이 필요한 경우

다음 요구사항은 manifest 변경만으로 처리할 수 없다.

### 새로운 argument source가 필요한 경우

예: Redis 값, 현재 시각, 암호화된 사용자 토큰을 직접 주입해야 하는 경우.

수정 위치:

```text
app/subagents/prompt_loader.py
app/mcp/client.py
```

두 부분을 반드시 같이 수정한다.

1. manifest 검증기가 새 source를 허용
2. `_resolve_arguments()`가 실제 값을 생성

검증만 추가하고 실행 로직을 추가하지 않거나 그 반대로 수정하면 런타임 오류가
발생한다.

### 시나리오마다 MCP ID 또는 토큰이 다른 경우

현재 `Settings`와 `ManifestMcpToolExecutor`는 하나의 endpoint와 HTTP
클라이언트를 공유한다. 연결 프로필별 설정과 클라이언트 선택 기능을 추가해야
한다.

### MCP 결과가 dict가 아닌 경우

현재 응답 모델은 `structuredContent` dict를 요구한다. 문자열이나 배열을
직접 반환하는 도구라면 GenOS MCP 도구 쪽에서 dict로 감싸는 방식을 권장한다.

예:

```json
{
  "items": []
}
```

### 필수 파라미터 누락 시 프론트 재입력이 필요한 경우

현재 `address`가 없으면 `null`이 MCP로 전달된다. MCP 호출 전에 프론트 입력창을
띄우려면 다음 기능을 별도로 추가해야 한다.

1. manifest에 필수 파라미터 정책 정의
2. MCP 호출 전 파라미터 검증 LangGraph 노드
3. 일반 Redis에 `MCP_PARAMETER_REQUIRED` HITL 상태 저장
4. 같은 `/v1/chat`에 `thread_id`와 `hitl_input`으로 재진입
5. 입력 검증 후 MCP 노드로 분기

---

## 13. 자동 테스트 수정

현재 공통 테스트는 모든 도구가 `test_tool`이라고 가정한다. 첫 운영 도구를
추가하면 이 테스트 기대값을 시나리오별 매핑으로 변경해야 한다.

대상 파일:

```text
tests/test_registered_subagents.py
```

권장 기대값:

```python
expected_tools = {
    ("RP", "APARTMENT_RP_LIST"): (
        "search_apartment_management_fee_rp"
    ),
}
```

테스트 시에는 대상 세부 시나리오가 다음 조건을 만족하는지 확인한다.

1. 정확한 `tool_name`
2. `employee_no.source == "context"`
3. `employee_no.name == "employee_id"`
4. `search_keyword.source == "parameter"`
5. `search_keyword.name == "address"`
6. `request_channel.value == "CHAT"`

추가로 MCP HTTP MockTransport 테스트에서 다음 payload를 검증한다.

```json
{
  "method": "tools/call",
  "params": {
    "name": "search_apartment_management_fee_rp",
    "arguments": {
      "employee_no": "EMP001",
      "search_keyword": "잠실동",
      "request_channel": "CHAT"
    }
  }
}
```

테스트 명령:

```powershell
python -m compileall -q app
$env:LOG_LEVEL="WARNING"
python -m unittest discover -s tests -v
```

모든 테스트가 통과하기 전에는 `active.yaml`을 새 버전으로 변경하지 않는다.

---

## 14. Postman 테스트

### 요청

```http
POST http://localhost:8080/v1/chat
Content-Type: application/json
```

```json
{
  "message": "잠실동에서 관리비 자동납부 가능한 아파트를 알려줘",
  "employee_id": "EMP001",
  "conversation_id": "conversation-rp-001",
  "frontend_agent_code": "RP"
}
```

### 확인 순서

1. HTTP 상태가 `200`
2. `status == "PASS"`
3. `classification.agent_code == "RP"`
4. `subagent.scenario_code == "APARTMENT_MANAGEMENT_FEE_RP_ELIGIBILITY"`
5. `subagent.detail_scenario_code == "APARTMENT_RP_LIST"`
6. `subagent.parameters.address == "잠실동"`
7. `mcp.backend == "http"`
8. `mcp.tool_name == "search_apartment_management_fee_rp"`
9. `mcp.succeeded == true`
10. `mcp.result`가 GenOS 도구의 `structuredContent`와 동일

프론트 에이전트를 선택하지 않는 동작도 확인하려면
`frontend_agent_code`를 생략한다. 이 경우 마스터 분류 결과와 비교하지 않고
분류된 에이전트로 바로 진행한다.

---

## 15. 목업 화면 테스트

서버 실행:

```powershell
uvicorn main:app --env-file .env --host 0.0.0.0 --port 8080
```

브라우저:

```text
http://localhost:8080/tester
```

입력 예:

```text
사원번호: EMP001
conversation_id: conversation-rp-001
프론트 선택 에이전트: RP
질문: 잠실동에서 관리비 자동납부 가능한 아파트를 알려줘
```

화면에서 다음 순서로 결과가 표시돼야 한다.

```text
마스터 분류
→ RP 서브에이전트
→ APARTMENT_MANAGEMENT_FEE_RP_ELIGIBILITY
→ APARTMENT_RP_LIST
→ address=잠실동
→ 사용 MCP 도구
→ MCP arguments
→ structuredContent 원본 dict
```

---

## 16. 로그 확인

정상 호출 시 다음 로그 구간을 순서대로 확인한다.

```text
======== 요청 도착
======== 1차 의도 분류
======== LLM 전달
======== 서브에이전트 실행 시작
======== 서브에이전트 분류 완료
======== MCP Payload 생성
======== MCP HTTP 응답 대기
======== MCP 실행 완료
```

`MCP Payload 생성` 로그에서 확인할 값:

- 도구명
- 추적 ID
- argument 이름
- parameter/context 값 연결

보안상 확인하면 안 되는 값:

- Authorization Bearer Token
- MCP Bearer Token 원문

---

## 17. 주요 오류와 점검 방법

### `mcp.tool_name이 필요합니다`

원인:

- 세부 시나리오에 `mcp`가 없음
- `tool_name`이 비어 있음

조치:

- 모든 `details[]` 항목에 `mcp.tool_name` 추가

### `알 수 없는 파라미터를 참조합니다`

원인:

- `source: parameter`의 `name`이 `parameter_definitions`에 없음

조치:

- 오타 수정 또는 `parameter_definitions` 추가

### GenOS에서 tool not found

원인:

- manifest의 `tool_name`과 GenOS 등록 이름 불일치
- 다른 MCP ID의 도구를 호출

조치:

- GenOS의 정확한 tool name과 MCP ID 재확인

### `structuredContent가 dict 형식이 아닙니다`

원인:

- MCP 도구가 `structuredContent`를 반환하지 않음
- 문자열이나 배열을 직접 반환

조치:

- MCP 도구 반환값을 dict로 통일
- GenOS 원본 SSE 응답 확인

### `mcp.succeeded == false`이고 HTTP 오류가 있음

점검 항목:

- MCP endpoint
- MCP ID
- Bearer Token
- 폐쇄망 방화벽과 DNS
- timeout
- GenOS Gateway 상태

### `search_keyword`가 null

원인:

- 사용자 질문에 주소가 없음
- 프롬프트가 주소를 추출하지 못함
- 잘못된 세부 시나리오가 선택됨

조치:

- `subagent.detail_scenario_code` 확인
- `subagent.parameters.address` 확인
- RP system/scenario 프롬프트 보강
- 필수 파라미터 HITL 구현 검토

---

## 18. 배포 및 롤백 순서

권장 배포 순서:

1. GenOS MCP 도구 배포
2. 도구를 단독 `tools/call`로 검증
3. RP `v2` 프롬프트/manifest 생성
4. manifest 로딩 테스트
5. Pydantic Structured Output 테스트
6. MCP payload MockTransport 테스트
7. 전체 자동 테스트
8. 개발 환경 실제 GenOS 통합 테스트
9. `active.yaml`을 `v2`로 변경
10. 애플리케이션 재시작
11. Postman 테스트
12. 목업 화면 테스트
13. 운영 로그와 추적 ID 확인

문제 발생 시:

1. `active.yaml`을 `v1`으로 복원
2. 애플리케이션 재시작
3. 이전 `test_tool` 또는 이전 운영 도구 호출 확인
4. 실패 요청의 `request_id`로 GenOS Gateway 로그 확인

---

## 19. 최종 체크리스트

### GenOS

- [ ] MCP ID 확인
- [ ] Bearer Token 주입
- [ ] 실제 tool name 확인
- [ ] argument 이름과 자료형 확인
- [ ] `structuredContent` dict 반환 확인

### 프롬프트와 manifest

- [ ] 새 프롬프트 버전 생성
- [ ] `parameter_definitions` 확인
- [ ] 시나리오 Markdown의 추출 규칙 확인
- [ ] 세부 시나리오 코드 확인
- [ ] `mcp.tool_name` 변경
- [ ] arguments source와 name 확인
- [ ] `active.yaml` 변경 전 테스트

### 테스트

- [ ] 프롬프트 로더 통과
- [ ] 동적 Pydantic JSON Schema 통과
- [ ] MCP payload 테스트 통과
- [ ] 전체 테스트 통과
- [ ] 실제 GenOS LLM 호출 성공
- [ ] 실제 GenOS MCP 호출 성공
- [ ] Postman 응답 확인
- [ ] `/tester` 화면 확인
- [ ] 추적 ID 로그 확인

이 체크리스트를 통과하면 해당 세부 시나리오는 공통 테스트 MCP가 아니라
자체 운영 MCP 도구를 사용하는 상태가 된다.
