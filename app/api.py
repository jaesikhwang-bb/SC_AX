"""FastAPI 엔드포인트와 애플리케이션 의존성을 구성하는 모듈."""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)

from app.answers import AnswerService, create_answer_service
from app.classifier import IntentClassifier, create_classifier
from app.config import Settings
from app.csv_trace import TraceRecorder, create_trace_recorder
from app.error_responses import build_safe_error_answer
from app.employee_context import enrich_user_context
from app.graph import MasterIntentGraph, MasterResult
from app.guardrail import (
    GuardrailClient,
    GuardrailContext,
    create_guardrail_client,
    split_period_sentences,
)
from app.history import ChatHistoryStore, create_history_store
from app.hitl import is_hitl_input_guardrail_enabled
from app.hitl_store import (
    HitlStateNotFoundError,
    HitlStateStore,
    create_hitl_state_store,
)
from app.models import StreamingChatRequest, StreamingUser
from app.mcp.client import McpToolExecutor, create_mcp_tool_executor
from app.observability import (
    configure_langfuse,
    configure_logging,
    developer_trace_context,
    error_code_for_exception,
    finish_langfuse_trace,
    flush_langfuse,
    langfuse_request_trace,
    langfuse_status,
    log_context,
    log_failure_diagnostic,
    logger,
    record_langfuse_checkpoint,
    timed,
)
from app.prompt_loader import PromptBundleLoader
from app.recommended_questions import (
    RecommendedQuestionRegistry,
    create_recommended_question_registry,
)
from app.subagents.router import SubagentRouter, create_subagent_router
from app.streaming import build_action_event, encode_sse, split_text


SAFE_INPUT_FALLBACK_MESSAGE = (
    "요청 입력 형식을 확인하지 못했습니다. 필수 입력값과 JSON 형식을 확인한 후 "
    "다시 질문해 주세요."
)
SAFE_PROCESSING_FALLBACK_MESSAGE = (
    "현재 요청을 처리하는 중 일시적인 문제가 발생했습니다. 잠시 후 같은 질문을 "
    "다시 입력해 주세요."
)
SAFE_HITL_FALLBACK_MESSAGE = (
    "이전 추가 입력 상태를 이어갈 수 없습니다. 처음 질문부터 다시 시작해 주세요."
)
SAFE_GUARDRAIL_BLOCK_MESSAGE = (
    "요청하신 내용은 보안 정책에 따라 답변을 제공할 수 없습니다."
)


def _mcp_result_data_count(execution: Any) -> int:
    """MCP 원문을 로그에 남기지 않고 대표 data 항목 개수만 계산한다."""

    for source in (
        getattr(execution, "formatted_result", None),
        getattr(execution, "result", None),
    ):
        if not isinstance(source, dict):
            continue
        data = source.get("data")
        if isinstance(data, (list, tuple, dict)):
            return len(data)
        for key in ("rows", "items", "records", "gridRows"):
            rows = source.get(key)
            if isinstance(rows, (list, tuple, dict)):
                return len(rows)
    return 0


