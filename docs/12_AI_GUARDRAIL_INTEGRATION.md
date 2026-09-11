# AI 가드레일 입력·문장 스트리밍·테이블 처리

## 1. 적용 결과

공통 클라이언트는 [`app/guardrail.py`](../app/guardrail.py)에 있고 HTTP API 연결은
[`app/api.py`](../app/api.py)의 단일 SSE `/chat` 경로에 적용된다.

```text
프론트 INPUT
→ INPUT 가드레일(user)
→ PASS 원문 / MASK processed_content / BLOCK 그래프 미호출
→ 분류·시나리오·MCP·답변 생성
→ OUTPUT 가드레일(assistant)
→ MASK이면 PERSON_NAME_n만 검증된 matched_text로 복원
→ 검사된 token과 Markdown table만 프론트 전송
```

API 키가 없으면 네트워크를 호출하지 않고 모든 입력과 출력을 `PASS`한다. 따라서
현재 개발 환경 동작은 유지된다. 키가 주입되는 순간 같은 코드가 실제 고객사 API를
호출한다.

## 2. 환경변수

```dotenv
AI_GUARDRAIL_BASE_URL=https://bastionguardian-api.startfort.io
AI_GUARDRAIL_ENDPOINT_PATH=/v1/huard/api
AI_GUARDRAIL_API_KEY=
AI_GUARDRAIL_TIMEOUT_SECONDS=15
AI_GUARDRAIL_FAIL_OPEN=false
```

- `AI_GUARDRAIL_API_KEY`가 빈 값이면 명시적 PASS-through다.
- 운영 키는 `.env`나 Git에 저장하지 않고 배포 Secret으로 주입한다.
- 키가 설정된 상태의 API 오류, 타임아웃, 잘못된 응답은 기본적으로 `BLOCK`한다.
- `AI_GUARDRAIL_FAIL_OPEN=true`는 장애 시 원문을 통과시키므로 개발 환경에서만
  제한적으로 사용한다.

`/health`의 `ai_guardrail` 값이 `pass-through`면 키 없음, `enabled`면 실제 호출
상태다.

## 3. 실제 요청 형식

텍스트는 고객사가 제시한 `messages[].content` 배열에 다음 content part로 넣는다.

```json
{
  "messages": [
    {
      "role": "assistant",
      "content": [{"type": "text", "text": "검사할 문장."}]
    }
  ],
  "processType": "OUTPUT",
  "additionalData": {},
  "option": {
    "trace_id": "acqsc:...",
    "session_id": "...",
    "user_id": "...",
    "metadata": {
      "endpoint": "acqsc",
      "surface": "output.sentence[0]"
    },
    "tags": ["acqsc", "acqsc", "output.sentence[0]"]
  }
}
```

요청 헤더는 다음과 같다.

```http
X-Starfort-Guard-Api-Key: <api_key>
Content-Type: application/json
```

고객사 문서에 `content: []` 내부 항목의 상세 스키마가 추가로 확정되면
[`BastionGuardianClient.process_text()`](../app/guardrail.py)의 content part 한 곳만
수정하면 된다. 현재 구현은 일반적인 text part인 `{"type":"text","text":"..."}`를
사용한다.

## 4. INPUT 처리

### `/chat`

- `body.message`를 검사한다.
- HITL 재진입이면 `humanInput[].code`에 등록된 정책에 따라 각 입력을 검사한다.
- `PASS`: 원문을 분류·HITL·MCP로 전달한다.
- `MASK`: 반드시 `processed_content`만 전달한다.
- `BLOCK`: LangGraph, LLM, MCP를 호출하지 않고 고정 차단 답변을 반환한다.

HITL 정책은 Redis에 저장하지 않으며 프론트가 임의의 boolean으로 결정하지도
않는다. [`ScenarioActionInput`](../app/scenario_actions.py)의
`guardrail_enabled`를 서버가 input code별로 등록한다. 기본값은 `True`이고,
등록되지 않은 코드도 항상 검사한다. MCP 조회키처럼 MASK되면 조회할 수 없는
필드만 다음처럼 명시적으로 끈다.

```python
ScenarioActionInput(
    parameter_name="fax_part_1",
    input_code="INPUT_FAX",
    order=1,
    label="지역번호",
    sensitive=True,
    guardrail_enabled=False,
)
```

현재 에이전트 전환 제어값 `SWITCH_AGENT`와 팩스 입력 `INPUT_FAX`는 원문을
유지한다. 다른 Python action도 `guardrail_enabled=False`로 등록한 input code만
같은 방식으로 처리한다. RAG 문서 검색은
사용자 추가입력 Action을 사용하지 않으므로 `search_query` HITL 자체가 없다. 같은
`input_code`를 여러 action에서 공유하면 모든 action에서 같은 정책을 사용해야
한다. 서로 다른 정책이 필요하면 input code 자체를 구분한다. 가드레일을 끄더라도
graph의 대기 중 action code/field 검증은 그대로 수행되므로 임의 입력은 MCP로
바로 전달되지 않는다.

