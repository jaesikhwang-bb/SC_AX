"""FastAPI 엔드포인트와 애플리케이션 의존성을 구성하는 모듈."""

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from app.answers import AnswerService, create_answer_service
from app.classifier import IntentClassifier, create_classifier
from app.config import Settings
from app.csv_trace import TraceRecorder, create_trace_recorder
from app.graph import MasterIntentGraph, MasterResult
from app.history import ChatHistoryStore, create_history_store
from app.hitl_store import (
    HitlStateNotFoundError,
    HitlStateStore,
    HitlStateStoreUnavailableError,
    create_hitl_state_store,
)
from app.models import ChatRequest, ChatResponse, StreamingChatRequest
from app.mcp.client import McpToolExecutor, create_mcp_tool_executor
from app.observability import configure_logging, logger, timed
from app.prompt_loader import PromptBundleLoader
from app.subagents.router import SubagentRouter, create_subagent_router
from app.streaming import build_action_event, encode_sse


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
) -> FastAPI:
    """설정, 프롬프트, LLM, Redis, 그래프를 조립해 FastAPI 앱을 만든다.

    테스트에서는 가짜 분류기와 메모리 저장소를 주입할 수 있다. 운영 실행에서는
    실제 GenOS 분류기, Redis 대화 이력 저장소, 일반 Redis HITL 저장소가 생성된다.
    LangGraph Checkpointer는 생성하거나 연결하지 않는다.
    """

    configure_logging(os.getenv("LOG_LEVEL", "INFO"))
    settings = settings or Settings.from_env()
    prompt = PromptBundleLoader().load(settings.prompt_version)
    classifier = classifier or create_classifier(settings, prompt)
    history_store = history_store or create_history_store(settings)
    hitl_store = hitl_store or create_hitl_state_store(settings)
    subagent_router = subagent_router or create_subagent_router(settings)
    mcp_executor = mcp_executor or create_mcp_tool_executor(settings)
    answer_service = answer_service or create_answer_service(settings)
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
            "======== 애플리케이션 시작 | 프로젝트=%s | 프롬프트버전=%s | "
            "이력저장소=%s | HITL저장소=%s | HITL활성=%s | "
            "서브에이전트=%s | MCP=%s | "
            "Checkpointer=사용안함",
            settings.project_code,
            prompt.version,
            settings.history_backend,
            settings.hitl_state_backend,
            hitl_store.enabled,
            subagent_router.registered_codes(),
            settings.mcp_backend,
        )
        try:
            yield
        finally:
            # 두 저장소는 기본적으로 같은 Redis 서버를 사용하지만 연결 풀은
            # 각각 생성되므로 애플리케이션 종료 시 모두 닫아 준다.
            await history_store.aclose()
            await hitl_store.aclose()
            await mcp_executor.aclose()
            logger.info("======== 애플리케이션 종료")

    app = FastAPI(
        title="Master Agent Chat API",
        version="0.3.0",
        description="단일 채팅 API 기반 1차 의도분류 및 일반 Redis HITL",
        lifespan=lifespan,
    )

    @app.get("/tester", include_in_schema=False)
    @timed("의도분류 테스트 화면")
    async def intent_tester() -> FileResponse:
        """브라우저에서 사용할 단일 HTML 테스트 화면을 반환한다."""

        html_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "intent_tester.html"
        )
        logger.info(
            "======== 의도분류 테스트 화면 요청 | 파일=%s",
            html_path,
        )
        return FileResponse(html_path, media_type="text/html")

    @app.get("/v1/metadata")
    @timed("채팅 메타데이터 API")
    async def metadata() -> dict[str, Any]:
        """활성 프롬프트에서 읽은 테스트용 에이전트 코드와 모드를 반환한다."""

        logger.info(
            "======== 채팅 메타데이터 요청 | 에이전트개수=%d",
            len(prompt.agent_codes),
        )
        return {
            "prompt_version": prompt.version,
            # manifest에서 동적으로 읽은 값이므로 HTML에 코드를 하드코딩하지 않는다.
            "agent_codes": list(prompt.agent_codes),
            "chat_history_store": settings.history_backend,
            "hitl_state_store": settings.hitl_state_backend,
            "subagent_codes": list(subagent_router.registered_codes()),
            "mcp_backend": settings.mcp_backend,
            "stream_endpoint": "v1/chat/stream",
        }

    @app.get("/health")
    @timed("상태 확인 API")
    async def health() -> dict[str, str]:
        """서버 상태, 프롬프트 버전, HITL 저장 방식을 반환한다."""

        logger.info("======== 상태 확인 요청")
        return {
            "status": "ok",
            "prompt_version": prompt.version,
            "chat_history_store": settings.history_backend,
            "hitl_state_store": settings.hitl_state_backend,
            "langgraph_checkpointer": "disabled",
            "mcp_backend": settings.mcp_backend,
        }

    @app.get("/v1/tester/history", include_in_schema=False)
    @timed("목업 대화이력 조회 API")
    async def tester_history(
        employee_id: str = Query(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_-]+$",
        ),
        conversation_id: str = Query(min_length=1, max_length=200),
        agent_code: str = Query(min_length=1, max_length=100),
    ) -> dict[str, Any]:
        """목업에서 현재 사원·대화·에이전트 범위의 이력을 조회한다.

        저장소 전체를 노출하지 않고 실제 멀티턴 분류에서 사용하는 get_recent와
        같은 범위 조건을 적용한다. 운영용 업무 API가 아니라 테스트 화면의
        진단 기능이므로 OpenAPI 문서에서는 숨긴다.
        """

        normalized_code = agent_code.upper()
        if normalized_code not in prompt.agent_codes:
            logger.info(
                "======== 목업 이력 조회 거절 | 미등록에이전트=%s",
                normalized_code,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "등록되지 않은 agent_code입니다.",
                    "allowed_codes": list(prompt.agent_codes),
                },
            )

        logger.info(
            "======== 목업 대화이력 조회 요청 | 사원번호=%s | "
            "conversation_id=%s | 에이전트=%s | 저장소=%s",
            employee_id,
            conversation_id,
            normalized_code,
            settings.history_backend,
        )
        messages = await history_store.get_recent(
            employee_id,
            conversation_id,
            normalized_code,
            settings.history_limit,
        )
        return {
            "backend": settings.history_backend,
            "scope": {
                "employee_id": employee_id,
                "conversation_id": conversation_id,
                "agent_code": normalized_code,
            },
            "count": len(messages),
            "messages": messages,
        }

    @app.get("/v1/tester/conversations", include_in_schema=False)
    @timed("목업 전체 대화 목록 API")
    async def tester_conversations() -> dict[str, Any]:
        """현재 프로젝트 저장소의 모든 conversation_id를 목업에 반환한다."""

        conversations = await history_store.list_conversations()
        logger.info(
            "======== 목업 전체 대화 목록 반환 | 프로젝트=%s | "
            "저장소=%s | 개수=%d",
            settings.project_code,
            settings.history_backend,
            len(conversations),
        )
        return {
            "project_code": settings.project_code,
            "backend": settings.history_backend,
            "count": len(conversations),
            "conversations": conversations,
        }

    @app.delete("/v1/tester/conversations", include_in_schema=False)
    @timed("목업 대화 삭제 API")
    async def delete_tester_conversation(
        employee_id: str = Query(
            min_length=1,
            max_length=100,
            pattern=r"^[A-Za-z0-9_-]+$",
        ),
        conversation_id: str = Query(min_length=1, max_length=200),
    ) -> dict[str, Any]:
        """선택한 사원·conversation의 모든 에이전트 대화이력을 삭제한다."""

        logger.info(
            "======== 목업 대화 삭제 요청 | 프로젝트=%s | 사원번호=%s | "
            "conversation_id=%s | 저장소=%s",
            settings.project_code,
            employee_id,
            conversation_id,
            settings.history_backend,
        )
        deleted_messages = await history_store.delete_conversation(
            employee_id,
            conversation_id,
        )
        return {
            "project_code": settings.project_code,
            "backend": settings.history_backend,
            "employee_id": employee_id,
            "conversation_id": conversation_id,
            "deleted_message_count": deleted_messages,
        }

    @app.post("/v1/chat", response_model=ChatResponse)
    @timed(
        "채팅 요청 전체 처리",
        expected_exceptions=(HTTPException,),
    )
    async def chat(body: ChatRequest) -> ChatResponse:
        """신규 질문 또는 Redis HITL 입력을 동일한 API에서 처리한다."""

        graph: MasterIntentGraph = app.state.graph

        # thread_id가 있으면 신규 질문이 아니라 이전 INPUT_REQUIRED 응답에 대한
        # 후속 입력이다. Redis에 저장된 hitl_type과 상태를 기준으로 LangGraph가
        # 검증 Edge를 선택하므로 프론트가 분기 코드를 따로 판단할 필요가 없다.
        if body.is_hitl_continuation:
            assert body.thread_id is not None
            assert body.hitl_input is not None
            logger.info(
                "======== 통합 채팅 요청 도착 | 유형=HITL재진입 | "
                "thread_id=%s | 입력=%s",
                body.thread_id,
                body.hitl_input,
            )
            try:
                result = await graph.resume(
                    thread_id=body.thread_id,
                    value=body.hitl_input,
                )
            except HitlStateNotFoundError as exc:
                # TTL 만료, 잘못된 thread_id 또는 이미 승인되어 DEL된 상태이다.
                logger.info(
                    "======== HITL 재진입 거절 | Redis HITL 상태 없음 | "
                    "thread_id=%s",
                    body.thread_id,
                )
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "HITL_STATE_NOT_FOUND",
                        "message": (
                            "이어 갈 HITL 상태를 찾을 수 없습니다. 상태가 "
                            "만료됐거나, 이미 처리됐거나, thread_id가 "
                            "올바르지 않을 수 있습니다."
                        ),
                        "thread_id": body.thread_id,
                        "action": "START_NEW_CHAT",
                    },
                ) from exc
            except HitlStateStoreUnavailableError as exc:
                raise _hitl_store_unavailable() from exc

            logger.info(
                "======== 통합 채팅 처리 결과 | 유형=HITL재진입 | "
                "thread_id=%s | 상태=%s",
                body.thread_id,
                result.status,
            )
            return _response(result)

        # Pydantic의 요청 모드 검증을 통과했으므로 신규 질문의 message와
        # employee_id는 반드시 존재한다. frontend_agent_code는 사용자가
        # 에이전트를 선택하지 않은 경우 None일 수 있다.
        assert body.message is not None
        assert body.employee_id is not None
        frontend_code = (
            body.frontend_agent_code.upper()
            if body.frontend_agent_code is not None
            else None
        )
        if (
            frontend_code is not None
            and frontend_code not in prompt.agent_codes
        ):
            logger.info(
                "======== 요청 검증 실패 | 알 수 없는 에이전트코드=%s",
                frontend_code,
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "등록되지 않은 frontend_agent_code입니다.",
                    "allowed_codes": list(prompt.agent_codes),
                },
            )

        # thread_id는 한 번의 채팅/HITL 흐름을 식별한다. conversation_id는
        # 같은 사원이 동일 에이전트와 이어 가는 여러 질문을 하나로 묶는다.
        thread_id = str(uuid4())
        conversation_id = body.conversation_id or thread_id
        logger.info(
            "======== 통합 채팅 요청 도착 | 유형=신규질문 | "
            "thread_id=%s | 사원번호=%s | conversation_id=%s | "
            "프론트에이전트=%s",
            thread_id,
            body.employee_id,
            conversation_id,
            frontend_code or "선택하지 않음",
        )

        try:
            result = await graph.start(
                thread_id=thread_id,
                employee_id=body.employee_id,
                conversation_id=conversation_id,
                message=body.message,
                frontend_agent_code=frontend_code,
            )
        except HitlStateStoreUnavailableError as exc:
            raise _hitl_store_unavailable() from exc

        logger.info(
            "======== 통합 채팅 처리 결과 | 유형=신규질문 | "
            "thread_id=%s | 상태=%s",
            thread_id,
            result.status,
        )
        return _response(result)

    @app.post("/v1/chat/stream")
    @timed("스트리밍 채팅 요청 접수")
    async def stream_chat(
        body: StreamingChatRequest,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        """신규 프론트 계약을 받아 SSE 이벤트 스트림으로 처리한다.

        GenOS Gateway가 Authorization을 검증하는 운영 구성을 전제로 한다.
        로컬 목업에서도 호출할 수 있도록 애플리케이션 자체에서는 토큰 값을
        강제하지 않으며, 헤더 존재 여부만 로그에 기록한다.
        """

        normalized_endpoint = body.endpoint.casefold()
        if normalized_endpoint != settings.project_code:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_ENDPOINT",
                    "message": "이 서비스에서 처리할 수 없는 endpoint입니다.",
                    "allowed_endpoint": settings.project_code,
                },
            )
        normalized_agent = (
            body.agent_code.upper() if body.agent_code is not None else None
        )
        if (
            normalized_agent is not None
            and normalized_agent not in prompt.agent_codes
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_AGENT_CODE",
                    "message": "등록되지 않은 agent_code입니다.",
                    "allowed_codes": list(prompt.agent_codes),
                },
            )

        request_id = f"{settings.project_code}:{uuid4()}"
        # 신규 질문은 서버가 thread_id를 생성하고, HITL 재진입은 프론트가
        # 직전 action 이벤트에서 받은 값을 그대로 반환한다.
        thread_id = body.thread_id or str(uuid4())
        user_message_id = str(uuid4())
        assistant_message_id = str(uuid4())
        logger.info(
            "======== 스트리밍 요청 도착 | request_id=%s | session_id=%s | "
            "thread_id=%s | 사원번호=%s | 유형=%s | 인증헤더=%s",
            request_id,
            body.session_id,
            thread_id,
            body.employee_id,
            "HITL재진입" if body.is_hitl_continuation else "신규질문",
            "있음" if authorization else "없음(로컬테스트)",
        )

        async def event_stream():
            started_at = perf_counter()
            yield encode_sse("request_id", request_id)
            yield encode_sse("session_id", body.session_id)
            yield encode_sse("thread_id", thread_id)
            yield encode_sse(
                "message_id",
                {"role": "user", "id": user_message_id},
            )
            yield encode_sse(
                "message_id",
                {"role": "assistant", "id": assistant_message_id},
            )
            yield encode_sse(
                "messages",
                [
                    {
                        "role": "user",
                        "id": user_message_id,
                        "content": (
                            body.message
                            if body.message is not None
                            else [
                                item.model_dump(by_alias=True)
                                for item in body.human_input
                            ]
                        ),
                    }
                ],
            )

            try:
                graph: MasterIntentGraph = app.state.graph
                if body.is_hitl_continuation:
                    result = await graph.resume(
                        thread_id=thread_id,
                        value=body.to_hitl_value(),
                        expected_employee_id=body.employee_id,
                        expected_conversation_id=body.session_id,
                    )
                else:
                    assert body.message is not None
                    result = await graph.start(
                        thread_id=thread_id,
                        employee_id=body.employee_id,
                        conversation_id=body.session_id,
                        message=body.message,
                        frontend_agent_code=normalized_agent,
                    )

                # 기존 JSON API에서 확인하던 마스터·서브·MCP 결과도 messages의
                # metadata로 제공한다. 표준 이벤트 종류를 늘리지 않으면서 목업과
                # 운영 프론트가 처리 과정을 진단할 수 있다.
                yield encode_sse(
                    "messages",
                    [
                        {
                            "role": "assistant",
                            "id": assistant_message_id,
                            "content": "",
                            "metadata": {
                                "status": result.status,
                                "classification": result.classification.model_dump(
                                    mode="json"
                                ),
                                "subagent": (
                                    result.subagent.model_dump(mode="json")
                                    if result.subagent is not None
                                    else None
                                ),
                                "mcp_results": [
                                    item.model_dump(mode="json")
                                    for item in (result.mcp_results or [])
                                ],
                            },
                        }
                    ],
                )

                if result.status == "INPUT_REQUIRED":
                    yield encode_sse(
                        "action",
                        build_action_event(thread_id, result.interrupt),
                    )
                else:
                    prepared = answer_service.prepare(result)
                    answer_parts: list[str] = []
                    if prepared.source_documents:
                        yield encode_sse(
                            "sourceDocuments",
                            prepared.source_documents,
                        )
                    async for token in prepared.tokens:
                        if token:
                            answer_parts.append(token)
                            yield encode_sse("token", token)

                    full_answer = "".join(answer_parts)
                    if full_answer:
                        # token 누적이 끝난 뒤 완성된 assistant 메시지를 한 번 더
                        # 보내 재접속·메시지 트리 동기화 시 그대로 저장할 수 있게 한다.
                        yield encode_sse(
                            "messages",
                            [
                                {
                                    "role": "assistant",
                                    "id": assistant_message_id,
                                    "content": full_answer,
                                }
                            ],
                        )

                    # 마스터 예외는 기존 정책대로 어떤 대화도 저장하지 않는다.
                    # 정상 PASS 답변만 같은 사원·session·agent 범위에 assistant
                    # 메시지로 저장하여 다음 멀티턴에서 질문과 답변을 함께 참고한다.
                    if (
                        result.status == "PASS"
                        and result.classification.agent_code is not None
                        and answer_parts
                    ):
                        await history_store.append_message(
                            employee_id=body.employee_id,
                            conversation_id=body.session_id,
                            agent_code=result.classification.agent_code,
                            role="assistant",
                            content=full_answer,
                            message_id=assistant_message_id,
                        )

                duration = round(perf_counter() - started_at, 3)
                yield encode_sse(
                    "duration",
                    {"seconds": duration, "formatted": f"{duration:.3f}초"},
                )
                yield encode_sse(
                    "end",
                    {"status": result.status, "thread_id": thread_id},
                )
                logger.info(
                    "======== 스트리밍 정상 종료 | request_id=%s | "
                    "상태=%s | 소요시간=%.3f초",
                    request_id,
                    result.status,
                    duration,
                )
            except asyncio.CancelledError:
                # 연결을 끊은 클라이언트에는 stopped 이벤트를 더 보낼 수 없으므로
                # 서버 로그에 중단 상태를 남기고 실행 취소를 전파한다.
                logger.info(
                    "======== 스트리밍 사용자 중단 | request_id=%s | "
                    "thread_id=%s",
                    request_id,
                    thread_id,
                )
                raise
            except HitlStateNotFoundError:
                duration = round(perf_counter() - started_at, 3)
                yield encode_sse(
                    "error",
                    {
                        "code": "HITL_STATE_NOT_FOUND",
                        "message": (
                            "이어 갈 입력 상태를 찾을 수 없습니다. 새 질문으로 "
                            "다시 시작해 주세요."
                        ),
                        "thread_id": thread_id,
                    },
                )
                yield encode_sse(
                    "duration",
                    {"seconds": duration, "formatted": f"{duration:.3f}초"},
                )
            except Exception as exc:
                duration = round(perf_counter() - started_at, 3)
                logger.exception(
                    "======== 스트리밍 처리 오류 | request_id=%s | 오류=%s",
                    request_id,
                    exc,
                )
                # 내부 URL, 토큰, 스택 정보는 프론트로 노출하지 않는다.
                yield encode_sse(
                    "error",
                    {
                        "code": "STREAM_PROCESSING_ERROR",
                        "message": (
                            "요청 처리 중 오류가 발생했습니다. 잠시 후 다시 "
                            "시도해 주세요."
                        ),
                        "request_id": request_id,
                    },
                )
                yield encode_sse(
                    "duration",
                    {"seconds": duration, "formatted": f"{duration:.3f}초"},
                )

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


def _hitl_store_unavailable() -> HTTPException:
    """HITL Redis 장애에 사용할 공통 503 응답을 만든다."""

    logger.exception(
        "======== HITL 처리 실패 | 일반 Redis 저장소에 연결할 수 없음"
    )
    return HTTPException(
        status_code=503,
        detail={
            "code": "HITL_STATE_STORE_UNAVAILABLE",
            "message": (
                "HITL 상태 저장소에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요."
            ),
            "action": "RETRY",
        },
    )


def _response(result: MasterResult) -> ChatResponse:
    """내부 그래프 결과를 외부 HTTP 응답 모델로 변환한다."""

    return ChatResponse(
        status=result.status,
        thread_id=result.thread_id,
        classification=result.classification,
        subagent=result.subagent,
        mcp=result.mcp,
        mcp_results=result.mcp_results or [],
        # 필드 이름은 기존 프론트 계약 호환을 위해 interrupt를 유지한다.
        # 실제 구현은 LangGraph interrupt가 아니라 Redis 기반 입력 요청이다.
        interrupt=result.interrupt,
    )
