# Langfuse 4.14.0 관측 운영 가이드

## 1. 적용 기준

이 프로젝트는 `langfuse==4.14.0` Python SDK만 활성화한다. 다른 버전이
설치되면 기동은 계속되지만 Langfuse 전송은 no-op으로 전환되고 서버 로그에
설치 버전 불일치가 기록된다.

관측 계층은 다음과 같다.

```text
acqsc.chat                                      요청 1건의 trace 루트
├─ checkpoint.LANGGRAPH_EXECUTION_STARTED       API 흐름·분기 event
├─ stage.마스터 에이전트 1차 의도분류           @timed 함수 span
│  └─ llm.master.intent_classification          LLM generation
├─ stage.시나리오 서브에이전트 실행             @timed 함수 span
│  └─ llm.subagent.<AGENT_CODE>.classification  LLM generation
├─ stage.MCP 도구 실행                          MCP 처리 span
├─ stage.최종 답변 구성 준비                    답변 처리 span
│  └─ llm.answer.final_generation               LLM generation
└─ checkpoint.SSE_RESPONSE_COMPLETED             최종 SSE 상태 event
```

- 요청 한 번은 trace 한 개다.
- 동일 `session_id`의 여러 요청 trace는 Langfuse session으로 묶인다.
- `@timed`가 적용된 함수는 요청 trace 안에서 자동으로 span이 된다.
- LangChain `ChatOpenAI` 호출은 기존 공통 callback을 통해 generation이 된다.
- SSE 토큰 한 조각마다 관측을 만들지 않는다. 완성 상태와 길이만 기록한다.

## 2. 설치와 설정

```bash
pip install -r requirements.txt
```

`requirements.txt`에는 다음과 같이 정확한 SDK 버전이 고정되어 있다.

```text
langfuse==4.14.0
```

이 프로젝트는 `.env` 파일을 읽지 않는다. 비밀이 아닌 Langfuse 설정은
`app/config.py` 상단에서 관리한다.

```python
DEFAULT_LANGFUSE_ENABLED = True
DEFAULT_LANGFUSE_HOST = "http://langfuse-web:3000"
DEFAULT_LANGFUSE_AUTH_CHECK_ON_STARTUP = False
```

Public Key와 Secret Key만 컨테이너 Secret 환경변수로 등록한다.

```text
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

- `DEFAULT_LANGFUSE_ENABLED`: 실제 관측 전송 활성 여부다.
- `DEFAULT_LANGFUSE_HOST`: 클러스터에서 접근 가능한 Langfuse 주소다.
- `DEFAULT_LANGFUSE_AUTH_CHECK_ON_STARTUP=True`: 기동 시 `auth_check()`를
  호출한다. 네트워크 호출이므로 배포 검증 시에만 켜는 것을 권장한다.
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`: 컨테이너에 직접 등록하는
  Langfuse 프로젝트 Secret이다.

키, Bearer 토큰, 호스트 원문은 애플리케이션 로그에 출력하지 않는다.

## 3. 파일별 동작

### `app/observability.py`

- `configure_langfuse()`: `Settings`가 전달한 주소·키, SDK 4.14.0을 검사하고
  명시적인 `Langfuse(...)` 클라이언트를 한 번 준비한다. 이 모듈은 환경변수를
  직접 읽지 않는다.
- `langfuse_request_trace()`: 실제 SSE generator 안에서 trace 루트를 연다.
- `_langfuse_stage_span()`: 활성 요청 안에서 `@timed` 함수의 span을 연다.
- `record_langfuse_checkpoint()`: API의 주요 분기와 조립 상태를 event로 남긴다.
- `LlmUsageCallbackHandler`: LLM generation의 모델, 성공 여부, 시간, 입력·출력·
  전체 토큰 수를 기록하고 `finally` 성격의 종료 경계에서 항상 닫는다.
- `finish_langfuse_trace()`: 최종 상태, 선택 에이전트·시나리오 코드, 답변 길이,
  전체 시간을 trace 출력 요약으로 기록한다.
- `flush_langfuse()`: FastAPI 종료 시에만 잔여 배치를 전송한다.

### `app/api.py`

- `create_app()`에서 Langfuse를 초기화한다.
- `event_stream()` 안에서 root trace를 연다. 따라서 `asyncio.create_task()`로
  실행되는 LangGraph·답변 작업도 생성 시점의 컨텍스트를 상속한다.
