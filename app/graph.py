"""일반 Redis HITL 상태를 사용하는 마스터 에이전트 LangGraph 워크플로."""

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.classifier import IntentClassifier
from app.csv_trace import EmptyTraceRecorder, TraceRecorder
from app.domain import ClassificationType, IntentClassification
from app.history import ChatHistoryStore
from app.hitl import build_hitl_request, validate_ok_signal
from app.hitl_store import (
    HitlStateNotFoundError,
    HitlStateStore,
    HitlStateStoreUnavailableError,
)
from app.mcp.client import EmptyMcpToolExecutor, McpToolExecutor
from app.mcp.models import McpExecutionResult
from app.observability import logger, timed
from app.subagents.models import SubagentResult
from app.subagents.router import EmptySubagentRouter, SubagentRouter


class MasterState(TypedDict, total=False):
    """한 번의 stateless 그래프 실행에서 노드 사이에 전달되는 상태."""

    # NEW_CHAT은 최초 진입, HITL_RESUME은 Redis 상태 복원 후 재진입을 뜻한다.
    entry_stage: str
    thread_id: str
    message: str
    message_id: str
    employee_id: str
    conversation_id: str
    frontend_agent_code: str | None
    history: list[dict[str, str]]
    classification: dict[str, Any]
    subagent: dict[str, Any] | None
    mcp: dict[str, Any] | None
    # Redis에 저장된 대기 유형이다. HITL 재진입 시 START 조건부 Edge가 이 값을
    # 사용해 유형별 검증 노드로 직접 분기한다.
    hitl_type: str
    human_input: Any
    interrupt: dict[str, Any] | None
    status: str
    approved: bool


@dataclass(frozen=True)
class MasterResult:
    """FastAPI 응답으로 변환하기 전의 프레임워크 독립적인 그래프 결과."""

    status: Literal["PASS", "INPUT_REQUIRED", "EXCEPTION"]
    thread_id: str
    classification: IntentClassification
    interrupt: dict | None = None
    subagent: SubagentResult | None = None
    mcp: McpExecutionResult | None = None