def _debug_trace_requested(value: str | None) -> bool:
    """명시적인 개발 헤더만 내부 함수 추적을 활성화한다."""

    return (value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _recommended_questions_for_result(
    result: MasterResult,
    registry: RecommendedQuestionRegistry,
) -> list[dict[str, Any]]:
    """정상 결과에 해당하는 manifest 추천질문을 반환한다."""

    if result.status == "PASS":
        return registry.for_subagent(result.subagent)
    return []


def create_app(
    *,
    settings: Settings | None = None,
    classifier: IntentClassifier | None = None,
    history_store: ChatHistoryStore | None = None,
    hitl_store: HitlStateStore | None = None,
    subagent_router: SubagentRouter | None = None,
    mcp_executor: McpToolExecutor | None = None,
    trace_recorder: TraceRecorder | None = None,
    answer_service: AnswerService | None = None,
    recommended_question_registry: RecommendedQuestionRegistry | None = None,
    guardrail_client: GuardrailClient | None = None,
) -> FastAPI:
    """설정, 프롬프트, LLM, Redis, 그래프를 조립해 FastAPI 앱을 만든다.

    테스트에서는 가짜 분류기와 메모리 저장소를 주입할 수 있다. 운영 실행에서는
    실제 GenOS 분류기, Redis 대화 이력 저장소, 일반 Redis HITL 저장소가 생성된다.
    LangGraph Checkpointer는 생성하거나 연결하지 않는다.
    """

    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    settings = settings or Settings.from_env()
    # SDK 4.14.0과 LANGFUSE_* 환경변수가 준비된 경우에만 실제 전송을
    # 활성화한다. 초기화 실패는 로깅만 남기고 전체 업무 기능은 계속 동작한다.
    configure_langfuse(
        enabled=settings.langfuse_enabled,
        host=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        auth_check_on_startup=settings.langfuse_auth_check_on_startup,
        project_code=settings.project_code,
    )
    prompt = PromptBundleLoader().load(settings.prompt_version)
    classifier = classifier or create_classifier(settings, prompt)
    history_store = history_store or create_history_store(settings)
    hitl_store = hitl_store or create_hitl_state_store(settings)
    subagent_router = subagent_router or create_subagent_router(settings)
    mcp_executor = mcp_executor or create_mcp_tool_executor(settings)
    answer_service = answer_service or create_answer_service(settings)
    guardrail_client = guardrail_client or create_guardrail_client(settings)
    recommended_question_registry = (
        recommended_question_registry or create_recommended_question_registry()
    )
    trace_recorder = trace_recorder or create_trace_recorder(
        enabled=settings.csv_trace_enabled,
        directory=settings.csv_trace_dir,
        project_code=settings.project_code,
    )

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        """그래프 생성과 두 Redis 연결의 종료 수명주기를 관리한다."""

        # 그래프는 Checkpointer 없이 한 번 컴파일한다. HTTP 요청 사이에 유지할
        # HITL 상태는 그래프 내부가 아니라 일반 Redis 저장소가 직접 관리한다.
        app_instance.state.graph = MasterIntentGraph(
            classifier,
            history_store,
            hitl_store,
            settings.history_limit,
            subagent_router,
            mcp_executor,
            trace_recorder,
        )
        logger.info(
            "FLOW 애플리케이션 시작 | 프로젝트=%s | 프롬프트버전=%s | "
            "이력저장소=%s | HITL저장소=%s | HITL활성=%s | "
            "서브에이전트=%s | MCP=%s | Reranking=%s | "
            "Reranking모델=%s | LLM자동재시도=%d회 | "
            "MCP자동재시도=%d회 | LangGraph노드재시도=0회 | "
            "Checkpointer=사용안함",
            settings.project_code,
            prompt.version,
            settings.history_backend,
            settings.hitl_state_backend,
            hitl_store.enabled,
            subagent_router.registered_codes(),
            settings.mcp_backend,
            settings.reranking_enabled,
            settings.reranking_model,
            settings.llm_max_retries,
            settings.mcp_max_retries,
        )
        try:
            yield
        finally:
            # 두 저장소는 기본적으로 같은 Redis 서버를 사용하지만 연결 풀은
            # 각각 생성되므로 애플리케이션 종료 시 모두 닫아 준다.
            await history_store.aclose()
            await hitl_store.aclose()
            await mcp_executor.aclose()
            await answer_service.aclose()
            await guardrail_client.aclose()
            # 요청마다 flush하면 스트리밍 지연이 생기므로 프로세스 종료 직전에만
            # 백그라운드 전송 큐를 비운다.
            await asyncio.to_thread(flush_langfuse)
            logger.info("======== 애플리케이션 종료")

    app = FastAPI(
        title="Master Agent Chat API",
        version="0.4.0",
        description=(
            "WAS 사용자 정보와 간결한 SSE 출력을 사용하는 마스터·서브에이전트 "
            "및 일반 Redis HITL API"
        ),
        lifespan=lifespan,
    )

    # 연계 프론트의 도메인이 아직 확정되지 않았으므로 모든 Origin을 허용한다.
    # allow_origins=["*"]와 credentials를 조합하면 브라우저가 인증 응답을
    # 거절할 수 있어, 정규식으로 Origin을 반사해 Bearer/쿠키 요청도 허용한다.
    # 운영 도메인이 확정되면 반드시 명시적인 allow_origins 목록으로 제한한다.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse | StreamingResponse:
        """그래프 진입 전 입력 계약 오류도 필드·위치·수정 방법을 기록한다."""

        request_id = request.headers.get("x-request-id", str(uuid4()))
        log_failure_diagnostic(
            stage="FastAPI 요청 본문 검증",
            code_location="app/api.py:request_validation_error_handler",
            exc=exc,
            likely_cause=(
                "필수 필드 누락, 타입·길이·패턴 오류 또는 null 허용 범위 위반"
            ),
            corrective_action=(
                "로그의 validation_errors loc 필드와 app/models.py의 "
                "요청 경로에 연결된 Pydantic 스키마와 비교해 호출 본문을 수정하세요."
            ),
            retry_count=0,
            context={
                "method": request.method,
                "path": request.url.path,
                "validation_errors": exc.errors(),
            },
        )
        missing_authorization = any(
            tuple(error.get("loc", ())) == ("header", "authorization")
            for error in exc.errors()
        )
        if missing_authorization:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "INVALID_AUTHORIZATION",
                        "message": ("Authorization 헤더에 Bearer 토큰이 필요합니다."),
                        "request_id": request_id,
                    }
                },
            )
        if request.url.path == "/chat":
            request_id = f"validation:{uuid4()}"
            session_id = str(uuid4())
            thread_id = str(uuid4())
            user_message_id = str(uuid4())
            assistant_message_id = str(uuid4())
            validation_context = _guardrail_context(
                trace_id=request_id,
                session_id=session_id,
                user_id="validation",
                endpoint=settings.project_code,
                surface="output.validation_fallback",
                project_code=settings.project_code,
            )
            validation_summary = "; ".join(
                (
                    f"{'.'.join(str(item) for item in error.get('loc', ())) or 'body'}: "
                    f"{error.get('msg', '입력값 오류')}"
                )
                for error in exc.errors()
            )
            validation_error_answer = build_safe_error_answer(
                SAFE_INPUT_FALLBACK_MESSAGE,
                error_code="REQUEST_VALIDATION_ERROR",
                error_detail=validation_summary,
                include_details=settings.response_error_details_enabled,
            )
            guarded_fallback = await _guard_output_text(
                validation_error_answer,
                guardrail_client=guardrail_client,
                context=validation_context,
            )
            if guarded_fallback is None:
                guarded_fallback = await _guard_block_message(
                    guardrail_client=guardrail_client,
                    context=validation_context,
                )
            frames = _fallback_sse_frames(
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                user_content="입력 형식을 확인해 주세요.",
                answer=guarded_fallback,
                duration_seconds=0.0,
            )
            if _debug_trace_requested(request.headers.get("x-debug-trace")):
                validation_trace = encode_sse(
                    "trace",
                    {
                        "sequence": 1,
                        "offsetMs": 0.0,
                        "kind": "checkpoint",
                        "stageCode": "REQUEST_VALIDATION_ERROR",
                        "stage": "HTTP 요청 본문 검증 실패",
                        "phase": "FAILED",
                        "source": {
                            "file": "app/models.py",
                            "function": "StreamingChatRequest",
                            "line": None,
                        },
                        "durationMs": 0.0,
                        "details": {
                            "path": request.url.path,
                            "validationErrors": [
                                {
                                    "location": list(error.get("loc", ())),
                                    "type": error.get("type"),
                                    "message": error.get("msg"),
                                }
                                for error in exc.errors()
                            ],
                        },
                        "error": {
                            "code": "REQUEST_VALIDATION_ERROR",
                            "type": type(exc).__name__,
                            "message": "요청 필드 또는 JSON 형식 검증에 실패했습니다.",
                        },
                        "customizationHint": (
                            "표시된 location을 app/models.py의 "
                            "StreamingChatRequest 필드와 비교하세요. input/bytes "
                            "봉투 파싱은 normalize_json_request_body를 확인하세요."
                        ),
                    },
                )
                frames = (*frames[:3], validation_trace, *frames[3:])
            return StreamingResponse(
                iter(frames),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
            )
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                {
                    "error": {
                        "code": "REQUEST_VALIDATION_ERROR",
                        "message": "요청 입력 형식이 올바르지 않습니다.",
                        "request_id": request_id,
                        "details": exc.errors(),
                    }
                }
            ),
        )

    @app.get("/tester", include_in_schema=False)
    @timed("의도분류 테스트 화면")
    async def intent_tester() -> HTMLResponse:
        """별도 metadata API 없이 설정이 주입된 테스트 화면을 반환한다."""

        html_path = (
            Path(__file__).resolve().parents[1] / "static" / "intent_tester.html"
        )
        logger.info(
            "======== 의도분류 테스트 화면 요청 | 파일=%s",
            html_path,
        )
        bootstrap = {
            "prompt_version": prompt.version,
            "agent_codes": list(prompt.agent_codes),
            "chat_history_store": settings.history_backend,
            "hitl_state_store": settings.hitl_state_backend,
            "subagent_codes": list(subagent_router.registered_codes()),
            "mcp_backend": settings.mcp_backend,
        }
        serialized_bootstrap = json.dumps(
            bootstrap,
            ensure_ascii=False,
            separators=(",", ":"),
        ).replace("<", "\\u003c")
        html = html_path.read_text(encoding="utf-8").replace(
            "__SC_AX_TESTER_BOOTSTRAP__",
            serialized_bootstrap,
        )
        return HTMLResponse(html)

    @app.get("/chatting", include_in_schema=False)
    @timed("채팅 전용 화면")
    async def chatting() -> FileResponse:
        """진단 정보 없이 질문과 답변만 제공하는 채팅 화면을 반환한다."""

        html_path = Path(__file__).resolve().parents[1] / "static" / "chatting.html"
        logger.info("======== 채팅 전용 화면 요청 | 파일=%s", html_path)
        return FileResponse(html_path, media_type="text/html")

    @app.get("/health")
    @timed("상태 확인 API")
    async def health() -> dict[str, Any]:
        """서버 상태, 프롬프트 버전, HITL 저장 방식을 반환한다."""

        logger.info("======== 상태 확인 요청")
        return {
            "status": "ok",
            "prompt_version": prompt.version,
            "chat_history_store": settings.history_backend,
            "hitl_state_store": settings.hitl_state_backend,
            "langgraph_checkpointer": "disabled",
            "mcp_backend": settings.mcp_backend,
            "reranking_enabled": str(settings.reranking_enabled).lower(),
            "reranking_model": settings.reranking_model,
            "llm_max_retries": str(settings.llm_max_retries),
            "mcp_max_retries": str(settings.mcp_max_retries),
            "ai_guardrail": (
                "enabled" if guardrail_client.enabled else "pass-through"
            ),
            "response_error_details_enabled": str(
                settings.response_error_details_enabled
            ).lower(),
            "langgraph_node_retries": "0",
            "langfuse": langfuse_status(),
        }

    @app.post("/chat")
    @timed("스트리밍 채팅 요청 접수")
    async def stream_chat(
        body: StreamingChatRequest,
        authorization: str | None = Header(default=None),
        x_debug_trace: str | None = Header(default=None, alias="X-Debug-Trace"),
    ) -> StreamingResponse:
        """최종 WAS 연계 요청을 받아 간결한 SSE 이벤트로 처리한다.

        외부 응답에는 프론트 동작에 필요한 식별자·메시지·답변·action만 보낸다.
        마스터/서브에이전트 분류, 시나리오, 파라미터와 MCP 세부 결과는 모두
        request_id 문맥이 붙은 서버 로그로 추적한다.
        """

        debug_trace_enabled = _debug_trace_requested(x_debug_trace)
        trace_started_at = perf_counter()
        trace_sequence = 0
        trace_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # 입력 검증·인증·INPUT 가드레일은 StreamingResponse 생성 전에 끝난다.
        # 매뉴얼 원칙에 따라 Langfuse 루트는 실제 SSE generator 안에서 열되,
        # 그 이전 checkpoint는 잠시 보관했다가 루트의 첫 자식 event로 기록한다.
        pending_langfuse_checkpoints: list[dict[str, Any]] = []
        langfuse_pipeline_entered = False

        def trace_sink(payload: dict[str, Any]) -> None:
            """현재 요청의 개발 추적 이벤트에 순서와 상대 시간을 붙인다."""

            nonlocal trace_sequence
            if not debug_trace_enabled:
                return
            trace_sequence += 1
            enriched = dict(payload)
            enriched["sequence"] = trace_sequence
            enriched["offsetMs"] = round(
                (perf_counter() - trace_started_at) * 1000,
                3,
            )
            trace_queue.put_nowait(enriched)

        def trace_checkpoint(
            *,
            stage_code: str,
            stage: str,
            file: str,
            function: str,
            phase: str = "COMPLETED",
            details: dict[str, Any] | None = None,
            customization_hint: str | None = None,
            error: BaseException | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "kind": "checkpoint",
                "stageCode": stage_code,
                "stage": stage,
                "phase": phase,
                "source": {"file": file, "function": function, "line": None},
                "durationMs": round(
                    (perf_counter() - trace_started_at) * 1000,
                    3,
                ),
            }
            if details is not None:
                payload["details"] = details
            if customization_hint:
                payload["customizationHint"] = customization_hint
            if error is not None:
                payload["error"] = {
                    "code": error_code_for_exception(error),
                    "type": type(error).__name__,
                    "message": str(error),
                }
            # 개발용 SSE trace 활성 여부와 무관하게, 실제 요청 trace가 열린
            # 구간의 주요 checkpoint는 Langfuse에 event로 기록한다. 원문 계열
            # 필드는 observability의 공통 비식별화 경계를 통과한다.
            if langfuse_pipeline_entered:
                record_langfuse_checkpoint(payload)
            else:
                pending_langfuse_checkpoints.append(dict(payload))
            trace_sink(payload)

        trace_checkpoint(
            stage_code="HTTP_REQUEST_VALIDATED",
            stage="HTTP 요청 본문 정규화 및 Pydantic 검증",
            file="app/models.py",
            function="StreamingChatRequest",
            details={
                "mode": "HITL_RESUME" if body.is_hitl_continuation else "NEW_CHAT",
                "messageLength": len(body.message),
                "humanInputCodes": [item.code for item in (body.human_input or [])],
                "hasSessionId": body.session_id is not None,
                "hasThreadId": body.thread_id is not None,
                "authorizationSource": (
                    "header" if authorization is not None else "body"
                ),
            },
            customization_hint=(
                "입력 필드, 별칭, bytes/input envelope 파싱은 app/models.py의 "
                "StreamingChatRequest와 normalize_json_request_body를 수정하세요."
            ),
        )

        if authorization is not None:
            # 헤더가 존재하면 반드시 표준 Bearer 형식이어야 한다. 잘못된 헤더를
            # Body 토큰으로 우회하지 않아 인증 경로의 모호성을 방지한다.
            if not authorization.casefold().startswith("bearer ") or not (
                authorization[7:].strip()
            ):
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "INVALID_AUTHORIZATION",
                        "message": "Authorization 헤더의 Bearer 토큰이 올바르지 않습니다.",
                    },
                )
            access_token = authorization[7:].strip()
            authorization_source = "header"
        else:
            # Body에는 원본 토큰과 'Bearer 토큰' 형식을 모두 허용한다. 이후
            # request_context에는 접두사를 제거한 실제 토큰만 전달한다.
            body_access_token = body.access_token or ""
            if body_access_token.casefold().startswith("bearer "):
                body_access_token = body_access_token[7:].strip()
            if not body_access_token:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "INVALID_AUTHORIZATION",
                        "message": (
                            "Authorization 헤더 또는 Body access_token이 필요합니다."
                        ),
                    },
                )
            access_token = body_access_token
            authorization_source = "body"
        trace_checkpoint(
            stage_code="AUTHORIZATION_VALIDATED",
            stage="Access Token 입력 검증",
            file="app/api.py",
            function="create_app.stream_chat",
            details={
                "source": authorization_source,
                "scheme": "Bearer" if authorization_source == "header" else "Body",
                "tokenPresent": True,
            },
            customization_hint=(
                "Authorization/Body access_token 우선순위는 app/api.py의 "
                "stream_chat 시작 부분을 수정하세요. 토큰 원문은 추적 화면에 "
                "표시하지 않습니다."
            ),
        )

        # 명세상 null을 허용하는 식별자는 서버에서 안전하게 보완한 뒤 그 값을
        # SSE로 즉시 반환한다. 이후 같은 응답 안에서는 보완된 값만 사용한다.
        session_id = body.session_id or str(uuid4())
        thread_id = body.thread_id or str(uuid4())
        # endpoint는 프론트가 전달한 서비스 별칭을 그대로 사용한다. 비어 있거나
        # null인 경우에만 PROJECT_CODE를 기본값으로 적용하며 허용 목록으로 제한하지
        # 않는다. 이 값은 요청 context와 Redis namespace에도 동일하게 전달된다.
        normalized_endpoint = str(
            body.endpoint or settings.project_code
        ).strip() or settings.project_code
        normalized_agent = (
            body.agent_code.upper() if body.agent_code is not None else None
        )
        if normalized_agent is not None and normalized_agent not in prompt.agent_codes:
            logger.warning(
                "!!!!!!!! 스트리밍 미등록 agent_code 무시 | 입력=%s | "
                "마스터자동분류=사용",
                normalized_agent,
            )
            normalized_agent = None

        trace_checkpoint(
            stage_code="REQUEST_CONTEXT_NORMALIZED",
            stage="요청 식별자·endpoint·에이전트 정규화",
            file="app/api.py",
            function="create_app.stream_chat",
            details={
                "sessionId": session_id,
                "threadId": thread_id,
                "endpoint": normalized_endpoint,
                "frontendAgentCode": normalized_agent,
            },
            customization_hint=(
                "ID 자동 생성, endpoint와 agent_code 보정 규칙은 app/api.py의 "
                "stream_chat 정규화 구간을 수정하세요."
            ),
        )
        request_id = f"{normalized_endpoint}:{uuid4()}"
        employee_id = _resolve_employee_id(body.user, session_id)
        rp_apartment_pending: dict[str, str] | None = None

        # RP 아파트 주소 입력 안내 뒤의 일반 message는 humanInput이 아니므로
        # 평소처럼 의도분류를 수행한다. 단, 직전 action에서 고른 구/신주소 값은
        # 같은 thread의 Redis HITL 상태에서 한 번만 꺼내 request_context로 넘긴다.
        # 주소 메시지가 다른 업무로 분류돼도 기존 대기 상태가 남지 않도록 여기서
        # 삭제하고, 해당 요청은 새 thread_id로 이어 간다.
        if not body.is_hitl_continuation and body.thread_id:
            pending_entry = await hitl_store.get(
                body.thread_id,
                project_code=normalized_endpoint,
            )
            pending_context = (
                dict(pending_entry.interrupt.get("context", {}))
                if pending_entry is not None
                and isinstance(pending_entry.interrupt, dict)
                else {}
            )
            if (
                pending_entry is not None
                and pending_entry.graph_state.get("employee_id") == employee_id
                and pending_entry.graph_state.get("session_id") == session_id
                and pending_context.get("await_message_input") is True
                and str(pending_context.get("apartment_address_type", "")) in {"1", "2"}
            ):
                rp_apartment_pending = {
                    "address_type": str(pending_context["apartment_address_type"]),
                }
                await hitl_store.delete(body.thread_id, project_code=normalized_endpoint)
                previous_thread_id = thread_id
                thread_id = str(uuid4())
                logger.info(
                    "======== RP 아파트 주소 일반입력 전환 | 이전thread=%s | "
                    "새thread=%s | 주소구분=%s | 이전HITL삭제=예",
                    previous_thread_id,
                    thread_id,
                    pending_context["apartment_address_type"],
                )
        # MCP에서 필요한 요청 정보를 한 객체로 전달한다. access_token은 그래프
        # 실행 중에만 사용하고 Redis HITL/대화이력에는 저장하지 않는다.
        request_context = {
            "access_token": access_token,
            "endpoint": normalized_endpoint,
            "recruitment_org_type_code": (
                _derive_recruitment_org_type_code(
                    body.user.id if body.user is not None else None
                )
            ),
            "user": (
                body.user.model_dump(mode="json")
                if body.user is not None
                else {
                    "id": None,
                    "name": None,
                    "deptcode": None,
                    "deptname": None,
                }
            ),
        }
        if rp_apartment_pending is not None:
            request_context["rp_apartment_pending"] = rp_apartment_pending

        # 실제 사번이 전달되면 본 업무 분류 전에 직원정보를 한 번 조회한다.
        # 조회 결과의 이름은 이 HTTP 요청 안에서만 유지되는 user.name에 넣는다.
        # AccessToken을 포함한 request_context는 Redis 대화이력/HITL에 저장하지 않는다.
        employee_lookup_started_at = perf_counter()
        with log_context(
            request_id=request_id,
            session_id=session_id,
            thread_id=thread_id,
            user_id=employee_id,
        ):
            request_context["user"] = await enrich_user_context(
                user=request_context["user"],
                employee_id=employee_id,
                session_id=session_id,
                thread_id=thread_id,
                request_context=request_context,
                executor=mcp_executor,
                settings=settings,
            )
        trace_checkpoint(
            stage_code="EMPLOYEE_CONTEXT_LOOKUP_COMPLETED",
            stage="사번 기준 직원정보 MCP 선조회",
            file="app/employee_context.py",
            function="enrich_user_context",
            details={
                "enabled": settings.employee_lookup_enabled,
                "employeeIdPresent": bool(
                    body.user is not None and body.user.id
                ),
                "employeeNameResolved": bool(
                    request_context["user"].get("name")
                ),
                "elapsedSeconds": round(
                    perf_counter() - employee_lookup_started_at,
                    3,
                ),
            },
            customization_hint=(
                "직원정보 MCP 도구·payload·이름 파싱은 app/employee_context.py와 "
                "app/config.py의 EMPLOYEE_LOOKUP_* 설정을 수정하세요."
            ),
        )

        def stream_guard_context(surface: str) -> GuardrailContext:
            return _guardrail_context(
                trace_id=request_id,
                session_id=session_id,
                user_id=employee_id,
                endpoint=normalized_endpoint,
                surface=surface,
                project_code=settings.project_code,
            )

        input_decision = await guardrail_client.process_text(
            body.message,
            role="user",
            process_type="INPUT",
            context=stream_guard_context("input.message"),
        )
        input_blocked = not input_decision.allowed
        guarded_message = input_decision.processed_content or ""
        trace_checkpoint(
            stage_code="INPUT_GUARDRAIL_COMPLETED",
            stage="사용자 메시지 INPUT 가드레일",
            file="app/guardrail.py",
            function="BastionGuardianClient.process_text",
            details={
                "enabled": guardrail_client.enabled,
                "action": input_decision.action,
                "allowed": input_decision.allowed,
                "originalLength": len(body.message),
                "processedLength": len(guarded_message),
            },
            customization_hint=(
                "가드레일 요청·응답 파싱과 PASS/MASK/BLOCK 정책은 "
                "app/guardrail.py를 수정하세요."
            ),
        )
        guarded_hitl_value: dict[str, Any] = {}
        if not input_blocked and body.is_hitl_continuation:
            try:
                guarded_hitl_value = await _guard_hitl_input_values(
                    body.to_hitl_value(),
                    guardrail_client=guardrail_client,
                    context_factory=stream_guard_context,
                )
            except ValueError as exc:
                if str(exc) != "GUARDRAIL_INPUT_BLOCK":
                    raise
                input_blocked = True

        user_message_id = str(uuid4())
        assistant_message_id = str(uuid4())
        with log_context(
            request_id=request_id,
            session_id=session_id,
            thread_id=thread_id,
            user_id=employee_id,
        ):
            logger.info(
                "FLOW 요청 도착 | 유형=%s | endpoint=%s | "
                "선택에이전트=%s | 원본질문(가드레일처리후)=%s | "
                "인증=확인완료 | 인증입력=%s",
                "HITL재진입" if body.is_hitl_continuation else "신규질문",
                normalized_endpoint,
                normalized_agent,
                guarded_message,
                authorization_source,
            )
            logger.info(
                "======== 사용자 문맥 확인 | 사용자식별자=로그컨텍스트해시사용 | "
                "유치조직구분코드=%s | 익명내부식별자=%s",
                request_context["recruitment_org_type_code"],
                body.user is None or body.user.id is None,
            )
            logger.info(
                "======== 사용자 질문 INPUT 가드레일 통과 | action=%s | "
                "허용=%s | 원문길이=%d | 처리길이=%d | 본문로그=생략",
                input_decision.action,
                input_decision.allowed,
                len(body.message),
                len(guarded_message),
            )
            logger.info(
                "======== 사용자 추가입력 | 개수=%d | 코드=%s",
                len(body.human_input or []),
                [item.code for item in (body.human_input or [])],
            )

        def drain_trace_frames() -> list[str]:
            """현재까지 쌓인 개발 추적 레코드를 SSE 프레임으로 비운다."""

            frames: list[str] = []
            if not debug_trace_enabled:
                return frames
            while not trace_queue.empty():
                frames.append(encode_sse("trace", trace_queue.get_nowait()))
            return frames

        async def stream_task_trace_frames(task: asyncio.Task[Any]):
            """긴 그래프/LLM 작업 중 생성되는 함수 추적을 실시간 전송한다."""

            if not debug_trace_enabled:
                return
            while not task.done():
                try:
                    record = await asyncio.wait_for(
                        trace_queue.get(),
                        timeout=0.1,
                    )
                except TimeoutError:
                    continue
                yield encode_sse("trace", record)
            for frame in drain_trace_frames():
                yield frame

        async def event_stream():
            nonlocal langfuse_pipeline_entered
            with developer_trace_context(
                trace_sink if debug_trace_enabled else None
            ), log_context(
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                user_id=employee_id,
            ), langfuse_request_trace(
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                user_id=employee_id,
                endpoint=normalized_endpoint,
                frontend_agent_code=normalized_agent,
                is_hitl_continuation=body.is_hitl_continuation,
                input_guardrail_action=input_decision.action,
                input_length=len(guarded_message),
            ) as langfuse_trace:
                langfuse_pipeline_entered = True
                for pending_checkpoint in pending_langfuse_checkpoints:
                    record_langfuse_checkpoint(pending_checkpoint)
                pending_langfuse_checkpoints.clear()
                started_at = perf_counter()
                logger.info("======== SSE 시작 | 이벤트=request_id")
                yield encode_sse("request_id", request_id)
                logger.info("======== SSE 식별자 전달 | 이벤트=session_id")
                yield encode_sse("session_id", session_id)
                logger.info("======== SSE 식별자 전달 | 이벤트=thread_id")
                yield encode_sse("thread_id", thread_id)
                for trace_frame in drain_trace_frames():
                    yield trace_frame

                initial_content: Any = guarded_message if not input_blocked else None
                if body.is_hitl_continuation:
                        initial_content = {
                            "message": guarded_message if not input_blocked else None,
                            "humanInput": [
                                {
                                    "code": item.code,
                                    "input": "***SUBMITTED***",
                                    **(
                                        {"order": item.order}
                                        if item.order is not None
                                        else {}
                                    ),
                                }
                                for item in (body.human_input or [])
                            ],
                        }
                initial_messages = [
                    {
                        "role": "user",
                        "id": user_message_id,
                        "content": initial_content,
                    },
                    {
                        "role": "assistant",
                        "id": assistant_message_id,
                        "parentMessageId": user_message_id,
                        "content": "",
                        "metadata": {
                            "renderables": [],
                            "recommendedQuestions": [],
                        },
                    },
                ]
                logger.info(
                    "======== SSE 메시지 트리 전달 | 이벤트=messages | 메시지개수=%d",
                    len(initial_messages),
                )
                yield encode_sse("messages", initial_messages)

                if input_blocked:
                    block_answer = await _guard_block_message(
                        guardrail_client=guardrail_client,
                        context=stream_guard_context("output.block_message"),
                    )
                    trace_checkpoint(
                        stage_code="INPUT_BLOCK_RESPONSE_READY",
                        stage="INPUT BLOCK 고정답변 준비 완료",
                        file="app/api.py",
                        function="_guard_block_message",
                        details={"responseStatus": "BLOCK"},
                    )
                    for trace_frame in drain_trace_frames():
                        yield trace_frame
                    for chunk in split_text(block_answer):
                        yield encode_sse("token", chunk)
                    completed = [
                        initial_messages[0],
                        {
                            "role": "assistant",
                            "id": assistant_message_id,
                            "parentMessageId": user_message_id,
                            "content": block_answer,
                            "metadata": {
                                "renderables": [],
                                "recommendedQuestions": [],
                            },
                        },
                    ]
                    yield encode_sse("messages", completed)
                    duration = round(perf_counter() - started_at, 3)
                    finish_langfuse_trace(
                        langfuse_trace,
                        status="BLOCK",
                        duration_seconds=duration,
                        answer_length=len(block_answer),
                    )
                    yield encode_sse("duration", {"seconds": duration})
                    yield encode_sse("end", {"status": "BLOCK"})
                    return

                current_stage = (
                    "LANGGRAPH_HITL_RESUME"
                    if (body.is_hitl_continuation)
                    else "LANGGRAPH_NEW_CHAT"
                )
                answer_delivered = False
                hitl_pending = False
                full_answer = ""
                result: MasterResult | None = None
                try:
                    graph: MasterIntentGraph = app.state.graph
                    if body.is_hitl_continuation:
                        logger.info(
                            "======== LangGraph 호출 | 모드=HITL재진입 | 입력코드=%s",
                            sorted(body.to_hitl_value()),
                        )
                        graph_call = graph.resume(
                            thread_id=thread_id,
                            value=guarded_hitl_value,
                            expected_employee_id=employee_id,
                            expected_session_id=session_id,
                            request_context=request_context,
                        )
                    else:
                        logger.info(
                            "======== LangGraph 호출 | 모드=신규질문 | "
                            "프론트에이전트=%s",
                            normalized_agent,
                        )
                        graph_call = graph.start(
                            thread_id=thread_id,
                            employee_id=employee_id,
                            session_id=session_id,
                            message=guarded_message,
                            frontend_agent_code=normalized_agent,
                            request_context=request_context,
                        )

                    trace_checkpoint(
                        stage_code="LANGGRAPH_EXECUTION_STARTED",
                        stage="LangGraph 업무 처리 시작",
                        file="app/graph.py",
                        function=(
                            "MasterIntentGraph.resume"
                            if body.is_hitl_continuation
                            else "MasterIntentGraph.start"
                        ),
                        phase="STARTED",
                        details={
                            "entryMode": (
                                "HITL_RESUME"
                                if body.is_hitl_continuation
                                else "NEW_CHAT"
                            ),
                            "threadId": thread_id,
                        },
                        customization_hint=(
                            "전체 노드 연결과 분기 조건은 app/graph.py의 "
                            "MasterIntentGraph.__init__에서 수정하세요."
                        ),
                    )
                    graph_task = asyncio.create_task(graph_call)
                    async for trace_frame in stream_task_trace_frames(graph_task):
                        yield trace_frame
                    result = await graph_task

                    # 프론트 미선택 자동분류, 기존 선택과 일치, SWITCH_AGENT=yes
                    # 완료 시 최종 에이전트 화면으로 이동할 수 있도록 운영 SSE로
                    # 전달한다. 불일치 확인 대기 중과 no 취소 응답에서는 화면을
                    # 먼저 바꾸거나 잘못 바꾸지 않도록 전송하지 않는다.
                    is_agent_mismatch_waiting = (
                        result.status == "INPUT_REQUIRED"
                        and isinstance(result.interrupt, dict)
                        and result.interrupt.get("type") == "AGENT_CODE_MISMATCH"
                    )
                    if (
                        result.classification.agent_code
                        and not is_agent_mismatch_waiting
                        and not result.agent_switch_cancelled
                    ):
                        logger.info(
                            "======== 최종 에이전트 코드 전달 | "
                            "이벤트=agent_code | 에이전트=%s",
                            result.classification.agent_code,
                        )
                        yield encode_sse(
                            "agent_code",
                            result.classification.agent_code,
                        )

                    trace_checkpoint(
                        stage_code="LANGGRAPH_RESULT_READY",
                        stage="LangGraph 결과 변환 완료",
                        file="app/graph.py",
                        function="MasterIntentGraph._to_result",
                        details={
                            "status": result.status,
                            "classificationType": (
                                result.classification.classification_type.value
                            ),
                            "agentCode": result.classification.agent_code,
                            "scenarioCount": (
                                len(result.subagent.matches)
                                if result.subagent is not None
                                else 0
                            ),
                            "detailScenarioCodes": (
                                [
                                    match.detail_scenario_code
                                    for match in result.subagent.matches
                                ]
                                if result.subagent is not None
                                else []
                            ),
                            "mcpWorkflowResults": [
                                {
                                    "stepCode": item.workflow_step_code,
                                    "executionMode": item.workflow_execution_mode,
                                    "isAggregate": item.workflow_is_aggregate,
                                    "handlerCode": item.workflow_handler_code,
                                    "toolName": item.tool_name,
                                    "requestId": item.request_id,
                                    "succeeded": item.succeeded,
                                    "outcome": item.outcome,
                                    "dataCount": _mcp_result_data_count(item),
                                }
                                for item in (result.mcp_workflow_results or [])
                            ],
                            "mcpResults": [
                                {
                                    "toolName": item.tool_name,
                                    "requestId": item.request_id,
                                    "succeeded": item.succeeded,
                                    "outcome": item.outcome,
                                    "resultFormat": item.result_format,
                                    "dataCount": _mcp_result_data_count(item),
                                }
                                for item in (result.mcp_results or [])
                            ],
                        },
                        customization_hint=(
                            "마스터 분류는 app/classifier.py, 시나리오는 "
                            "app/subagents/router.py, MCP 업무 호출은 "
                            "app/mcp/scenarios의 handler 함수, 공통 전송은 "
                            "app/mcp/client.py를 수정하세요."
                        ),
                    )
                    for trace_frame in drain_trace_frames():
                        yield trace_frame

                    logger.info(
                        "======== 마스터 분류 결과 | 상태=%s | 분류유형=%s | "
                        "에이전트=%s | 보정질문길이=%d | 보정질문로그=생략",
                        result.status,
                        result.classification.classification_type.value,
                        result.classification.agent_code,
                        len(result.classification.refined_query),
                    )
                    logger.info(
                        "======== 서브에이전트 분류 결과 | 에이전트=%s | "
                        "매칭개수=%d | 세부시나리오=%s | 파라미터로그=생략",
                        result.subagent.agent_code if result.subagent else None,
                        len(result.subagent.matches) if result.subagent else 0,
                        (
                            [
                                match.detail_scenario_code
                                for match in result.subagent.matches
                            ]
                            if result.subagent
                            else []
                        ),
                    )
                    logger.info(
                        "======== MCP 내부 결과 준비 완료 | 개수=%d | "
                        "결과형식=dict배열 | 프론트MCP결과전송=안함 | "
                        "사용처=최종답변생성 | 성공개수=%d | 무데이터개수=%d | "
                        "오류개수=%d | 조회내용로그=생략",
                        len(result.mcp_results or []),
                        sum(
                            item.succeeded and item.outcome == "SUCCESS"
                            for item in (result.mcp_results or [])
                        ),
                        sum(
                            item.outcome == "NO_DATA"
                            for item in (result.mcp_results or [])
                        ),
                        sum(
                            not item.succeeded or item.outcome == "ERROR"
                            for item in (result.mcp_results or [])
                        ),
                    )

                    response_status = result.status
                    if result.status == "INPUT_REQUIRED":
                        current_stage = "SSE_ACTION"
                        actions = build_action_event(thread_id, result.interrupt)
                        action_message = (
                            actions[0].get("message") if actions else ""
                        )
                        guarded_action_message = await _guard_output_text(
                            action_message if isinstance(action_message, str) else "",
                            guardrail_client=guardrail_client,
                            context=stream_guard_context("output.action"),
                        )
                        trace_checkpoint(
                            stage_code="ACTION_GUARDRAIL_COMPLETED",
                            stage="HITL action 안내문 OUTPUT 가드레일 완료",
                            file="app/api.py",
                            function="_guard_output_text",
                            details={
                                "actionCode": (
                                    actions[0].get("code") if actions else None
                                ),
                                "allowed": guarded_action_message is not None,
                                "inputCount": len(actions),
                            },
                            customization_hint=(
                                "action 외부 형식은 app/streaming.py의 "
                                "build_action_event, 입력 검증은 app/hitl.py를 "
                                "수정하세요."
                            ),
                        )
                        for trace_frame in drain_trace_frames():
                            yield trace_frame
                        if guarded_action_message is None:
                            response_status = "BLOCK"
                            fixed_answer = await _guard_block_message(
                                guardrail_client=guardrail_client,
                                context=stream_guard_context("output.block_message"),
                            )
                            for chunk in split_text(fixed_answer):
                                yield encode_sse("token", chunk)
                            yield encode_sse(
                                "messages",
                                [
                                    initial_messages[0],
                                    {
                                        "role": "assistant",
                                        "id": assistant_message_id,
                                        "parentMessageId": user_message_id,
                                        "content": fixed_answer,
                                        "metadata": {
                                            "renderables": [],
                                            "recommendedQuestions": [],
                                        },
                                    },
                                ],
                            )
                            answer_delivered = True
                        else:
                            for action in actions:
                                action["message"] = guarded_action_message
                            logger.info(
                                "======== 사용자 입력 요청 | 이벤트=action | "
                                "action코드=%s | 입력필드개수=%d | "
                                "안내문길이=%d | 안내문로그=생략",
                                actions[0].get("code") if actions else None,
                                len(actions),
                                len(guarded_action_message),
                            )
                            # 주소 입력 대기는 채팅 말풍선에도 안내를 표시한다.
                            # action은 같은 thread의 다음 일반 message를 연결한다.
                            if actions and actions[0].get("inputMode") == "message":
                                for chunk in split_text(guarded_action_message):
                                    yield encode_sse("token", chunk)
                            yield encode_sse("action", actions)
                            hitl_pending = bool(actions)
                    else:
                        current_stage = "ANSWER_PREPARE"
                        trace_checkpoint(
                            stage_code="ANSWER_PREPARATION_STARTED",
                            stage="최종 답변·표·출처 구성 시작",
                            file="app/answers.py",
                            function="DefaultAnswerService.prepare",
                            phase="STARTED",
                            details={"graphStatus": result.status},
                            customization_hint=(
                                "고정답변/RAG 선택과 MCP 결과의 화면 데이터 변환은 "
                                "app/answers.py의 DefaultAnswerService.prepare를 "
                                "수정하세요."
                            ),
                        )
                        prepare_task = asyncio.create_task(
                            answer_service.prepare(result)
                        )
                        async for trace_frame in stream_task_trace_frames(
                            prepare_task
                        ):
                            yield trace_frame
                        prepared = await prepare_task
                        trace_checkpoint(
                            stage_code="ANSWER_PREPARATION_COMPLETED",
                            stage="최종 답변·표·출처 구성 완료",
                            file="app/answers.py",
                            function="DefaultAnswerService.prepare",
                            details={
                                "mode": prepared.mode,
                                "sourceDocumentCount": len(
                                    prepared.source_documents
                                ),
                                "renderableCount": len(prepared.renderables),
                                "renderableTypes": [
                                    item.get("type")
                                    for item in prepared.renderables
                                    if isinstance(item, dict)
                                ],
                            },
                            customization_hint=(
                                "본문 형식은 app/answers.py, 표/카드 구조는 "
                                "app/renderables.py와 app/mcp/result_adapters.py를 "
                                "확인하세요."
                            ),
                        )
                        for trace_frame in drain_trace_frames():
                            yield trace_frame
                        logger.info(
                            "======== 최종 답변 생성 시작 | 모드=%s | 출처문서개수=%d",
                            prepared.mode,
                            len(prepared.source_documents),
                        )
                        guarded_renderables, table_blocked = (
                            await _guard_markdown_tables(
                                prepared.renderables,
                                guardrail_client=guardrail_client,
                                context_factory=stream_guard_context,
                            )
                        )
                        trace_checkpoint(
                            stage_code="TABLE_GUARDRAIL_COMPLETED",
                            stage="표 renderable OUTPUT 가드레일 완료",
                            file="app/api.py",
                            function="_guard_markdown_tables",
                            details={
                                "inputCount": len(prepared.renderables),
                                "outputCount": len(guarded_renderables),
                                "blocked": table_blocked,
                            },
                            customization_hint=(
                                "표를 Markdown으로 검사하고 구조화 data를 유지하는 "
                                "정책은 app/api.py의 _guard_markdown_tables를 "
                                "수정하세요."
                            ),
                        )
                        for trace_frame in drain_trace_frames():
                            yield trace_frame
                        answer_parts: list[str] = []
                        output_blocked = table_blocked
                        if prepared.source_documents and not output_blocked:
                            trace_checkpoint(
                                stage_code="SOURCE_DOCUMENTS_READY",
                                stage="RAG 출처 문서 SSE 출력 준비",
                                file="app/answers.py",
                                function="DefaultAnswerService.prepare",
                                details={
                                    "count": len(prepared.source_documents),
                                    "nextEvent": "sourceDocuments",
                                },
                                customization_hint=(
                                    "출처 문서 형식은 app/answers.py의 "
                                    "_build_source_documents 계열 함수를 수정하세요."
                                ),
                            )
                            for trace_frame in drain_trace_frames():
                                yield trace_frame
                            logger.info(
                                "======== RAG 출처 문서 단건 전달 | "
                                "이벤트=sourceDocuments | 전송방식=JSON배열1회 | "
                                "MCP원본스트리밍=아님 | 개수=%d | "
                                "문서내용로그=생략",
                                len(prepared.source_documents),
                            )
                            yield encode_sse(
                                "sourceDocuments",
                                prepared.source_documents,
                            )

                        current_stage = "ANSWER_STREAM"
                        token_count = 0
                        sentence_buffer = ""
                        if not output_blocked:
                            async for token in prepared.tokens:
                                if token is None:
                                    continue
                                normalized_token = (
                                    token if isinstance(token, str) else str(token)
                                )
                                if not normalized_token:
                                    continue
                                sentence_buffer += normalized_token
                                sentences, sentence_buffer = split_period_sentences(
                                    sentence_buffer
                                )
                                for sentence_index, sentence in enumerate(sentences):
                                    guarded_sentence = await _guard_output_text(
                                        sentence,
                                        guardrail_client=guardrail_client,
                                        context=stream_guard_context(
                                            f"output.sentence[{token_count + sentence_index}]"
                                        ),
                                    )
                                    if guarded_sentence is None:
                                        output_blocked = True
                                        break
                                    answer_parts.append(guarded_sentence)
                                    token_count += 1
                                    trace_checkpoint(
                                        stage_code="OUTPUT_SENTENCE_APPROVED",
                                        stage="문장 단위 OUTPUT 가드레일 통과",
                                        file="app/api.py",
                                        function="_guard_output_text",
                                        details={
                                            "sentenceIndex": token_count,
                                            "outputLength": len(guarded_sentence),
                                            "nextEvent": "token",
                                        },
                                        customization_hint=(
                                            "온점 기준 버퍼링과 문장별 가드레일은 "
                                            "app/api.py의 ANSWER_STREAM 구간 및 "
                                            "app/guardrail.py를 수정하세요."
                                        ),
                                    )
                                    for trace_frame in drain_trace_frames():
                                        yield trace_frame
                                    yield encode_sse("token", guarded_sentence)
                                if output_blocked:
                                    break

                        if not output_blocked and sentence_buffer:
                            guarded_tail = await _guard_output_text(
                                sentence_buffer,
                                guardrail_client=guardrail_client,
                                context=stream_guard_context("output.sentence.final"),
                            )
                            if guarded_tail is None:
                                output_blocked = True
                            else:
                                answer_parts.append(guarded_tail)
                                token_count += 1
                                trace_checkpoint(
                                    stage_code="OUTPUT_FINAL_TAIL_APPROVED",
                                    stage="마지막 미완결 문장 OUTPUT 가드레일 통과",
                                    file="app/api.py",
                                    function="_guard_output_text",
                                    details={
                                        "sentenceIndex": token_count,
                                        "outputLength": len(guarded_tail),
                                        "nextEvent": "token",
                                    },
                                )
                                for trace_frame in drain_trace_frames():
                                    yield trace_frame
                                yield encode_sse("token", guarded_tail)

                        if not output_blocked and not "".join(answer_parts).strip():
                            guarded_empty_answer = await _guard_output_text(
                                "조회 결과가 없습니다. 잠시 후 다시 시도해 주세요.",
                                guardrail_client=guardrail_client,
                                context=stream_guard_context("output.empty_fallback"),
                            )
                            if guarded_empty_answer is None:
                                output_blocked = True
                            else:
                                answer_parts.append(guarded_empty_answer)
                                token_count += 1
                                for trace_frame in drain_trace_frames():
                                    yield trace_frame
                                yield encode_sse("token", guarded_empty_answer)

                        if output_blocked:
                            response_status = "BLOCK"
                            guarded_renderables = []
                            # 앞 문장이 이미 token으로 전달됐더라도 같은 메시지 ID의
                            # 완성 메시지로 빈 본문을 먼저 보내 프론트 표시를 초기화한다.
                            yield encode_sse(
                                "messages",
                                [
                                    initial_messages[0],
                                    {
                                        "role": "assistant",
                                        "id": assistant_message_id,
                                        "parentMessageId": user_message_id,
                                        "content": "",
                                        "metadata": {
                                            "renderables": [],
                                            "recommendedQuestions": [],
                                        },
                                    },
                                ],
                            )
                            full_answer = await _guard_block_message(
                                guardrail_client=guardrail_client,
                                context=stream_guard_context("output.block_message"),
                            )
                            for trace_frame in drain_trace_frames():
                                yield trace_frame
                            for chunk in split_text(full_answer):
                                token_count += 1
                                yield encode_sse("token", chunk)
                            recommended_questions = []
                        else:
                            full_answer = "".join(answer_parts).strip()
                            try:
                                recommended_questions = (
                                    _recommended_questions_for_result(
                                        result,
                                        recommended_question_registry,
                                    )
                                )
                            except Exception as exc:
                                trace_checkpoint(
                                    stage_code="ASSISTANT_HISTORY_SAVE_FAILED",
                                    stage="최종 답변 대화이력 저장 실패",
                                    file="app/history.py",
                                    function="ChatHistoryStore.append_message",
                                    phase="FAILED",
                                    details={"answerDeliveryContinues": True},
                                    customization_hint=(
                                        "Redis/메모리 이력 저장 구현과 장애 정책은 "
                                        "app/history.py를 수정하세요."
                                    ),
                                    error=exc,
                                )
                                logger.warning(
                                    "!!!!!!!! 추천질문 출력 변환 실패 | "
                                    "답변반환계속=예 | 오류유형=%s",
                                    type(exc).__name__,
                                )
                                recommended_questions = []
                        trace_checkpoint(
                            stage_code="FINAL_OUTPUT_ASSEMBLED",
                            stage="최종 사용자 출력 조립 완료",
                            file="app/api.py",
                            function="create_app.stream_chat.event_stream",
                            details={
                                "responseStatus": response_status,
                                "answerLength": len(full_answer),
                                "tokenEventCount": token_count,
                                "renderableCount": len(guarded_renderables),
                                "recommendedQuestionCount": len(
                                    recommended_questions
                                ),
                            },
                            customization_hint=(
                                "최종 messages/token/recommendedQuestions 조립은 "
                                "app/api.py의 event_stream 후반부를 수정하세요."
                            ),
                        )
                        for trace_frame in drain_trace_frames():
                            yield trace_frame
                        logger.info(
                            "FLOW 최종 답변 완료 | OUTPUT가드레일=통과 | "
                            "응답상태=%s | SSE토큰이벤트개수=%d | 답변길이=%d | "
                            "답변본문로그=생략",
                            response_status,
                            token_count,
                            len(full_answer),
                        )

                        # token 스트리밍이 끝난 뒤 같은 assistant_message_id로 완성
                        # 메시지를 다시 보낸다. 프론트는 기존 말풍선을 갱신하고,
                        # 본문과 분리된 renderables를 표·차트·카드 등으로 렌더링한다.
                        completed_messages = [
                            initial_messages[0],
                            {
                                "role": "assistant",
                                "id": assistant_message_id,
                                "parentMessageId": user_message_id,
                                "content": full_answer,
                                "metadata": {
                                    "renderables": guarded_renderables,
                                    "recommendedQuestions": (recommended_questions),
                                },
                            },
                        ]
                        logger.info(
                            "======== SSE 완성 메시지 전달 | 이벤트=messages | "
                            "답변길이=%d | 확장데이터개수=%d | 메시지ID=%s",
                            len(full_answer),
                            len(guarded_renderables),
                            assistant_message_id,
                        )
                        yield encode_sse("messages", completed_messages)
                        answer_delivered = True
                        if isinstance(result.post_answer_interrupt, dict):
                            post_actions = build_action_event(
                                thread_id,
                                result.post_answer_interrupt,
                            )
                            if post_actions:
                                guarded_post_message = await _guard_output_text(
                                    str(post_actions[0].get("message") or ""),
                                    guardrail_client=guardrail_client,
                                    context=stream_guard_context("output.post_action"),
                                )
                                if guarded_post_message is not None:
                                    for action in post_actions:
                                        action["message"] = guarded_post_message
                                    logger.info(
                                        "======== 답변 후 action 전달 | action=%s | thread=%s",
                                        post_actions[0].get("code"),
                                        thread_id,
                                    )
                                    yield encode_sse("action", post_actions)
                                    hitl_pending = True
                        if recommended_questions:
                            logger.info(
                                "======== SSE 추천질문 전달 | "
                                "이벤트=recommendedQuestions | 개수=%d | "
                                "세부시나리오=%s",
                                len(recommended_questions),
                                [
                                    item["detailScenarioCode"]
                                    for item in recommended_questions
                                ],
                            )
                            yield encode_sse(
                                "recommendedQuestions",
                                recommended_questions,
                            )

                        # 예외 응답은 이력에 저장하지 않고 정상 업무 PASS의 답변만
                        # 보정 질문과 같은 사원·세션·에이전트 범위에 저장한다.
                        if (
                            response_status == "PASS"
                            and result.classification.agent_code is not None
                            and not result.agent_switch_cancelled
                            and full_answer
                        ):
                            current_stage = "REDIS_ASSISTANT_HISTORY_SAVE"
                            try:
                                saved = await history_store.append_message(
                                    employee_id=employee_id,
                                    session_id=session_id,
                                    agent_code=result.classification.agent_code,
                                    role="assistant",
                                    content=full_answer,
                                    message_id=assistant_message_id,
                                    metadata={
                                        "renderables": guarded_renderables,
                                        "recommendedQuestions": (recommended_questions),
                                    },
                                    project_code=normalized_endpoint,
                                )
                                logger.info(
                                    "======== 최종 답변 Redis 저장 결과 | 저장=%s | "
                                    "에이전트=%s | 답변길이=%d",
                                    saved,
                                    result.classification.agent_code,
                                    len(full_answer),
                                )
                            except Exception as exc:
                                logger.warning(
                                    "!!!!!!!! 최종 답변 이력 저장 실패 | "
                                    "답변반환계속=예 | 오류유형=%s",
                                    type(exc).__name__,
                                )
                        else:
                            logger.info(
                                "======== 최종 답변 Redis 저장 생략 | 상태=%s | "
                                "에이전트=%s | 답변존재=%s",
                                result.status,
                                result.classification.agent_code,
                                bool(full_answer),
                            )

                    trace_checkpoint(
                        stage_code="SSE_RESPONSE_COMPLETED",
                        stage="SSE 응답 최종 이벤트 준비 완료",
                        file="app/api.py",
                        function="create_app.stream_chat.event_stream",
                        details={
                            "status": response_status,
                            "answerDelivered": answer_delivered,
                            "nextEvents": ["duration", "end"],
                        },
                        customization_hint=(
                            "SSE 이벤트명과 전송 순서는 app/api.py의 event_stream, "
                            "직렬화 봉투는 app/streaming.py의 encode_sse를 "
                            "수정하세요."
                        ),
                    )
                    for trace_frame in drain_trace_frames():
                        yield trace_frame

                    duration = round(perf_counter() - started_at, 3)
                    current_stage = "SSE_COMPLETE"
                    logger.info(
                        "FLOW 요청 처리 종료 | 상태=%s | 총소요시간=%.3f초",
                        response_status,
                        duration,
                    )
                    yield encode_sse("duration", {"seconds": duration})
                    logger.info(
                        "======== 정상 종료 전달 | 이벤트=end | 상태=%s",
                        response_status,
                    )
                    finish_langfuse_trace(
                        langfuse_trace,
                        status=response_status,
                        duration_seconds=duration,
                        agent_code=(
                            result.classification.agent_code if result else None
                        ),
                        scenario_codes=(
                            [
                                match.detail_scenario_code
                                for match in result.subagent.matches
                            ]
                            if result is not None and result.subagent is not None
                            else []
                        ),
                        answer_length=len(full_answer),
                    )
                    # 클라이언트가 end 이벤트를 받은 직후 연결을 닫아도 trace의
                    # 최종 output이 남도록 end를 yield하기 전에 업데이트한다.
                    # PASS도 NEXT_PAGE 대기 중일 수 있으므로 status만으로 HITL
                    # 종료를 판단하지 않는다. thread_id는 재개 가능한 ID만 반환한다.
                    yield encode_sse("end", {
                        "status": response_status,
                        "hitl_pending": hitl_pending,
                        "thread_id": thread_id if hitl_pending else None,
                    })
                    logger.info(
                        "======== 요청 처리 전체 완료 | 상태=%s | 소요시간=%.3f초",
                        response_status,
                        duration,
                    )
                except asyncio.CancelledError:
                    cancelled_duration = perf_counter() - started_at
                    finish_langfuse_trace(
                        langfuse_trace,
                        status="STOPPED",
                        duration_seconds=cancelled_duration,
                        agent_code=(
                            result.classification.agent_code if result else None
                        ),
                        answer_length=len(full_answer),
                    )
                    logger.info(
                        "======== 사용자 연결 중단 | 현재까지소요시간=%.3f초",
                        cancelled_duration,
                    )
                    raise
                except HitlStateNotFoundError as exc:
                    duration = round(perf_counter() - started_at, 3)
                    finish_langfuse_trace(
                        langfuse_trace,
                        status="ERROR",
                        duration_seconds=duration,
                        answer_length=0,
                    )
                    trace_checkpoint(
                        stage_code="HITL_STATE_NOT_FOUND",
                        stage="HITL 재진입 상태 조회 실패",
                        file="app/graph.py",
                        function="MasterIntentGraph.resume",
                        phase="FAILED",
                        details={
                            "currentStage": current_stage,
                            "threadId": thread_id,
                            "fallbackAnswerWillBeSent": True,
                        },
                        customization_hint=(
                            "thread/session 전달값과 app/hitl_store.py의 키·TTL·범위 "
                            "검증을 확인하세요. 개발 중 Redis가 없으면 "
                            "HITL_STATE_BACKEND=memory를 사용하세요."
                        ),
                        error=exc,
                    )
                    for trace_frame in drain_trace_frames():
                        yield trace_frame
                    logger.warning(
                        "!!!!!!!! 처리 중단 진단 | 실패단계=%s | "
                        "코드위치=app/api.py:event_stream -> "
                        "app/graph.py:MasterIntentGraph.resume | "
                        "오류코드=HITL_STATE_NOT_FOUND | 가능한원인=thread_id 만료, "
                        "이미 완료·삭제된 상태, 사원/session 범위 불일치 | "
                        "확인및수정=Redis HITL 키 TTL과 프론트 thread_id/session_id를 "
                        "확인 | 자동재시도횟수=0 | 소요시간=%.3f초",
                        current_stage,
                        duration,
                    )
                    hitl_error_answer = build_safe_error_answer(
                        SAFE_HITL_FALLBACK_MESSAGE,
                        error_code="HITL_STATE_NOT_FOUND",
                        error_detail=str(exc),
                        include_details=settings.response_error_details_enabled,
                    )
                    hitl_fallback = await _guard_output_text(
                        hitl_error_answer,
                        guardrail_client=guardrail_client,
                        context=stream_guard_context("output.hitl_fallback"),
                    )
                    if hitl_fallback is None:
                        hitl_fallback = await _guard_block_message(
                            guardrail_client=guardrail_client,
                            context=stream_guard_context("output.block_message"),
                        )
                    for trace_frame in drain_trace_frames():
                        yield trace_frame
                    for frame in _fallback_answer_frames(
                        user_message=initial_messages[0],
                        assistant_message_id=assistant_message_id,
                        answer=hitl_fallback,
                        duration_seconds=duration,
                    ):
                        yield frame
                except Exception as exc:
                    duration = round(perf_counter() - started_at, 3)
                    finish_langfuse_trace(
                        langfuse_trace,
                        status="ERROR",
                        duration_seconds=duration,
                        agent_code=(
                            result.classification.agent_code if result else None
                        ),
                        answer_length=len(full_answer),
                    )
                    trace_checkpoint(
                        stage_code="REQUEST_PROCESSING_FAILED",
                        stage=f"요청 처리 실패/{current_stage}",
                        file="app/api.py",
                        function="create_app.stream_chat.event_stream",
                        phase="FAILED",
                        details={
                            "currentStage": current_stage,
                            "answerAlreadyDelivered": answer_delivered,
                            "fallbackAnswerWillBeSent": not answer_delivered,
                        },
                        customization_hint=(
                            "바로 앞 FAILED trace의 source.file/function과 같은 "
                            "request_id의 서버 실패 진단 로그를 먼저 확인하세요."
                        ),
                        error=exc,
                    )
                    for trace_frame in drain_trace_frames():
                        yield trace_frame
                    log_failure_diagnostic(
                        stage=f"SSE 요청 처리/{current_stage}",
                        code_location="app/api.py:create_app.event_stream",
                        exc=exc,
                        likely_cause=(
                            "현재단계 또는 직전 LangGraph 노드의 외부 호출·상태 "
                            "변환·답변 스트리밍 오류"
                        ),
                        corrective_action=(
                            "같은 request_id의 가장 먼저 출력된 '실패 진단' 로그에서 "
                            "코드위치와 확인및수정 항목을 확인하세요. 이 요청은 "
                            "자동 재시도하지 않았습니다."
                        ),
                        retry_count=0,
                        context={
                            "current_stage": current_stage,
                            "endpoint": normalized_endpoint,
                            "frontend_agent_code": normalized_agent,
                            "is_hitl_continuation": body.is_hitl_continuation,
                            "duration_seconds": duration,
                        },
                    )
                    # URL, 토큰, 스택과 내부 분류 결과는 프론트로 노출하지 않는다.
                    # 답변을 이미 보낸 뒤 이력 저장 등 부가 단계가 실패한 경우에는
                    # 기존 답변을 덮지 않고 종료 이벤트만 보낸다.
                    if answer_delivered:
                        yield encode_sse("duration", {"seconds": duration})
                        yield encode_sse("end", {"status": "PASS"})
                    else:
                        processing_error_answer = build_safe_error_answer(
                            SAFE_PROCESSING_FALLBACK_MESSAGE,
                            error_code=error_code_for_exception(exc),
                            error_detail=str(exc),
                            include_details=(
                                settings.response_error_details_enabled
                            ),
                        )
                        processing_fallback = await _guard_output_text(
                            processing_error_answer,
                            guardrail_client=guardrail_client,
                            context=stream_guard_context("output.error_fallback"),
                        )
                        if processing_fallback is None:
                            processing_fallback = await _guard_block_message(
                                guardrail_client=guardrail_client,
                                context=stream_guard_context("output.block_message"),
                            )
                        for trace_frame in drain_trace_frames():
                            yield trace_frame
                        # 스트리밍 도중 일부 token이 전달된 뒤 오류가 난 경우에도
                        # 프론트가 부분 답변/표를 유지하지 않도록 빈 assistant 트리를
                        # 먼저 다시 전달한 후 오류 고정답변으로 교체한다.
                        yield encode_sse("messages", initial_messages)
                        for frame in _fallback_answer_frames(
                            user_message=initial_messages[0],
                            assistant_message_id=assistant_message_id,
                            answer=processing_fallback,
                            duration_seconds=duration,
                        ):
                            yield frame

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _guardrail_context(
    *,
    trace_id: str,
    session_id: str,
    user_id: str,
    endpoint: str,
    surface: str,
    project_code: str,
) -> GuardrailContext:
    """한 요청 안에서 입력·문장·테이블을 구분할 추적 문맥을 만든다."""

    return GuardrailContext(
        trace_id=trace_id,
        session_id=session_id,
        user_id=user_id,
        metadata={"endpoint": endpoint, "surface": surface},
        tags=(project_code, endpoint, surface),
    )