- `trace_checkpoint()`가 주요 SSE·LangGraph 분기를 Langfuse event로 전달한다.
- 정상, INPUT_REQUIRED, BLOCK, STOPPED, ERROR 종료 상태를 trace에 기록한다.
- FastAPI lifespan 종료 시 `flush_langfuse()`를 실행한다.
- `/health`의 `langfuse` 필드에서 활성 여부, 요구 SDK 버전과 비활성 사유를
  확인할 수 있다.

## 4. Langfuse에 저장하는 정보

저장 항목:

- `request_id`, `thread_id`, `session_id`
- 해시 처리한 사용자 식별자
- endpoint, 프론트 선택 agent code, 신규/HITL 재진입 구분
- 함수 단계명, stage code, 파일·함수·시작 줄
- 분류 결과 코드, 시나리오 코드, MCP 성공·무데이터·오류 여부와 건수
- 각 단계와 전체 요청의 소요시간
- LLM 모델명, 입력·출력·전체 토큰 수, 성공·오류 상태
- 최종 상태와 답변 길이

저장하지 않는 항목:

- 사용자 질문 원문과 보정 질문 원문
- 프롬프트와 채팅 이력 원문
- LLM 답변 원문
- MCP payload·조회 데이터 원문
- RAG 검색 문서·chunk 원문
- Authorization, Access Token, API Key
- HITL 입력값과 Redis 상태 원문

복합 값이 실수로 관측 metadata에 들어오더라도 `sanitize_log_value()`를 거쳐
민감 키는 제거되고 질문·답변·문서·payload 계열 필드는 길이·지문 또는 구조
요약으로 치환된다. 이는 기존 로컬 로그 가드레일 정책과 같은 경계다.

## 5. 가드레일과의 관계

기존 Bastion Guardian 정책은 변경하지 않는다.

1. 사용자 질문은 `INPUT` 가드레일을 통과한 뒤 LangGraph로 전달된다.
2. 최종 답변·action·Markdown 표는 `OUTPUT` 가드레일을 통과한 뒤 SSE와 Redis로
   전달된다.
3. OUTPUT 이름 복원 정책은 기존처럼 이름 토큰에만 적용된다.
4. Langfuse에는 위 본문을 저장하지 않고 길이·상태·건수만 저장한다.
5. Python stdout 로그는 `_LogGuardrailFilter`와
   `_ReadableMultilineFormatter`의 최종 정제 경계를 계속 통과한다.

따라서 Langfuse 연결 여부가 질문·답변의 가드레일 판정이나 Redis 저장 내용을
바꾸지 않는다.

## 6. 장애 정책

다음 상황에서는 Langfuse만 비활성 또는 해당 관측만 생략한다.

- 패키지 미설치
- SDK가 4.14.0이 아님
- 호스트 또는 프로젝트 키 누락
- 초기화·auth check 실패
- span/generation/event 생성·업데이트·종료 실패
- 종료 시 flush 실패

관측 오류는 채팅, LangGraph, LLM, MCP, Redis 예외로 전환하지 않는다. 기동 로그의
`Langfuse 활성`, `Langfuse 비활성`, `Langfuse 초기화 실패`와 `/health` 응답으로
원인을 확인한다.

## 7. 운영 확인 순서

1. 컨테이너에서
   `python -c "from importlib.metadata import version; print(version('langfuse'))"`
   결과가 `4.14.0`인지 확인한다.
2. 컨테이너의 두 `LANGFUSE_*_KEY` Secret과 클러스터 DNS 연결을 확인한다.
3. 필요할 때만 `app/config.py`의
   `DEFAULT_LANGFUSE_AUTH_CHECK_ON_STARTUP=True`로 한 번 기동해 인증을 검증한다.
4. `/health`에서 `langfuse.enabled=true`를 확인한다.
5. `/chat` 요청 한 건을 보내 Langfuse UI에 `acqsc.chat` trace가 생성되는지
   확인한다.
6. 동일 `session_id` 요청들이 하나의 session으로 묶이는지 확인한다.
7. trace 안에 `stage.*`, `checkpoint.*`, `llm.*`가 부모-자식으로 표시되는지
   확인한다.
8. LLM generation에 토큰 수가 표시되는지 확인한다. GenOS가 usage를 반환하지
   않으면 애플리케이션 로그에는 `GenOS응답미제공`으로 남는다.
9. 질문·답변·문서·MCP 원문이 Langfuse UI에 저장되지 않았는지 확인한다.