class MasterIntentGraph:
    """1차 의도분류, HITL 승인, 등록된 시나리오 서브에이전트를 실행한다.

    LangGraph Checkpointer와 ``interrupt()``는 사용하지 않는다. 사용자 입력이
    필요하면 필요한 상태만 Redis에 저장하고 현재 실행을 END로 종료한다. 재개
    요청에서는 Redis 상태를 복원해 START의 조건부 Edge가 검증 노드로 바로
    분기하므로 이전 LLM 분류 단계를 다시 실행하지 않는다.
    """

    def __init__(
        self,
        classifier: IntentClassifier,
        history_store: ChatHistoryStore,
        hitl_store: HitlStateStore,
        history_limit: int = 10,
        subagent_router: SubagentRouter | None = None,
        mcp_executor: McpToolExecutor | None = None,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self._classifier = classifier
        self._history_store = history_store
        self._hitl_store = hitl_store
        self._history_limit = history_limit
        self._subagent_router = subagent_router or EmptySubagentRouter()
        self._mcp_executor = mcp_executor or EmptyMcpToolExecutor()
        self._trace_recorder = trace_recorder or EmptyTraceRecorder()

        builder = StateGraph(MasterState)
        builder.add_node("load_history", self._load_history)
        builder.add_node("classify_intent", self._classify_intent)
        builder.add_node("verify_selection", self._verify_selection)
        builder.add_node(
            "validate_agent_code_mismatch",
            self._validate_agent_code_mismatch,
        )
        builder.add_node("save_hitl_state", self._save_hitl_state)
        builder.add_node("persist_user_message", self._persist_user_message)
        builder.add_node("run_subagent", self._run_subagent)
        builder.add_node("call_mcp", self._call_mcp)
        builder.add_node("clear_hitl_state", self._clear_hitl_state)
        builder.add_node("finish_exception", self._finish_exception)

        # 매 HTTP 요청은 새 그래프 실행이다. Redis에서 상태를 복원한 요청은
        # load_history와 LLM 의도분류를 건너뛰고 HITL 검증 Edge로 진입한다.
        builder.add_conditional_edges(
            START,
            self._route_entry,
            {
                "load_history": "load_history",
                "validate_agent_code_mismatch": (
                    "validate_agent_code_mismatch"
                ),
            },
        )
        builder.add_edge("load_history", "classify_intent")
        builder.add_conditional_edges(
            "classify_intent",
            self._after_classification,
            {
                "verify_selection": "verify_selection",
                "finish_exception": "finish_exception",
            },
        )
        builder.add_conditional_edges(
            "verify_selection",
            self._after_input_decision,
            {
                "save_hitl_state": "save_hitl_state",
                "persist_user_message": "persist_user_message",
            },
        )
        builder.add_conditional_edges(
            "validate_agent_code_mismatch",
            self._after_input_decision,
            {
                "save_hitl_state": "save_hitl_state",
                "persist_user_message": "persist_user_message",
            },
        )
        builder.add_edge("save_hitl_state", END)
        builder.add_conditional_edges(
            "persist_user_message",
            self._after_message_persist,
            {
                "run_subagent": "run_subagent",
                "clear_hitl_state": "clear_hitl_state",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "run_subagent",
            self._after_subagent,
            {
                "call_mcp": "call_mcp",
                "clear_hitl_state": "clear_hitl_state",
                "end": END,
            },
        )
        builder.add_conditional_edges(
            "call_mcp",
            self._after_mcp_call,
            {
                "clear_hitl_state": "clear_hitl_state",
                "end": END,
            },
        )
        builder.add_edge("clear_hitl_state", END)
        builder.add_edge("finish_exception", END)

        # Checkpointer를 전달하지 않는다. 상태 지속성은 HitlStateStore가 필요한
        # 필드만 일반 Redis SET/GET/DEL로 직접 관리한다.
        self._graph = builder.compile()

    @staticmethod
    @timed("그래프 진입 단계 라우팅")
    def _route_entry(
        state: MasterState,
    ) -> Literal["load_history", "validate_agent_code_mismatch"]:
        """신규 요청과 Redis HITL 유형에 맞는 시작 Edge를 선택한다."""

        entry_stage = state.get("entry_stage", "NEW_CHAT")
        if entry_stage == "NEW_CHAT":
            logger.info("======== 진입 라우팅 | 신규 채팅 분류 단계")
            return "load_history"
        if entry_stage == "HITL_RESUME":
            hitl_type = state.get("hitl_type")
            if hitl_type == "AGENT_CODE_MISMATCH":
                logger.info(
                    "======== 진입 라우팅 | HITL유형=%s | "
                    "에이전트 코드 변경 검증 단계",
                    hitl_type,
                )
                return "validate_agent_code_mismatch"

            # 향후 MCP 파라미터 입력 유형을 추가할 때는 해당 검증 노드를 등록하고
            # 이 조건부 Edge에 hitl_type 매핑만 추가하면 된다.
            raise ValueError(
                f"지원하지 않는 Redis HITL 유형입니다: {hitl_type}"
            )
        raise ValueError(f"지원하지 않는 entry_stage입니다: {entry_stage}")

    @timed("Redis 대화이력 조회")
    async def _load_history(self, state: MasterState) -> MasterState:
        """프론트가 선택한 에이전트와 동일한 범위의 이력만 조회한다."""

        frontend_code = state.get("frontend_agent_code")
        if frontend_code is None:
            # 에이전트를 선택하지 않은 시점에는 어떤 agent_code의 Redis 키를
            # 조회해야 하는지 확정할 수 없다. 다른 에이전트 이력을 섞지 않도록
            # 빈 이력으로 1차 분류하고, 결과는 최종 분류 코드 범위에 저장한다.
            logger.info(
                "======== Redis 이력 조회 생략 | 프론트 에이전트 미선택 | "
                "이전대화=빈 이력"
            )
            update: MasterState = {"history": []}
            self._trace_recorder.record("대화이력조회완료", {**state, **update})
            return update

        logger.info(
            "======== Redis 이력 조회 요청 | 사원번호=%s | "
            "conversation_id=%s | 에이전트=%s | 최대개수=%d",
            state["employee_id"],
            state["conversation_id"],
            frontend_code,
            self._history_limit,
        )
        history = await self._history_store.get_recent(
            state["employee_id"],
            state["conversation_id"],
            frontend_code,
            self._history_limit,
        )
        logger.info(
            "======== Redis 이력 조회 완료 | 사원번호=%s | "
            "conversation_id=%s | 에이전트=%s | 조회개수=%d",
            state["employee_id"],
            state["conversation_id"],
            frontend_code,
            len(history),
        )
        update = {"history": history}
        self._trace_recorder.record("대화이력조회완료", {**state, **update})
        return update

    @timed("마스터 에이전트 1차 의도분류")
    async def _classify_intent(self, state: MasterState) -> MasterState:
        """현재 질문과 같은 범위의 멀티턴 이력으로 GenOS를 한 번 호출한다."""

        logger.info(
            "======== 1차 의도 분류 시작 | 이전대화개수=%d",
            len(state["history"]),
        )
        started_at = perf_counter()
        try:
            result = await self._classifier.classify(
                state["message"],
                state["history"],
                state.get("frontend_agent_code"),
            )
        except Exception as exc:
            self._trace_recorder.record(
                "마스터의도분류오류",
                state,
                elapsed_seconds=perf_counter() - started_at,
                error=exc,
            )
            raise
        logger.info(
            "======== 1차 의도 분류 완료 | 분류유형=%s | 에이전트=%s",
            result.classification_type.value,
            result.agent_code,
        )
        update = {"classification": result.model_dump(mode="json")}
        self._trace_recorder.record(
            "마스터의도분류완료",
            {**state, **update},
            elapsed_seconds=perf_counter() - started_at,
        )
        return update

    @staticmethod
    @timed("의도분류 결과 라우팅")
    def _after_classification(
        state: MasterState,
    ) -> Literal["verify_selection", "finish_exception"]:
        """정상 업무는 코드 비교로, 예외 유형은 예외 종료로 보낸다."""

        classification = IntentClassification.model_validate(
            state["classification"]
        )
        if classification.classification_type == ClassificationType.AGENT:
            logger.info("======== 라우팅 결정 | 에이전트 코드 비교 단계")
            return "verify_selection"

        logger.info(
            "======== 라우팅 결정 | 예외 종료 단계 | 유형=%s",
            classification.classification_type.value,
        )
        return "finish_exception"

    @staticmethod
    @timed("예외 분류 종료")
    def _finish_exception(state: MasterState) -> MasterState:
        """모든 마스터 예외 유형을 이력 저장 없이 종료한다."""

        logger.info(
            "======== 예외 분류 종료 | 유형=%s | Redis저장=안함",
            state["classification"]["classification_type"],
        )
        return {
            "status": "EXCEPTION",
            "approved": False,
            "interrupt": None,
        }

    @timed("프론트 선택 코드 비교")
    def _verify_selection(self, state: MasterState) -> MasterState:
        """프론트 코드가 있으면 비교하고, 미선택이면 분류 결과를 바로 통과시킨다."""

        classification = IntentClassification.model_validate(
            state["classification"]
        )
        frontend_code = state.get("frontend_agent_code")
        if frontend_code is None:
            logger.info(
                "======== 에이전트 코드 비교 생략 | 프론트 미선택 | "
                "LLM분류=%s | 결과=PASS",
                classification.agent_code,
            )
            update: MasterState = {
                "status": "PASS",
                "approved": True,
                "interrupt": None,
            }
            self._trace_recorder.record("에이전트비교생략", {**state, **update})
            return update

        frontend_code = frontend_code.upper()
        logger.info(
            "======== 에이전트 코드 비교 | 프론트=%s | LLM분류=%s",
            frontend_code,
            classification.agent_code,
        )

        if classification.agent_code == frontend_code:
            logger.info(
                "======== 에이전트 코드 일치 | 결과=PASS | 에이전트=%s",
                frontend_code,
            )
            update = {
                "status": "PASS",
                "approved": True,
                "interrupt": None,
            }
            self._trace_recorder.record("에이전트코드일치", {**state, **update})
            return update

        interrupt = build_hitl_request(
            hitl_type="AGENT_CODE_MISMATCH",
            message=(
                "선택한 에이전트와 질문 의도가 다릅니다. "
                "분류된 에이전트로 변경하시겠습니까?"
            ),
            fields=[
                {
                    "name": "signal",
                    "label": "변경 승인",
                    "type": "hidden",
                    "required": True,
                    "expected_value": "OK",
                }
            ],
            context={
                "frontend_agent_code": frontend_code,
                "classified_agent_code": classification.agent_code,
            },
        )
        logger.info(
            "======== 에이전트 코드 불일치 | Redis HITL 저장 단계 | "
            "프론트=%s | 변경대상=%s",
            frontend_code,
            classification.agent_code,
        )
        update = {
            "status": "INPUT_REQUIRED",
            "approved": False,
            "interrupt": interrupt,
        }
        self._trace_recorder.record("HITL입력요청", {**state, **update})
        return update

    @staticmethod
    @timed("Redis HITL 사용자 입력 검증")
    def _validate_agent_code_mismatch(state: MasterState) -> MasterState:
        """에이전트 코드 불일치 팝업에서 받은 OK 입력을 검증한다."""

        interrupt = state["interrupt"]
        if not isinstance(interrupt, dict):
            raise ValueError("복원된 HITL 입력 요청이 없습니다.")

        hitl_type = state.get("hitl_type")
        if hitl_type != "AGENT_CODE_MISMATCH":
            raise ValueError(
                f"에이전트 코드 검증 노드에 잘못 진입했습니다: {hitl_type}"
            )

        errors = validate_ok_signal(state.get("human_input"))
        if errors:
            logger.info(
                "======== HITL 입력 검증 실패 | 유형=%s | 오류=%s",
                hitl_type,
                errors,
            )
            refreshed = build_hitl_request(
                hitl_type=hitl_type,
                message=str(interrupt["message"]),
                fields=list(interrupt["fields"]),
                context=dict(interrupt["context"]),
                errors=errors,
            )
            return {
                "status": "INPUT_REQUIRED",
                "approved": False,
                "interrupt": refreshed,
            }

        logger.info("======== HITL 입력 검증 완료 | 유형=%s", hitl_type)
        return {
            "status": "PASS",
            "approved": True,
            "interrupt": None,
        }

    @staticmethod
    @timed("HITL 입력 결과 라우팅")
    def _after_input_decision(
        state: MasterState,
    ) -> Literal["save_hitl_state", "persist_user_message"]:
        """입력이 필요하면 Redis 저장, 통과하면 다음 업무 단계로 보낸다."""

        if state["status"] == "INPUT_REQUIRED":
            return "save_hitl_state"
        return "persist_user_message"

    @timed(
        "일반 Redis HITL 상태 저장",
        expected_exceptions=(HitlStateStoreUnavailableError,),
    )
    async def _save_hitl_state(self, state: MasterState) -> MasterState:
        """다음 HTTP 요청에 필요한 그래프 상태만 일반 Redis에 저장한다."""

        interrupt = state["interrupt"]
        if not isinstance(interrupt, dict):
            raise ValueError("Redis에 저장할 HITL 입력 요청이 없습니다.")

        # Redis에는 재개에 꼭 필요한 값만 명시적으로 선택해 저장한다. 상태 전체를
        # 그대로 저장하면 LangGraph 노드가 늘어날 때 임시 값이나 불필요한 사용자
        # 정보까지 자동으로 영속화될 수 있으므로 허용 목록 방식을 사용한다.
        stored_state = {
            "thread_id": state["thread_id"],
            "message_id": state["message_id"],
            "employee_id": state["employee_id"],
            "conversation_id": state["conversation_id"],
            "frontend_agent_code": state.get("frontend_agent_code"),
            "classification": state["classification"],
        }
        await self._hitl_store.save(
            thread_id=state["thread_id"],
            hitl_type=str(interrupt["type"]),
            graph_state=stored_state,
            interrupt=interrupt,
        )
        logger.info(
            "======== HITL 대기 상태 저장 완료 | thread_id=%s | 유형=%s",
            state["thread_id"],
            interrupt["type"],
        )
        return {}

    @timed("사용자 질문 Redis 저장")
    async def _persist_user_message(
        self,
        state: MasterState,
    ) -> MasterState:
        """보정된 질문을 최종 분류된 에이전트의 대화 이력에 저장한다."""

        classification = IntentClassification.model_validate(
            state["classification"]
        )
        assert classification.agent_code is not None

        logger.info(
            "======== Redis 대화 저장 요청 | 사원번호=%s | "
            "conversation_id=%s | 에이전트=%s | 역할=user",
            state["employee_id"],
            state["conversation_id"],
            classification.agent_code,
        )
        await self._history_store.append_message(
            employee_id=state["employee_id"],
            conversation_id=state["conversation_id"],
            agent_code=classification.agent_code,
            role="user",
            content=classification.refined_query,
            message_id=f"{state['message_id']}:user",
        )
        logger.info("======== Redis 대화 저장 단계 완료")
        return {}

    @timed("대화 저장 이후 라우팅")
    def _after_message_persist(
        self,
        state: MasterState,
    ) -> Literal["run_subagent", "clear_hitl_state", "end"]:
        """등록된 에이전트는 서브에이전트로, 나머지는 종료로 보낸다."""

        classification = IntentClassification.model_validate(
            state["classification"]
        )
        if (
            classification.agent_code is not None
            and self._subagent_router.supports(classification.agent_code)
        ):
            logger.info(
                "======== 서브에이전트 라우팅 | 에이전트=%s | 실행=예",
                classification.agent_code,
            )
            return "run_subagent"

        if state.get("entry_stage") == "HITL_RESUME":
            return "clear_hitl_state"
        return "end"

    @timed("시나리오 서브에이전트 실행")
    async def _run_subagent(self, state: MasterState) -> MasterState:
        """마스터가 선택한 에이전트의 시나리오와 파라미터를 분류한다."""

        classification = IntentClassification.model_validate(
            state["classification"]
        )
        assert classification.agent_code is not None
        logger.info(
            "======== 서브에이전트 실행 시작 | 에이전트=%s | 질문=%s",
            classification.agent_code,
            classification.refined_query,
        )
        started_at = perf_counter()
        try:
            result = await self._subagent_router.classify(
                agent_code=classification.agent_code,
                query=classification.refined_query,
            )
        except Exception as exc:
            # 서브에이전트의 일시적 LLM 오류나 예상하지 못한 구조화 응답 때문에
            # 이미 완료된 마스터 의도분류까지 500으로 바꾸지 않는다. 이 경우
            # subagent만 null로 두고 마스터 분류 결과는 정상 응답한다.
            logger.info(
                "======== 서브에이전트 실행 생략 | 마스터 결과로 계속 응답 | "
                "에이전트=%s | 오류유형=%s | 오류=%s",
                classification.agent_code,
                type(exc).__name__,
                exc,
            )
            self._trace_recorder.record(
                "서브에이전트의도분류오류",
                state,
                elapsed_seconds=perf_counter() - started_at,
                error=exc,
            )
            return {"subagent": None}
        if result is None:
            logger.info(
                "======== 서브에이전트 실행 결과 없음 | 에이전트=%s",
                classification.agent_code,
            )
            return {"subagent": None}
        update = {"subagent": result.model_dump(mode="json")}
        traced_state = {**state, **update}
        elapsed = perf_counter() - started_at
        self._trace_recorder.record(
            "서브에이전트의도분류완료",
            traced_state,
            elapsed_seconds=elapsed,
        )
        self._trace_recorder.record("시나리오의도분류완료", traced_state)
        return update

    @staticmethod
    @timed("서브에이전트 이후 라우팅")
    def _after_subagent(
        state: MasterState,
    ) -> Literal["call_mcp", "clear_hitl_state", "end"]:
        """분류 결과가 있으면 공통 MCP 단계로, 없으면 실행을 마무리한다."""

        if state.get("subagent") is not None:
            logger.info("======== 서브에이전트 이후 라우팅 | MCP 호출 단계")
            return "call_mcp"
        return MasterIntentGraph._completion_route(state)

    @timed("MCP 도구 실행")
    async def _call_mcp(self, state: MasterState) -> MasterState:
        """세부 시나리오 manifest의 도구를 추적 가능한 JSON-RPC id로 호출한다."""

        subagent_data = state.get("subagent")
        if subagent_data is None:
            return {"mcp": None}
        subagent = SubagentResult.model_validate(subagent_data)
        started_at = perf_counter()
        try:
            result = await self._mcp_executor.execute(
                subagent=subagent,
                employee_id=state["employee_id"],
                conversation_id=state["conversation_id"],
                thread_id=state["thread_id"],
            )
        except Exception as exc:
            self._trace_recorder.record(
                "MCP도구호출오류",
                state,
                elapsed_seconds=perf_counter() - started_at,
                error=exc,
            )
            raise
        if result is None:
            logger.info(
                "======== MCP 실행 결과 없음 | 에이전트=%s | 세부시나리오=%s",
                subagent.agent_code,
                subagent.detail_scenario_code,
            )
            return {"mcp": None}
        logger.info(
            "======== MCP 실행 완료 | 도구=%s | 추적ID=%s | 성공=%s",
            result.tool_name,
            result.request_id,
            result.succeeded,
        )
        update = {"mcp": result.model_dump(mode="json")}
        # 현재 executor가 manifest에서 실제 tool_name을 결정하므로 실행 결과가
        # 만들어진 직후에 선택 행을 남겨야 정확한 도구명이 CSV에 포함된다.
        self._trace_recorder.record("MCP도구선택완료", {**state, **update})
        self._trace_recorder.record(
            "MCP도구호출완료",
            {**state, **update},
            elapsed_seconds=perf_counter() - started_at,
        )
        return update

    @staticmethod
    @timed("MCP 이후 라우팅")
    def _after_mcp_call(
        state: MasterState,
    ) -> Literal["clear_hitl_state", "end"]:
        """MCP의 structuredContent dict를 최종 결과로 두고 실행을 끝낸다."""

        return MasterIntentGraph._completion_route(state)

    @staticmethod
    def _completion_route(
        state: MasterState,
    ) -> Literal["clear_hitl_state", "end"]:
        """HITL 재진입이면 저장 상태를 정리하고 신규 요청이면 종료한다."""

        if state.get("entry_stage") == "HITL_RESUME":
            return "clear_hitl_state"
        return "end"

    @timed(
        "일반 Redis HITL 상태 정리",
        expected_exceptions=(HitlStateStoreUnavailableError,),
    )
    async def _clear_hitl_state(self, state: MasterState) -> MasterState:
        """승인 완료 후 더 이상 필요 없는 HITL 상태를 삭제한다."""

        await self._hitl_store.delete(state["thread_id"])
        return {}

    @timed(
        "LangGraph 신규 실행",
        expected_exceptions=(HitlStateStoreUnavailableError,),
    )
    async def start(
        self,
        *,
        thread_id: str,
        employee_id: str,
        conversation_id: str,
        message: str,
        frontend_agent_code: str | None,
    ) -> MasterResult:
        """새 질문에 대한 stateless 그래프 실행을 시작한다."""

        logger.info(
            "======== LangGraph 신규 시작 | thread_id=%s | "
            "conversation_id=%s",
            thread_id,
            conversation_id,
        )
        input_state: MasterState = {
                "entry_stage": "NEW_CHAT",
                "thread_id": thread_id,
                "message": message,
                "message_id": thread_id,
                "employee_id": employee_id,
                "conversation_id": conversation_id,
                "frontend_agent_code": (
                    frontend_agent_code.upper()
                    if frontend_agent_code is not None
                    else None
                ),
            }
        self._trace_recorder.record("요청도착", input_state)
        state = await self._graph.ainvoke(input_state)
        self._trace_recorder.record("요청처리완료", state)
        return self._to_result(thread_id, state)

    @timed(
        "LangGraph Redis HITL 재진입",
        expected_exceptions=(
            HitlStateNotFoundError,
            HitlStateStoreUnavailableError,
        ),
    )
    async def resume(self, *, thread_id: str, value: Any) -> MasterResult:
        """일반 Redis 상태를 복원해 HITL 검증 Edge부터 새로 실행한다."""

        logger.info(
            "======== Redis HITL 재개 시작 | thread_id=%s | 입력=%s",
            thread_id,
            value,
        )
        entry = await self._hitl_store.get(thread_id)
        if entry is None:
            raise HitlStateNotFoundError(thread_id)

        restored_state: MasterState = dict(entry.graph_state)
        restored_state.update(
            {
                "entry_stage": "HITL_RESUME",
                "hitl_type": entry.hitl_type,
                "thread_id": thread_id,
                "human_input": value,
                "interrupt": entry.interrupt,
            }
        )
        self._trace_recorder.record("HITL재진입", restored_state)
        logger.info(
            "======== Redis HITL 그래프 상태 복원 | thread_id=%s | "
            "유형=%s | 진입단계=%s | 복원상태=%s",
            thread_id,
            entry.hitl_type,
            restored_state["entry_stage"],
            restored_state,
        )
        state = await self._graph.ainvoke(restored_state)
        self._trace_recorder.record("요청처리완료", state)
        return self._to_result(thread_id, state)

    @staticmethod
    def _to_result(thread_id: str, state: MasterState) -> MasterResult:
        """일반 Redis HITL 요청을 포함한 그래프 상태를 API 결과로 변환한다."""

        classification = IntentClassification.model_validate(
            state["classification"]
        )
        status = state["status"]
        interrupt = state.get("interrupt")
        subagent_data = state.get("subagent")
        mcp_data = state.get("mcp")
        logger.info(
            "======== 그래프 결과 변환 | 상태=%s | thread_id=%s",
            status,
            thread_id,
        )
        return MasterResult(
            status=status,
            thread_id=thread_id,
            classification=classification,
            interrupt=interrupt if status == "INPUT_REQUIRED" else None,
            subagent=(
                SubagentResult.model_validate(subagent_data)
                if subagent_data is not None
                else None
            ),
            mcp=(
                McpExecutionResult.model_validate(mcp_data)
                if mcp_data is not None
                else None
            ),
        )