## 5. OUTPUT 온점 단위 스트리밍

전체 AI 답변을 기다렸다가 한 번에 검사하지 않는다. LLM 또는 고정답변 token을
내부 버퍼에 누적하고 `.`이 나타날 때마다 해당 온점까지를 한 문장으로 분리한다.

```text
생성 token: "첫 번째 문"
→ 버퍼 유지, 프론트 전송 안 함

생성 token: "장입니다. 두 번째"
→ "첫 번째 문장입니다." OUTPUT 검사
→ PASS/MASK 결과를 즉시 token SSE로 전송
→ " 두 번째"는 다음 버퍼로 유지

생성 종료
→ 남은 버퍼도 OUTPUT 검사 후 전송
```

연속 온점 `...`은 하나의 경계로 묶는다. 숫자의 소수점과 URL의 온점도 현재는
요청대로 경계로 취급하지만 조각을 순서대로 합치면 원문은 보존된다. 경계 규칙을
바꾸려면 [`split_period_sentences()`](../app/guardrail.py)를 수정한다.

고정 응답, RAG 답변, 조회형 고정 포매터 답변, 오류 fallback도 모두 같은 OUTPUT
검사를 거친다.

### 5.1 OUTPUT 이름 마스크 복원 정책

최종 OUTPUT은 반드시 Bastion Guardian 검사를 먼저 통과한다. 가드레일 호출 전에
이름을 제외하거나, 검사하지 않은 원문을 프론트로 보내는 분기는 없다. 전체 action이
`MASK`이면 `processed_content`를 기준으로 다음 순서만 추가 수행한다.

```text
가드레일 OUTPUT 요청
→ MASK 응답 및 processed_content 수신
→ detected_items에서 복원 허용 항목 수집
→ PERSON_NAME_n 토큰만 matched_text로 복원
→ 전화번호·계좌번호·주민번호 등 나머지 마스크 유지
→ 복원된 OUTPUT을 token/messages SSE로 전달
```

예시는 다음과 같다.

```text
가드레일 processed_content:
내 이름은 [PERSON_NAME_1]이고 연락처는 [PHONE_NUMBER_1]입니다.

detected_items:
PERSON_NAME_1 → 황재식
PHONE_NUMBER_1 → 010-1234-5678

최종 OUTPUT:
내 이름은 황재식이고 연락처는 [PHONE_NUMBER_1]입니다.
```

이름 복원 조건은 모두 만족해야 한다.

- `processType`이 `OUTPUT`이어야 한다. `INPUT`에서는 이름도 계속 마스킹한다.
- 최종 action과 정책 결과 action이 `MASK`여야 한다.
- 정책 유형이 `PII`여야 한다.
- `rule_name`이 이름 규칙이어야 한다.
- `mask_word`가 정확히 `PERSON_NAME_<숫자>` 형식이어야 한다.
- `matched_text`가 비어 있지 않고 줄바꿈·NULL 문자가 없는 100자 이하 문자열이어야
  한다.
- 같은 `mask_word`가 서로 다른 `matched_text`로 중복되면 해당 토큰은 복원하지
  않는다.

복원 대상 이름이나 `matched_text` 원문은 로그에 기록하지 않는다. 로그에는
`이름복원개수`만 기록한다. 정책 코드는
[`restore_output_person_names()`](../app/guardrail.py)에 있으며 허용 토큰 정규식은
같은 파일의 `_PERSON_NAME_MASK_WORD` 한 곳에서 관리한다.

## 6. Markdown 테이블

검사 대상은 `renderables` 중 다음 조건을 모두 만족하는 항목이다.

```text
type == "table"
format == "markdown"
content == 완성된 Markdown 표 문자열
```

Markdown 전체를 assistant `OUTPUT` text part로 검사하고 결과를 다시
`renderable.content`에 넣는다.

- `PASS`: 원본 Markdown과 기존 `data`를 유지한다.
- `MASK`: `content`를 `processed_content`로 교체하고, 원문 `data.rows`가 화면에서
  마스킹을 우회하지 못하도록 `data=null`로 만든다.
- `BLOCK`: 모든 표를 제거하고 전체 답변을 고정 차단 답변으로 교체한다.

표는 별도 신규 SSE 이벤트를 추가하지 않는다. 기존 프론트 계약인 완성
`messages[].metadata.renderables[].content`에 가드레일을 통과한 Markdown만 넣는다.
차트·카드·추천질문·sourceDocuments는 이번 고객사 요청의 OUTPUT 검사 대상에서
제외했다.

## 7. BLOCK과 오류 정책

일반 입력·답변·표가 `BLOCK`이면 원문 또는 일부 결과를 최종 메시지에 넣지 않는다.
대신 다음 고정 문구를 다시 OUTPUT 가드레일에 태운다.

```text
요청하신 내용은 보안 정책에 따라 답변을 제공할 수 없습니다.
```