@timed("HITL 입력값 가드레일")
async def _guard_hitl_input_values(
    values: dict[str, Any],
    *,
    guardrail_client: GuardrailClient,
    context_factory,
) -> dict[str, Any]:
    """humanInput 코드를 기준으로 필드별 가드레일 정책을 적용한다.

    정책은 Python HITL/action 정의에서 등록되며 Redis 상태에는 저장하지 않는다.
    정책이 False인 필드는 MCP 조회키 보존을 위해 원문을 그대로 전달한다.
    """

    guarded: dict[str, Any] = {}
    for input_code, raw_value in values.items():
        enabled = is_hitl_input_guardrail_enabled(input_code)
        logger.info(
            "======== HITL 입력 가드레일 정책\n"
            "입력코드=%s\n가드레일적용=%s\n처리=%s",
            input_code,
            enabled,
            "INPUT 가드레일 호출" if enabled else "원문 유지",
        )
        if not enabled:
            guarded[input_code] = raw_value
            continue
        guarded[input_code] = await _guard_input_value(
            raw_value,
            guardrail_client=guardrail_client,
            context_factory=context_factory,
            path=f"humanInput.{input_code}",
        )
    return guarded


@timed("HITL 단일 입력값 가드레일")
async def _guard_input_value(
    value: Any,
    *,
    guardrail_client: GuardrailClient,
    context_factory,
    path: str,
) -> Any:
    """가드레일 적용 대상으로 선택된 입력 내부 문자열을 검사한다."""

    if isinstance(value, str):
        decision = await guardrail_client.process_text(
            value,
            role="user",
            process_type="INPUT",
            context=context_factory(f"input.{path}"),
        )
        if not decision.allowed:
            raise ValueError("GUARDRAIL_INPUT_BLOCK")
        return decision.processed_content
    if isinstance(value, list):
        return [
            await _guard_input_value(
                item,
                guardrail_client=guardrail_client,
                context_factory=context_factory,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: await _guard_input_value(
                item,
                guardrail_client=guardrail_client,
                context_factory=context_factory,
                path=f"{path}.{key}",
            )
            for key, item in value.items()
        }
    return value


@timed("OUTPUT 텍스트 가드레일")
async def _guard_output_text(
    text: str,
    *,
    guardrail_client: GuardrailClient,
    context: GuardrailContext,
) -> str | None:
    """assistant 출력에서 PASS 원문 또는 MASK 결과만 반환한다."""

    decision = await guardrail_client.process_text(
        text,
        role="assistant",
        process_type="OUTPUT",
        context=context,
    )
    return decision.processed_content if decision.allowed else None


@timed("BLOCK 고정답변 가드레일")
async def _guard_block_message(
    *,
    guardrail_client: GuardrailClient,
    context: GuardrailContext,
) -> str:
    """BLOCK 고정 문구도 OUTPUT 검사를 통과한 값만 반환한다."""

    guarded = await _guard_output_text(
        SAFE_GUARDRAIL_BLOCK_MESSAGE,
        guardrail_client=guardrail_client,
        context=context,
    )
    # 고정 차단 문구까지 BLOCK되면 미검사 대체 문구를 노출하지 않는다.
    return guarded or ""


@timed("Markdown 테이블 OUTPUT 가드레일")
async def _guard_markdown_tables(
    renderables: list[dict[str, Any]],
    *,
    guardrail_client: GuardrailClient,
    context_factory,
) -> tuple[list[dict[str, Any]], bool]:
    """Markdown table content만 검사하고 MASK 결과를 프론트 계약에 반영한다.

    MASK일 때 표의 원본 ``data``가 마스킹을 우회해 화면에 노출되지 않도록 data를
    제거해 프론트가 처리된 ``content``만 렌더링하게 한다. PASS일 때는 기존
    프론트 호환성을 위해 data를 유지한다. 다른 renderable 형식은 검사하지 않는다.
    """

    guarded_renderables: list[dict[str, Any]] = []
    for index, original in enumerate(renderables):
        item = deepcopy(original)
        if item.get("type") != "table" or item.get("format") != "markdown":
            guarded_renderables.append(item)
            continue
        markdown = item.get("content")
        if not isinstance(markdown, str):
            logger.warning(
                "!!!!!!!! Markdown 테이블 content 누락 | index=%d | 차단=예",
                index,
            )
            return [], True
        decision = await guardrail_client.process_text(
            markdown,
            role="assistant",
            process_type="OUTPUT",
            context=context_factory(f"output.table[{index}]"),
        )
        if not decision.allowed:
            logger.warning(
                "!!!!!!!! Markdown 테이블 가드레일 BLOCK | index=%d",
                index,
            )
            return [], True
        item["content"] = decision.processed_content
        # MASK일 때 구조화 data에는 여전히 원문이 있으므로 제거한다. PASS이면
        # 검사한 Markdown과 동일한 값이므로 기존 프론트 호환성을 위해 유지한다.
        if decision.action == "MASK":
            item["data"] = None
        metadata = item.get("metadata")
        item["metadata"] = dict(metadata) if isinstance(metadata, dict) else {}
        item["metadata"]["guardrailProcessed"] = decision.applied
        item["metadata"]["guardrailAction"] = decision.action
        guarded_renderables.append(item)
    return guarded_renderables, False


def _resolve_employee_id(
    user: StreamingUser | None,
    session_id: str,
) -> str:
    """Redis 범위에 사용할 사번 또는 세션별 익명 식별자를 반환한다.

    최종 연계 명세는 user와 user.id의 null을 허용한다. null을 공통 문자열로
    저장하면 서로 다른 사용자의 이력이 섞일 수 있으므로 session_id 해시를 붙인
    내부 값으로 격리한다. 실제 사번이 들어오면 변환 없이 그대로 사용한다.
    """

    if user is not None and user.id is not None:
        return user.id
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return f"anonymous_{digest}"


def _derive_recruitment_org_type_code(
    employee_id: str | None,
) -> str | None:
    """사원번호 형식으로 유치조직구분코드를 계산한다.

    - 일반(11): S + 숫자 6자리
    - 제휴(12): K + 숫자 7자리
    - 복합(13): U + 숫자 6자리

    규칙에 맞지 않는 사원번호는 임의 분류하지 않고 None을 반환한다.
    """

    if employee_id is None:
        return None
    normalized = employee_id.strip().upper()
    patterns = (
        (r"S\d{6}", "11"),
        (r"K\d{7}", "12"),
        (r"U\d{6}", "13"),
    )
    for pattern, code in patterns:
        if re.fullmatch(pattern, normalized):
            return code
    return None


def _fallback_answer_frames(
    *,
    user_message: dict[str, Any],
    assistant_message_id: str,
    answer: str,
    duration_seconds: float,
) -> tuple[str, ...]:
    """처리 예외를 프론트가 표시할 수 있는 정상 SSE 답변 프레임으로 만든다."""

    assistant_message = {
        "role": "assistant",
        "id": assistant_message_id,
        "parentMessageId": user_message.get("id"),
        "content": answer,
        "metadata": {
            "renderables": [],
            "recommendedQuestions": [],
            "fallbackUsed": True,
        },
    }
    return (
        *(encode_sse("token", chunk) for chunk in split_text(answer)),
        encode_sse("messages", [user_message, assistant_message]),
        encode_sse("duration", {"seconds": duration_seconds}),
        encode_sse("end", {"status": "EXCEPTION", "fallbackUsed": True}),
    )


def _fallback_sse_frames(
    *,
    request_id: str,
    session_id: str,
    thread_id: str,
    user_message_id: str,
    assistant_message_id: str,
    user_content: Any,
    answer: str,
    duration_seconds: float,
) -> tuple[str, ...]:
    """그래프 진입 전 입력 오류도 운영 SSE 계약의 답변으로 변환한다."""

    user_message = {
        "role": "user",
        "id": user_message_id,
        "content": user_content,
    }
    initial_assistant = {
        "role": "assistant",
        "id": assistant_message_id,
        "parentMessageId": user_message_id,
        "content": "",
        "metadata": {"renderables": [], "recommendedQuestions": []},
    }
    return (
        encode_sse("request_id", request_id),
        encode_sse("session_id", session_id),
        encode_sse("thread_id", thread_id),
        encode_sse("messages", [user_message, initial_assistant]),
        *_fallback_answer_frames(
            user_message=user_message,
            assistant_message_id=assistant_message_id,
            answer=answer,
            duration_seconds=duration_seconds,
        ),
    )