이 고정 문구가 `PASS` 또는 `MASK`된 결과만 token으로 나누어 스트리밍한다. 고정
문구까지 `BLOCK`되면 임의의 미검사 대체 문구를 보내지 않고 빈 답변으로 종료한다.
SSE의 마지막 `end.data.status`는 `BLOCK`이다.

답변의 앞 문장이 이미 통과되어 전송된 뒤 다음 문장에서 BLOCK될 수 있다. 이 경우
같은 `assistant_message_id`의 빈 완성 메시지를 먼저 보내 기존 표시를 초기화하고,
검사된 고정 차단 문구를 스트리밍한 뒤 최종 `messages`로 교체한다. 프론트는 token
문자열만 영구 저장하지 말고 마지막 `messages` 이벤트를 최종 상태로 사용해야 한다.

## 8. 추적 위치

| 변경 목적 | 코드 위치 |
|---|---|
| BASE URL, 키, 타임아웃, fail-open | [`app/config.py`](../app/config.py) |
| 요청 payload와 헤더 | [`BastionGuardianClient.process_text()`](../app/guardrail.py) |
| PASS/MASK/BLOCK·오류 봉투 파싱 | [`parse_guardrail_response()`](../app/guardrail.py) |
| OUTPUT 이름만 제한적으로 복원 | [`restore_output_person_names()`](../app/guardrail.py) |
| 온점 문장 분리 | [`split_period_sentences()`](../app/guardrail.py) |
| `/chat` INPUT과 문장 OUTPUT | [`stream_chat()`](../app/api.py) |
| HITL input code별 정책 registry | [`app/hitl.py`](../app/hitl.py) |
| 시나리오 입력별 ON/OFF | [`ScenarioActionInput`](../app/scenario_actions.py) |
| Markdown table 검사 | [`_guard_markdown_tables()`](../app/api.py) |
| 테스트 | [`tests/test_guardrail.py`](../tests/test_guardrail.py) |

## 9. 운영 키 적용 전 확인사항

1. 고객사에 `messages[].content[]` text part의 정확한 필드명이 `type/text`인지 확인한다.
2. OUTPUT 응답도 `input_results`를 쓰는지, 별도 `output_results`를 쓰는지 확인한다.
   코드는 두 형태를 모두 허용한다.
3. Markdown을 text part로 보낼지 실제 file part로 보낼지 고객사 스키마를 확인한다.
   현재는 완성된 Markdown 문자열을 text part로 검사한다.
4. `MASK processed_content`가 원문의 공백·Markdown 구문을 보존하는지 확인한다.
5. `guardrail_enabled=False`인 조회키의 허용 범위와 서버 로그 마스킹 정책을 확정한다.
6. 프론트가 BLOCK 시 마지막 `messages` 이벤트로 기존 token 표시를 교체하는지 확인한다.
7. 운영에서는 `AI_GUARDRAIL_FAIL_OPEN=false`를 유지한다.
8. 운영 가드레일의 이름 `mask_word`가 `PERSON_NAME_<숫자>` 형식인지 확인한다.
9. 이름 외 전화번호·계좌번호 등의 토큰이 최종 OUTPUT에서도 계속 마스킹되는지
   혼합 PII 응답으로 검증한다.

## 10. 전체 적용 정책 요약

| 대상 | processType | 기본 동작 | 이름 복원 | Redis 저장 |
|---|---|---|---|---|
| 신규 사용자 질문 | INPUT | PASS/MASK만 그래프 진입, BLOCK은 종료 | 안 함 | 처리된 질문을 기반으로 한 보정 질문 저장 |
| HITL 일반 입력 | INPUT | 등록되지 않은 코드도 기본 검사 | 안 함 | 처리 결과가 재개 상태에 포함될 수 있음 |
| HITL 정확 조회키 | INPUT | `guardrail_enabled=False`이면 원문 유지 | 해당 없음 | 재개에 필요하면 원문 보관 가능 |
| LLM·고정 최종 답변 | OUTPUT | 문장 단위 검사 후 SSE 전송 | 이름만 복원 | 검사·복원된 최종 답변 저장 |
| Markdown 표 | OUTPUT | 표 전체 검사, MASK이면 원본 data 제거 | 이름만 복원 | 검사·복원된 content만 답변 metadata에 저장 |
| Action 안내문 | OUTPUT | 검사 후 action 이벤트 전송 | 이름만 복원 | 내부 HITL 상태의 원본 안내문과 외부 전송값은 구분됨 |
| 로그 | 해당 없음 | 공통 로컬 비식별화 필터 적용 | 안 함 | 원문 대신 길이·건수·상태만 기록 |

`guardrail_enabled=False`는 전화번호·주소처럼 MCP가 정확한 원문으로 조회해야 하는
HITL 필드에만 사용한다. OUTPUT 이름 복원 정책과는 별개이며, OUTPUT 전체가
가드레일을 통과해야 한다는 원칙은 유지된다.
