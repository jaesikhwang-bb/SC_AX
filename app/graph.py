"""일반 Redis HITL 상태를 사용하는 마스터 에이전트 LangGraph 워크플로."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.classifier import IntentClassifier
from app.csv_trace import EmptyTraceRecorder, TraceRecorder
from app.domain import ClassificationType, IntentClassification
from app.history import ChatHistoryStore
from app.hitl import build_hitl_request, validate_input_value, validate_switch_agent
from app.hitl_store import (
    HitlStateNotFoundError,
    HitlStateStore,
    HitlStateStoreUnavailableError,
)
from app.mcp.client import EmptyMcpToolExecutor, McpToolExecutor
from app.mcp.models import MCP_SAFE_ERROR_MESSAGE, McpExecutionResult
from app.mcp.result_adapters import adapt_mcp_result
from app.mcp.scenario_runtime import ScenarioMcpHandlerContext
from app.mcp.scenarios import qualification, rp
from app.mcp.scenarios.registry import (
    get_scenario_handler_spec,
    run_scenario_handler,
)
from app.observability import log_failure_diagnostic, logger, timed
from app.scenario_actions import ScenarioActionRequired, get_scenario_action
from app.subagents.models import SubagentClassificationStatus, SubagentResult
from app.subagents.fixed_responses import (
    UNSUPPORTED_FEATURE_MESSAGE,
    get_subagent_fixed_response,
)
from app.subagents.router import EmptySubagentRouter, SubagentRouter


AGENT_DISPLAY_NAMES = {
    "PERFORMANCE_FEE": "실적·수수료",
    "FEE_STANDARD": "수수료 기준",
    "QUALIFICATION": "자격 기준",
    "RP": "RP",
    "PRODUCT_GUIDE": "상품 안내",
    "TABLET": "태블릿",
}
AGENT_SWITCH_CANCELLED_MESSAGE = "전환을 취소했습니다. 이어서 도와드리겠습니다."


def _post_answer_interrupt_for_results(
    subagent: SubagentResult,
    results: list[McpExecutionResult],
) -> dict[str, Any] | None:
    """시나리오 handler가 선언한 답변 후 action을 Redis 재개 계약으로 만든다."""
    query_matches = [
        (index, match) for index, match in enumerate(subagent.matches)
        if get_subagent_fixed_response(subagent.agent_code, match.detail_scenario_code) is None
    ]
    if any(not result.succeeded or result.outcome == "ERROR" or result.error for result in results):
        return None
    for (match_index, _), result in zip(query_matches, results):
        action = result.post_answer_action
        if not isinstance(action, Mapping):
            continue
        action_code = str(action.get("action_code", "")).strip()
        definition = get_scenario_action(
            subagent.agent_code,
            subagent.matches[match_index].detail_scenario_code,
            action_code,
        )
        if definition is None:
            raise ValueError(f"답변 후 action 정의가 없습니다: {action_code}")
        return build_hitl_request(
            hitl_type="MCP_PARAMETER_REQUIRED",
            action_code=definition.action_code,
            message=str(action.get("message") or definition.message),
            fields=definition.frontend_fields(),
            context={
                "agent_code": subagent.agent_code,
                "scenario_code": subagent.matches[match_index].scenario_code,
                "detail_scenario_code": subagent.matches[match_index].detail_scenario_code,
                "match_index": match_index,
                "input_code": definition.inputs[0].input_code,
                "parameter_name": definition.inputs[0].parameter_name,
                "scenario_action_code": definition.action_code,
                "action_source": "post_answer_mcp_handler",
                **dict(action.get("context") or {}),
            },
        )
    return None


class MasterState(TypedDict, total=False):
    """한 번의 stateless 그래프 실행에서 노드 사이에 전달되는 상태."""

    # NEW_CHAT은 최초 진입, HITL_RESUME은 Redis 상태 복원 후 재진입을 뜻한다.
    entry_stage: str
    thread_id: str
    message: str
    message_id: str
    employee_id: str
    session_id: str
    frontend_agent_code: str | None
    # 현재 HTTP 요청에서만 사용하는 MCP 런타임 정보다. AccessToken 등이 포함될
    # 수 있으므로 Redis HITL 상태와 CSV에는 저장하지 않는다. HITL 재진입 때는
    # 재진입 HTTP 요청의 최신 컨텍스트를 다시 주입한다.
    request_context: dict[str, Any]
    history: list[dict[str, Any]]
    # 최종 분류된 agent_code 범위에서 다시 조회한 RAG 답변용 과거 대화다.
    # 현재 질문은 별도 refined_query로 전달하므로 이 배열에는 포함하지 않는다.
    chat_history: list[dict[str, Any]]
    # 에이전트 전환 취소 등 직접 답변.
    direct_answer: str
    classification: dict[str, Any]
    # 같은 요청에서 문맥 보정/최종 판정을 반복하지 않는다.
    context_classified: bool
    subagent: dict[str, Any] | None
    mcp: dict[str, Any] | None
    # 모든 세부 시나리오의 중간/최종 MCP step 결과. 개발 추적과 다음 step의
    # argument 매핑에 사용하며 최종 답변 정렬에는 직접 사용하지 않는다.
    mcp_workflow_results: list[dict[str, Any]]
    mcp_results: list[dict[str, Any]]
    # 복수 MCP 중 추가 입력이 필요한 순번이다. 재진입 시 완료된 이전 도구를
    # 다시 호출하지 않고 이 순번부터 이어서 실행한다.
    mcp_start_index: int
    # Redis에 저장된 대기 유형이다. HITL 재진입 시 START 조건부 Edge가 이 값을
    # 사용해 유형별 검증 노드로 직접 분기한다.
    hitl_type: str
    human_input: Any
    interrupt: dict[str, Any] | None
    # 답변 token을 모두 전송한 뒤 표시할 후속 action이다. 현재 RP 아파트의
    # NEXT_PAGE처럼 MCP 성공 결과를 먼저 보여줘야 하는 경우에만 사용한다.
    post_answer_interrupt: dict[str, Any] | None
    status: str
    approved: bool
    # SWITCH_AGENT=no 응답을 일반 direct_answer와 구분해 API가 분류된
    # agent_code를 전송하지 않도록 하는 요청 단위 상태다.
    agent_switch_cancelled: bool


def _request_project_code(state: Mapping[str, Any]) -> str | None:
    """현재 요청의 서비스 별칭을 Redis namespace 전달값으로 반환한다."""

    request_context = state.get("request_context")
    if not isinstance(request_context, Mapping):
        return None
    return str(request_context.get("endpoint", "")).strip() or None


def _intent_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """분류에 필요한 본문과 추천질문만 남기고 화면용 metadata는 제외한다."""

    normalized: list[dict[str, Any]] = []
    for item in history:
        entry: dict[str, Any] = {
            "role": str(item.get("role", "")),
            "content": str(item.get("content", "")),
        }
        metadata = item.get("metadata")
        if isinstance(metadata, Mapping):
            recommended_questions = metadata.get("recommendedQuestions")
            if isinstance(recommended_questions, list):
                entry["metadata"] = {
                    "recommendedQuestions": recommended_questions,
                }
        normalized.append(entry)
    return normalized


def _should_preserve_shared_composite_frontend_agent(
    *,
    frontend_agent_code: str,
    classified_agent_code: str | None,
    original_query: str,
    refined_query: str,
) -> bool:
    """RP·실적수수료 공통 복합환산이면 사용자가 선택한 화면을 유지한다.

    두 에이전트가 동일하게 지원하는 환산점수·실적 건수와 미반영 내역에만
    적용한다. 수수료, 세금, 아파트 문서 안내 등 한쪽만 지원하는 질문은 기존
    마스터 분류와 HITL 비교를 그대로 사용한다.
    """

    shared_agents = {"RP", "PERFORMANCE_FEE"}
    if {
        str(frontend_agent_code).strip().upper(),
        str(classified_agent_code or "").strip().upper(),
    } != shared_agents:
        return False

    compact_query = "".join(
        f"{original_query} {refined_query}".casefold().split()
    )
    if any(term in compact_query for term in ("복합환산", "복합판매")):
        return True

    if any(
        term in compact_query
        for term in (
            "환산미반영",
            "실적미반영",
            "미반영내역",
            "실적제외",
            "자동이체미연결",
            "자동차연계",
            "삼성전자매출",
        )
    ):
        return True

    return "rp" in compact_query and any(
        term in compact_query
        for term in ("환산점수", "실적", "실적건수", "미반영", "제외")
    )


@dataclass(frozen=True)
class MasterResult:
    """FastAPI 응답으로 변환하기 전의 프레임워크 독립적인 그래프 결과."""

    status: Literal["PASS", "INPUT_REQUIRED", "EXCEPTION"]
    thread_id: str
    classification: IntentClassification
    interrupt: dict | None = None
    post_answer_interrupt: dict | None = None
    subagent: SubagentResult | None = None
    mcp: McpExecutionResult | None = None
    mcp_workflow_results: list[McpExecutionResult] | None = None
    mcp_results: list[McpExecutionResult] | None = None
    direct_answer: str | None = None
    chat_history: list[dict[str, Any]] = field(default_factory=list)
    # API가 user.id로 계산한 유치조직구분코드다. 서브에이전트 고정답변을
    # 11(일반)/12(제휴)/13(복합)으로 분기할 때 사용한다.
    recruitment_org_type_code: str = ""
    # AccessToken을 제거한 요청 사용자 문맥이다. 직원정보 선조회에서 채운
    # ``user.name``을 고정답변과 최종 RAG 답변에서 사용할 수 있다.
    request_user: dict[str, Any] = field(default_factory=dict)
    agent_switch_cancelled: bool = False


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
        # 현재는 오류 원인을 즉시 확인하기 위해 외부 호출 노드에 RetryPolicy를
        # 지정하지 않는다(None = 한 번만 실행). 추후 재시도가 필요하면 이 세
        # 노드에 langgraph.types.RetryPolicy를 명시적으로 주입하면 된다.
        builder.add_node("classify_intent", self._classify_intent)
        builder.add_node("verify_selection", self._verify_selection)
        builder.add_node(
            "validate_agent_code_mismatch",
            self._validate_agent_code_mismatch,
        )
        builder.add_node(
            "validate_mcp_parameter_input",
            self._validate_mcp_parameter_input,
        )
        builder.add_node("save_hitl_state", self._save_hitl_state)
        builder.add_node("save_post_answer_hitl_state", self._save_post_answer_hitl_state)
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
                "validate_agent_code_mismatch": ("validate_agent_code_mismatch"),
                "validate_mcp_parameter_input": ("validate_mcp_parameter_input"),
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
                "clear_hitl_state": "clear_hitl_state",
            },
        )
        builder.add_conditional_edges(
            "validate_agent_code_mismatch",
            self._after_input_decision,
            {
                "save_hitl_state": "save_hitl_state",
                "persist_user_message": "persist_user_message",
                "clear_hitl_state": "clear_hitl_state",
            },
        )
        builder.add_conditional_edges(
            "validate_mcp_parameter_input",
            self._after_mcp_parameter_input,
            {
                "save_hitl_state": "save_hitl_state",
                "clear_hitl_state": "clear_hitl_state",
                "call_mcp": "call_mcp",
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
                "save_hitl_state": "save_hitl_state",
                "save_post_answer_hitl_state": "save_post_answer_hitl_state",
                "clear_hitl_state": "clear_hitl_state",
                "end": END,
            },
        )
        builder.add_edge("clear_hitl_state", END)
        builder.add_edge("save_post_answer_hitl_state", END)
        builder.add_edge("finish_exception", END)

        # Checkpointer를 전달하지 않는다. 상태 지속성은 HitlStateStore가 필요한
        # 필드만 일반 Redis SET/GET/DEL로 직접 관리한다.
        self._graph = builder.compile()
        logger.info(
            "======== LangGraph 컴파일 완료 | Checkpointer=사용안함 | "
            "외부호출노드재시도=0회 | 대상노드=%s | "
            "향후설정위치=app/graph.py:MasterIntentGraph.__init__",
            [
                "classify_intent",
                "classify_intent_with_history",
                "run_subagent",
                "call_mcp",
            ],
        )

    @staticmethod
    @timed("그래프 진입 단계 라우팅")
    def _route_entry(
        state: MasterState,
    ) -> Literal[
        "load_history",
        "validate_agent_code_mismatch",
        "validate_mcp_parameter_input",
    ]:
        """신규 요청과 Redis HITL 유형에 맞는 시작 Edge를 선택한다."""

        entry_stage = state.get("entry_stage", "NEW_CHAT")
        if entry_stage == "NEW_CHAT":
            logger.info("======== 진입 라우팅 | 신규 채팅 분류 단계")
            return "load_history"
        if entry_stage == "HITL_RESUME":
            hitl_type = state.get("hitl_type")
            if hitl_type == "AGENT_CODE_MISMATCH":
                logger.info(
                    "======== 진입 라우팅 | HITL유형=%s | 에이전트 코드 변경 검증 단계",
                    hitl_type,
                )
                return "validate_agent_code_mismatch"

            if hitl_type == "MCP_PARAMETER_REQUIRED":
                logger.info(
                    "======== 진입 라우팅 | HITL유형=%s | MCP 필수 파라미터 검증 단계",
                    hitl_type,
                )
                return "validate_mcp_parameter_input"

            raise ValueError(f"지원하지 않는 Redis HITL 유형입니다: {hitl_type}")
        raise ValueError(f"지원하지 않는 entry_stage입니다: {entry_stage}")

    @timed("Redis 대화이력 조회")
    async def _load_history(self, state: MasterState) -> MasterState:
        """선택된 에이전트의 이력만 조회한다. 미선택 시 이력을 사용하지 않는다."""

        frontend_code = str(state.get("frontend_agent_code") or "").strip()
        if not frontend_code:
            # 범위를 식별할 수 없으면 다른 에이전트의 최근 이력을 대신 읽지 않는다.
            update: MasterState = {"history": []}
            self._trace_recorder.record("대화이력조회완료", {**state, **update})
            return update

        logger.info(
            "======== Redis 이력 조회 요청 | 에이전트=%s | 최대개수=%d",
            frontend_code,
            self._history_limit,
        )
        history = await self._history_store.get_recent(
            state["employee_id"],
            state["session_id"],
            frontend_code,
            self._history_limit,
            include_metadata=False,
            project_code=_request_project_code(state),
        )
        history = _intent_history(history)
        logger.info(
            "======== Redis 이력 조회 완료 | 에이전트=%s | 조회개수=%d",
            frontend_code,
            len(history),
        )
        update = {"history": history}
        self._trace_recorder.record("대화이력조회완료", {**state, **update})
        return update

    @timed("마스터 에이전트 1차 의도분류")
    async def _classify_intent(self, state: MasterState) -> MasterState:
        """클릭한 추천질문도 일반 사용자 질문과 같은 보정·분류를 거친다."""

        logger.info(
            "======== 1차 의도 분류 시작 | "
            "Redis조회이력=%d개 | 프론트에이전트전달=안함",
            len(state["history"]),
        )
        # 이력의 유무는 보정 모드만 결정한다. 보정과 최종 분류는 각각 한 번이다.
        return await self._classify_intent_with_history(state)

    @timed("마스터 에이전트 대화문맥 보정 분류")
    async def _classify_intent_with_history(
        self,
        state: MasterState,
    ) -> MasterState:
        """동일 범위 이력으로 먼저 보정한 질문을 최종 분류한다."""

        history = list(state.get("history", []))
        context_mode = "HISTORY_ALLOWED" if history else "CURRENT_ONLY"
        logger.info(
            "======== 문맥 보정 분류 시작 | 모드=%s | "
            "동일에이전트이력=%d개 | "
            "프론트에이전트전달=안함",
            context_mode,
            len(history),
        )
        started_at = perf_counter()
        try:
            result = await self._classifier.classify(
                state["message"],
                history,
                context_mode,
            )
        except Exception as exc:
            log_failure_diagnostic(
                stage="LangGraph 대화문맥 보정 의도분류 노드",
                code_location=(
                    "app/graph.py:MasterIntentGraph._classify_intent_with_history"
                ),
                exc=exc,
                likely_cause=("마스터 보정·분류 LLM 호출 또는 구조화 결과 변환 실패"),
                corrective_action=(
                    "동일 에이전트 Redis 이력과 HISTORY_ALLOWED 프롬프트 규칙을 "
                    "확인하세요."
                ),
                retry_count=0,
                context={
                    "graph_node": "classify_intent_with_history",
                    "context_mode": context_mode,
                    "history_count": len(history),
                    "message_length": len(str(state.get("message") or "")),
                },
            )
            self._trace_recorder.record(
                "문맥보정의도분류오류",
                state,
                elapsed_seconds=perf_counter() - started_at,
                error=exc,
            )
            raise

        if result.classification_type == ClassificationType.CONTEXT_REQUIRED:
            # HISTORY_ALLOWED에서는 CONTEXT_REQUIRED를 최종 결과로 허용하지
            # 않는다. 이력이 있든 없든 현재 질문을 AGENT/실제 예외로 확정하지
            # 못했다는 뜻이므로 외부 고정답변 계약인 EMPTY_QUERY로 종료한다.
            result = result.model_copy(
                update={"classification_type": ClassificationType.EMPTY_QUERY}
            )
            logger.info(
                "======== 문맥 보정 실패 | 이력개수=%d | "
                "현재질문과이력으로복원불가 | CONTEXT_REQUIRED→EMPTY_QUERY",
                len(history),
            )

        update = {
            "classification": result.model_dump(mode="json"),
            "context_classified": True,
        }
        logger.info(
            "======== 문맥 보정 분류 완료 | 모드=%s | "
            "분류유형=%s | 에이전트=%s | 보정질문길이=%d",
            context_mode,
            result.classification_type.value,
            result.agent_code,
            len(result.refined_query),
        )
        self._trace_recorder.record(
            "문맥보정의도분류완료",
            {**state, **update},
            elapsed_seconds=perf_counter() - started_at,
        )
        return update

    @staticmethod
    @timed("의도분류 결과 라우팅")
    def _after_classification(
        state: MasterState,
    ) -> Literal["verify_selection", "finish_exception"]:
        """최종 판정을 그대로 라우팅한다. 예외를 정상 업무로 재시도하지 않는다."""

        classification = IntentClassification.model_validate(state["classification"])
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

        # 주소를 기다리던 중 불완전한 일반 message가 들어온 경우에는 조회를
        # 시도하지 않았으므로 무데이터가 아닌 주소 재입력 안내를 반환한다.
        # 다른 업무로 정상 분류된 요청은 이 노드를 거치지 않는다.
        if (
            state.get("request_context", {}).get("rp_apartment_pending")
            and IntentClassification.model_validate(state["classification"]).classification_type
            == ClassificationType.EMPTY_QUERY
        ):
            return {
                "status": "EXCEPTION", "approved": False, "interrupt": None,
                "direct_answer": rp.APARTMENT_RP_ADDRESS_RETRY_MESSAGE,
            }

        logger.info(
            "======== 예외 분류 종료 | 유형=%s | Redis저장=안함",
            state["classification"]["classification_type"],
        )
        logger.info(
            "FLOW 마스터 예외 종료 | 유형=%s | MCP호출=생략 | Redis저장=안함",
            state["classification"]["classification_type"],
        )
        return {
            "status": "EXCEPTION",
            "approved": False,
            "interrupt": None,
        }

    @timed("프론트 선택 코드 비교")
    def _verify_selection(self, state: MasterState) -> MasterState:
        """프론트 선택 코드와 분류 결과를 비교해 전환 확인 여부를 결정한다.

        프론트에서 agent_code를 보내지 않은 경우도 임의로 자동 진입시키지 않는다.
        마스터가 분류한 에이전트로 이동해도 되는지 ``SWITCH_AGENT`` action으로
        확인받은 뒤, yes 응답을 받은 요청에서만 다음 단계로 진행한다.
        """

        classification = IntentClassification.model_validate(state["classification"])
        raw_frontend_code = state.get("frontend_agent_code")
        # API 요청 모델에서 null·빈 문자열·공백을 None으로 정규화하지만, 그래프를
        # 직접 호출하는 경로에서도 동일하게 동작하도록 여기서 한 번 더 보정한다.
        frontend_code = (
            str(raw_frontend_code).strip().upper() if raw_frontend_code is not None else ""
        ) or None
        frontend_agent_unselected = frontend_code is None

        if (
            frontend_code is not None
            and classification.agent_code != frontend_code
            and _should_preserve_shared_composite_frontend_agent(
                frontend_agent_code=frontend_code,
                classified_agent_code=classification.agent_code,
                original_query=state.get("message", ""),
                refined_query=classification.refined_query,
            )
        ):
            previous_agent_code = classification.agent_code
            classification = classification.model_copy(
                update={"agent_code": frontend_code}
            )
            update = {
                "classification": classification.model_dump(mode="json"),
                "status": "PASS",
                "approved": True,
                "interrupt": None,
            }
            logger.info(
                "======== 공통 복합환산 에이전트 유지\n"
                "프론트선택=%s\n마스터LLM원본분류=%s\n최종에이전트=%s\n"
                "HITL=생략\n보정질문길이=%d",
                frontend_code,
                previous_agent_code,
                frontend_code,
                len(classification.refined_query),
            )
            logger.info(
                "FLOW 에이전트 선택 비교 | 프론트선택=%s | 분류=%s | "
                "결과=공통업무유지",
                frontend_code,
                previous_agent_code,
            )
            self._trace_recorder.record(
                "공통복합환산에이전트유지",
                {**state, **update},
            )
            return update

        logger.info(
            "======== 에이전트 코드 비교 | 프론트=%s | LLM분류=%s",
            frontend_code,
            classification.agent_code,
        )

        if frontend_code is not None and classification.agent_code == frontend_code:
            logger.info(
                "FLOW 에이전트 선택 비교 | 프론트선택=%s | 분류=%s | 결과=PASS",
                frontend_code,
                classification.agent_code,
            )
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

        target_agent_name = AGENT_DISPLAY_NAMES.get(
            classification.agent_code or "",
            classification.agent_code or "해당",
        )
        action_message = (
            f"질문이 '{target_agent_name}' 업무로 분류되었습니다. "
            "해당 업무로 전환하시겠습니까?"
            if frontend_agent_unselected
            else (
                "현재 다른 업무를 진행 중입니다. "
                f"'{target_agent_name}' 업무로 전환하시겠습니까?"
            )
        )
        interrupt = build_hitl_request(
            hitl_type="AGENT_CODE_MISMATCH",
            action_code="SWITCH_AGENT",
            message=action_message,
            fields=[
                {
                    "name": "SWITCH_AGENT",
                    "label": "에이전트 전환 여부",
                    "type": "choice",
                    "required": True,
                    "allowed_values": ["yes", "no"],
                }
            ],
            context={
                "frontend_agent_code": frontend_code,
                "classified_agent_code": classification.agent_code,
            },
        )
        logger.info(
            "======== 에이전트 선택 확인 필요 | Redis HITL 저장 단계 | "
            "프론트=%s | 변경대상=%s",
            frontend_code or "미선택",
            classification.agent_code,
        )
        logger.info(
            "FLOW 에이전트 선택 비교 | 프론트선택=%s | 분류=%s | "
            "결과=SWITCH_AGENT 요청",
            frontend_code or "미선택",
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

        decision, errors = validate_switch_agent(state.get("human_input"))
        if errors:
            logger.info(
                "FLOW HITL 처리 | 유형=%s | 결과=입력재요청",
                hitl_type,
            )
            logger.info(
                "======== HITL 입력 검증 실패 | 유형=%s | 오류=%s",
                hitl_type,
                errors,
            )
            refreshed = build_hitl_request(
                hitl_type=hitl_type,
                action_code="SWITCH_AGENT",
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

        if decision == "no":
            logger.info("FLOW HITL 처리 | 유형=%s | 결정=no | 결과=전환취소", hitl_type)
            logger.info(
                "======== 에이전트 전환 취소 | 기존화면=유지 | "
                "서브에이전트호출=안함 | MCP호출=안함"
            )
            return {
                "status": "PASS",
                "approved": False,
                "interrupt": None,
                "direct_answer": AGENT_SWITCH_CANCELLED_MESSAGE,
                "agent_switch_cancelled": True,
            }

        logger.info("======== HITL 입력 검증 완료 | 유형=%s | 결정=yes", hitl_type)
        logger.info("FLOW HITL 처리 | 유형=%s | 결정=yes | 결과=진행", hitl_type)
        return {
            "status": "PASS",
            "approved": True,
            "interrupt": None,
            "agent_switch_cancelled": False,
        }

    @staticmethod
    @timed("MCP 필수 파라미터 사용자 입력 검증")
    def _validate_mcp_parameter_input(state: MasterState) -> MasterState:
        """Python action으로 받은 값을 대기 중인 match 파라미터에 반영한다."""

        interrupt = state.get("interrupt")
        if not isinstance(interrupt, dict):
            raise ValueError("복원된 MCP 파라미터 입력 요청이 없습니다.")
        if state.get("hitl_type") != "MCP_PARAMETER_REQUIRED":
            raise ValueError(
                f"MCP 파라미터 검증 노드에 잘못 진입했습니다: {state.get('hitl_type')}"
            )

        context = dict(interrupt.get("context", {}))
        input_code = str(context.get("input_code", ""))
        parameter_name = str(context.get("parameter_name", ""))
        match_index = int(context.get("match_index", -1))
        scenario_action_code = str(context.get("scenario_action_code", ""))
        scenario_action = None
        normalized_values: dict[str, str] = {}
        if scenario_action_code:
            scenario_action = get_scenario_action(
                str(context.get("agent_code", "")),
                str(context.get("detail_scenario_code", "")),
                scenario_action_code,
            )
            if scenario_action is None:
                errors = {
                    "action": (
                        "서버에 등록된 action 함수를 찾을 수 없습니다. "
                        "새 질문으로 다시 시작해 주세요."
                    )
                }
            else:
                normalized_values, errors = scenario_action.validate_submission(
                    state.get("human_input")
                )
        elif not input_code or not parameter_name:
            errors = {input_code or "input": "입력 설정을 확인해 주세요."}
        else:
            normalized_value, errors = validate_input_value(
                state.get("human_input"),
                input_code=input_code,
                expected_value=context.get("expected_value"),
                pattern=context.get("pattern"),
                min_length=context.get("min_length"),
                max_length=context.get("max_length"),
                allowed_values=context.get("allowed_values"),
                validation_message=context.get("validation_message"),
            )
            normalized_values = {parameter_name: normalized_value}

        if errors:
            logger.info(
                "FLOW HITL 처리 | 유형=MCP_PARAMETER_REQUIRED | action=%s | "
                "결과=입력재요청",
                scenario_action_code or input_code,
            )
            logger.info(
                "======== MCP 파라미터 입력 검증 실패 | 입력코드=%s | "
                "파라미터=%s | 오류=%s",
                input_code,
                parameter_name,
                errors,
            )
            refreshed = build_hitl_request(
                hitl_type="MCP_PARAMETER_REQUIRED",
                action_code=(
                    scenario_action.action_code
                    if scenario_action is not None
                    else str(
                        interrupt.get("action_code")
                        or interrupt.get("type")
                        or "MCP_PARAMETER_REQUIRED"
                    )
                ),
                # MCP 결과를 반영해 동적으로 만든 안내문도 재검증 응답에서 유지한다.
                message=str(
                    interrupt.get("message", "필수값을 입력해 주세요.")
                ),
                fields=(
                    scenario_action.frontend_fields()
                    if scenario_action is not None
                    else list(interrupt.get("fields", []))
                ),
                context=context,
                errors=errors,
            )
            return {
                "status": "INPUT_REQUIRED",
                "approved": False,
                "interrupt": refreshed,
            }

        if scenario_action is not None and any(
            str(value).casefold() in scenario_action.complete_on_values
            for value in normalized_values.values()
        ):
            logger.info(
                "======== HITL 종료 선택 | action=%s | MCP호출=생략",
                scenario_action.action_code,
            )
            return {
                "status": "PASS",
                "approved": False,
                "interrupt": None,
                "direct_answer": "다음 페이지 조회를 종료했습니다.",
                "agent_switch_cancelled": True,
            }

        subagent = SubagentResult.model_validate(state["subagent"])
        if match_index < 0 or match_index >= len(subagent.matches):
            raise ValueError(f"MCP 재개 match_index가 올바르지 않습니다: {match_index}")

        match = subagent.matches[match_index]
        # 답변 뒤 NEXT_PAGE action은 LLM을 다시 호출하지 않는다. Redis interrupt의
        # cursor만 검증된 세부 시나리오 parameter로 복원해 같은 handler를 재사용한다.
        pagination_parameters: dict[str, Any] = {}
        if context.get("action_source") == "post_answer_mcp_handler":
            pagination_parameters = {
                key: context.get(key, "")
                for key in (
                    "emdNm",
                    "atrRgno",
                    "inqrDvC",
                    "inqrCt",
                    "nextEtxtKeyCn",
                    "no2NextKeyCn",
                    "page_number",
                )
            }
            if pagination_parameters.get("emdNm"):
                pagination_parameters["address"] = pagination_parameters["emdNm"]
            if pagination_parameters.get("inqrDvC"):
                pagination_parameters["address_type"] = pagination_parameters["inqrDvC"]
        updated_parameters = {
            **match.parameters,
            **pagination_parameters,
            **normalized_values,
        }
        match.parameters = updated_parameters
        if match_index == 0:
            subagent.parameters = dict(updated_parameters)

        resumed_workflow_results = list(
            state.get("mcp_workflow_results", [])
        )
        resumed_terminal_results = list(state.get("mcp_results", []))
        if scenario_action is not None and scenario_action.invalidate_step_codes:
            handler_spec = get_scenario_handler_spec(
                subagent.agent_code,
                match.detail_scenario_code,
            )
            handler_code = handler_spec.code if handler_spec is not None else None
            invalidated_steps = set(scenario_action.invalidate_step_codes)
            resumed_workflow_results = [
                item
                for item in resumed_workflow_results
                if not (
                    isinstance(item, Mapping)
                    and item.get("workflow_handler_code") == handler_code
                    and item.get("workflow_step_code") in invalidated_steps
                )
            ]
            resumed_terminal_results = [
                item
                for item in resumed_terminal_results
                if not (
                    isinstance(item, Mapping)
                    and item.get("workflow_handler_code") == handler_code
                    and item.get("workflow_step_code") in invalidated_steps
                )
            ]
            logger.info(
                "======== Python action MCP 체크포인트 무효화 | action=%s | "
                "handler=%s | step=%s",
                scenario_action.action_code,
                handler_code,
                sorted(invalidated_steps),
            )

        logger.info(
            "======== Python action 입력 검증 완료 | action=%s | "
            "파라미터키=%s | 입력값본문로그=생략 | 재개순번=%d",
            scenario_action_code or "LEGACY_MCP_PARAMETER_REQUIRED",
            sorted(normalized_values),
            match_index,
        )
        logger.info(
            "FLOW HITL 처리 | 유형=MCP_PARAMETER_REQUIRED | action=%s | "
            "결과=MCP재개",
            scenario_action_code or input_code,
        )
        return {
            "status": "PASS",
            "approved": True,
            "interrupt": None,
            "subagent": subagent.model_dump(mode="json"),
            "mcp_start_index": match_index,
            "mcp_workflow_results": resumed_workflow_results,
            "mcp_results": resumed_terminal_results,
        }

    @staticmethod
    @timed("MCP 파라미터 입력 결과 라우팅")
    def _after_mcp_parameter_input(
        state: MasterState,
    ) -> Literal["save_hitl_state", "clear_hitl_state", "call_mcp"]:
        """입력이 유효하면 LLM 재호출 없이 대기 중 MCP부터 재개한다."""

        if state["status"] == "INPUT_REQUIRED":
            return "save_hitl_state"
        if state.get("agent_switch_cancelled"):
            return "clear_hitl_state"
        return "call_mcp"

    @staticmethod
    @timed("HITL 입력 결과 라우팅")
    def _after_input_decision(
        state: MasterState,
    ) -> Literal["save_hitl_state", "persist_user_message", "clear_hitl_state"]:
        """대기 상태는 저장하고, 전환 취소는 HITL만 정리한 뒤 종료한다."""

        if state["status"] == "INPUT_REQUIRED":
            return "save_hitl_state"
        if state.get("agent_switch_cancelled"):
            return "clear_hitl_state"
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
            "session_id": state["session_id"],
            "frontend_agent_code": state.get("frontend_agent_code"),
            "classification": state["classification"],
        }
        if interrupt["type"] == "MCP_PARAMETER_REQUIRED":
            # MCP 파라미터 재입력은 마스터/서브 LLM을 다시 호출하지 않는다.
            # 선택된 시나리오, 이미 완료된 MCP 결과와 재개 순번만 추가 저장한다.
            stored_state.update(
                {
                    "subagent": state["subagent"],
                    "mcp_workflow_results": state.get("mcp_workflow_results", []),
                    "mcp_results": state.get("mcp_results", []),
                    "mcp": state.get("mcp"),
                    "mcp_start_index": state.get("mcp_start_index", 0),
                    "status": "INPUT_REQUIRED",
                    "approved": False,
                }
            )
        await self._hitl_store.save(
            thread_id=state["thread_id"],
            hitl_type=str(interrupt["type"]),
            graph_state=stored_state,
            interrupt=interrupt,
            project_code=_request_project_code(state),
        )
        logger.info(
            "======== HITL 대기 상태 저장 완료 | 유형=%s",
            interrupt["type"],
        )
        # LangGraph 0.2는 노드가 선언된 상태 키를 하나도 쓰지 않으면
        # InvalidUpdateError를 발생시킨다. 저장소 반영이 주목적인 노드라도
        # 현재 HITL 상태를 명시적으로 반환해 유효한 상태 갱신으로 처리한다.
        return {"status": "INPUT_REQUIRED", "approved": False}

    @timed("사용자 질문 Redis 저장")
    async def _persist_user_message(
        self,
        state: MasterState,
    ) -> MasterState:
        """보정된 질문을 최종 분류된 에이전트의 대화 이력에 저장한다."""

        classification = IntentClassification.model_validate(state["classification"])
        assert classification.agent_code is not None

        # 최초 의도분류 이력은 프론트가 선택한 agent 기준일 수 있다. 최종 RAG
        # 답변에는 다른 서브에이전트 이력이 섞이지 않도록 분류된 agent_code로
        # 현재 질문 저장 전에 다시 읽는다. Redis 저장 형식은 변경하지 않는다.
        chat_history = await self._history_store.get_recent(
            state["employee_id"],
            state["session_id"],
            classification.agent_code,
            self._history_limit,
            include_metadata=False,
            project_code=_request_project_code(state),
        )
        chat_history = _intent_history(chat_history)
        logger.info(
            "======== RAG 답변용 대화이력 준비\n"
            "에이전트=%s\n과거대화개수=%d",
            classification.agent_code,
            len(chat_history),
        )
        logger.info(
            "======== Redis 대화 저장 요청 | 에이전트=%s | 역할=user | "
            "보정질문길이=%d | 입력가드레일=통과후데이터",
            classification.agent_code,
            len(classification.refined_query),
        )
        await self._history_store.append_message(
            employee_id=state["employee_id"],
            session_id=state["session_id"],
            agent_code=classification.agent_code,
            role="user",
            content=classification.refined_query,
            message_id=f"{state['message_id']}:user",
            project_code=_request_project_code(state),
        )
        logger.info("======== Redis 대화 저장 단계 완료")
        # Redis 저장은 부수효과지만 LangGraph 상태 노드는 최소 한 개의 상태
        # 키를 반환해야 한다. 기존 message_id를 유지하는 쓰기를 명시한다.
        return {
            "message_id": state["message_id"],
            "chat_history": chat_history,
        }

    @timed("대화 저장 이후 라우팅")
    def _after_message_persist(
        self,
        state: MasterState,
    ) -> Literal["run_subagent", "clear_hitl_state", "end"]:
        """등록된 에이전트는 서브에이전트로, 나머지는 종료로 보낸다."""

        classification = IntentClassification.model_validate(state["classification"])
        if classification.agent_code is not None and self._subagent_router.supports(
            classification.agent_code
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

        classification = IntentClassification.model_validate(state["classification"])
        assert classification.agent_code is not None
        logger.info(
            "======== 서브에이전트 실행 시작 | 에이전트=%s | 보정질문길이=%d",
            classification.agent_code,
            len(classification.refined_query),
        )
        started_at = perf_counter()
        try:
            result = await self._subagent_router.classify(
                agent_code=classification.agent_code,
                query=classification.refined_query,
            )
        except Exception as exc:
            log_failure_diagnostic(
                stage="LangGraph 서브에이전트 시나리오 분류 노드",
                code_location="app/graph.py:MasterIntentGraph._run_subagent",
                exc=exc,
                likely_cause=(
                    "선택된 서브에이전트의 LLM 호출, 시나리오 매칭 또는 "
                    "파라미터 구조화 처리 실패"
                ),
                corrective_action=(
                    "바로 앞의 서브에이전트 LLM 실패 진단, 해당 "
                    "prompts/subagents 폴더와 app/subagents/router.py를 확인하세요."
                ),
                retry_count=0,
                context={
                    "graph_node": "run_subagent",
                    "agent_code": classification.agent_code,
                    "refined_query": classification.refined_query,
                },
            )
            self._trace_recorder.record(
                "서브에이전트의도분류오류",
                state,
                elapsed_seconds=perf_counter() - started_at,
                error=exc,
            )
            # 오류를 정상 마스터 결과로 숨기지 않고 SSE error까지 즉시 전달한다.
            raise
        if result is None:
            logger.info(
                "======== 서브에이전트 실행 결과 없음 | 에이전트=%s",
                classification.agent_code,
            )
            return {"subagent": None}
        update: MasterState = {"subagent": result.model_dump(mode="json")}
        pending_address = state.get("request_context", {}).get(
            "rp_apartment_pending"
        ) if isinstance(state.get("request_context"), Mapping) else None
        if (
            isinstance(pending_address, Mapping)
            and result.agent_code == "RP"
            and str(pending_address.get("address_type", "")) in {"1", "2"}
        ):
            for matched in result.matches:
                if matched.detail_scenario_code == "APARTMENT_RP_LIST":
                    matched.parameters = {
                        **matched.parameters,
                        "address_type": str(pending_address["address_type"]),
                    }
            result.synchronize_primary_match()
            update["subagent"] = result.model_dump(mode="json")
            logger.info(
                "======== RP 아파트 주소구분 상태 결합 | 주소구분=%s | 매칭=%s",
                pending_address["address_type"],
                [item.detail_scenario_code for item in result.matches],
            )
        if result.status == SubagentClassificationStatus.UNSUPPORTED:
            update.update(
                {
                    "status": "EXCEPTION",
                    "approved": False,
                    "interrupt": None,
                    "direct_answer": UNSUPPORTED_FEATURE_MESSAGE,
                }
            )
            logger.info(
                "FLOW 미지원 기능 종료 | 에이전트=%s | "
                "세부시나리오=없음 | MCP호출=생략 | 고정답변=%s",
                result.agent_code,
                UNSUPPORTED_FEATURE_MESSAGE,
            )
        logger.info(
            "======== 서브에이전트 실행 완료 | 에이전트=%s | 매칭개수=%d | "
            "세부시나리오=%s",
            result.agent_code,
            len(result.matches),
            [match.detail_scenario_code for match in result.matches],
        )
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
        """조회 시나리오만 MCP로 보내고 고정 답변만 있으면 바로 종료한다."""

        subagent_data = state.get("subagent")
        if subagent_data is not None:
            subagent = SubagentResult.model_validate(subagent_data)
            if subagent.status == SubagentClassificationStatus.UNSUPPORTED:
                logger.info(
                    "FLOW 서브에이전트 분기 | 에이전트=%s | "
                    "분류상태=UNSUPPORTED | 다음단계=고정답변종료",
                    subagent.agent_code,
                )
                return MasterIntentGraph._completion_route(state)
            fixed_details = [
                match.detail_scenario_code
                for match in subagent.matches
                if get_subagent_fixed_response(
                    subagent.agent_code,
                    match.detail_scenario_code,
                )
                is not None
            ]
            requires_mcp = len(fixed_details) < len(subagent.matches)
            logger.info(
                "======== 서브에이전트 이후 라우팅\n"
                "에이전트=%s\n전체매칭개수=%d\n고정답변세부시나리오=%s\n"
                "MCP필요=%s",
                subagent.agent_code,
                len(subagent.matches),
                fixed_details,
                requires_mcp,
            )
            if requires_mcp:
                logger.info(
                    "FLOW 서브에이전트 분기 | 에이전트=%s | 매칭개수=%d | "
                    "다음단계=MCP",
                    subagent.agent_code,
                    len(subagent.matches),
                )
                return "call_mcp"
            logger.info(
                "FLOW 서브에이전트 분기 | 에이전트=%s | 매칭개수=%d | "
                "다음단계=고정답변",
                subagent.agent_code,
                len(subagent.matches),
            )
            return MasterIntentGraph._completion_route(state)
        return MasterIntentGraph._completion_route(state)

    def _record_mcp_workflow_result(
        self,
        *,
        state: MasterState,
        workflow_results: list[McpExecutionResult],
        stage: str,
    ) -> None:
        """각 단일/fan-out/fan-in 결과 직후 개발 추적 state를 기록한다."""

        self._trace_recorder.record(
            stage,
            {
                **state,
                "mcp_workflow_results": [
                    item.model_dump(mode="json") for item in workflow_results
                ],
            },
        )

    async def _apply_rag_batch_second_search(
        self,
        *,
        state: MasterState,
        subagent: SubagentResult,
        refined_query: str,
        results: list[McpExecutionResult],
        workflow_results: list[McpExecutionResult],
    ) -> None:
        """각 RAG 에이전트 파일의 명시적 함수로 2차 검색 여부를 결정한다.

        공통 RAG policy/registry는 사용하지 않는다. QUALIFICATION의 기준은
        qualification.py, RP의 기준은 rp.py에서 직접 확인하고 수정한다.
        PERFORMANCE_FEE를 포함한 나머지 agent는 즉시 종료한다.
        """

        agent_code = subagent.agent_code.strip().upper()
        if agent_code not in {"QUALIFICATION", "RP"}:
            return

        bindings: list[tuple[int, Any, int, McpExecutionResult, Any]] = []
        result_cursor = 0
        for match_index, match in enumerate(subagent.matches):
            if get_subagent_fixed_response(
                subagent.agent_code,
                match.detail_scenario_code,
            ) is not None:
                continue
            if result_cursor >= len(results):
                break
            execution = results[result_cursor]
            handler_spec = get_scenario_handler_spec(
                subagent.agent_code,
                match.detail_scenario_code,
            )
            if handler_spec is not None and handler_spec.rag_output_handler is not None:
                bindings.append(
                    (
                        match_index,
                        match,
                        result_cursor,
                        execution,
                        handler_spec,
                    )
                )
            result_cursor += 1

        if not bindings:
            return

        decision_input = [
            (str(match.detail_scenario_code), execution)
            for _, match, _, execution, _ in bindings
        ]
        if agent_code == "QUALIFICATION":
            decision = qualification.qualification_second_search_decision(
                decision_input
            )
            character_counter = qualification.qualification_document_character_count
        else:
            decision = rp.rp_second_search_decision(decision_input)
            character_counter = rp.rp_document_character_count

        detail_character_counts = decision["detail_character_counts"]
        total_character_count = decision["total_character_count"]
        character_threshold = decision["character_threshold"]
        all_initial_searches_succeeded = decision[
            "all_first_searches_succeeded"
        ]
        already_second = decision["already_second"]
        should_search_again = decision["should_search_again"]
        logger.info(
            "======== RAG 요청 전체 문서 길이 판정\n"
            "에이전트=%s\nRAG세부시나리오=%s\n세부글자수=%s\n"
            "전체글자수=%d\n글자수임계값=%d\n1차검색전체성공=%s\n"
            "이미2차결과=%s\n2차검색필요=%s",
            subagent.agent_code,
            [match.detail_scenario_code for _, match, _, _, _ in bindings],
            detail_character_counts,
            total_character_count,
            character_threshold,
            all_initial_searches_succeeded,
            already_second,
            should_search_again,
        )
        logger.info(
            "FLOW RAG 검색 분기 | 에이전트=%s | 세부시나리오수=%d | "
            "전처리후전체글자수=%d | 임계값=%d | 다음검색=%s",
            subagent.agent_code,
            len(bindings),
            total_character_count,
            character_threshold,
            "SECOND" if should_search_again else "FIRST_RESULT_USE",
        )
        self._trace_recorder.record(
            "RAG전체문서길이판정",
            {
                **state,
                "rag_batch_trace": {
                    "agentCode": subagent.agent_code,
                    "detailCharacterCounts": detail_character_counts,
                    "totalCharacterCount": total_character_count,
                    "characterThreshold": character_threshold,
                    "secondSearchRequired": should_search_again,
                },
            },
        )
        if not should_search_again:
            return

        for match_index, match, result_index, _, handler_spec in bindings:
            single = SubagentResult(
                agent_code=subagent.agent_code,
                prompt_version=subagent.prompt_version,
                scenario_code=match.scenario_code,
                scenario_name=match.scenario_name,
                detail_scenario_code=match.detail_scenario_code,
                detail_scenario_name=match.detail_scenario_name,
                parameters=match.parameters,
                matches=[match],
            )
            second_handler_code = f"{handler_spec.code}.second"
            restored_second_results = [
                item
                for item in workflow_results
                if item.workflow_handler_code == second_handler_code
            ]
            second_context = ScenarioMcpHandlerContext(
                handler_code=second_handler_code,
                executor=self._mcp_executor,
                subagent=single,
                employee_id=state["employee_id"],
                session_id=state["session_id"],
                thread_id=state["thread_id"],
                refined_query=refined_query,
                request_context=state.get("request_context", {}),
                rag_search_pass="SECOND",
                initial_results=restored_second_results,
            )
            logger.info(
                "======== RAG 2차 문서 검색 시작\n"
                "에이전트=%s\n순번=%d\n세부시나리오=%s\nhandler=%s",
                subagent.agent_code,
                match_index,
                match.detail_scenario_code,
                second_handler_code,
            )
            second_outcome = await run_scenario_handler(
                spec=handler_spec,
                context=second_context,
            )
            workflow_results[:] = [
                item
                for item in workflow_results
                if item.workflow_handler_code != second_handler_code
            ]
            workflow_results.extend(second_outcome.results)
            second_result = adapt_mcp_result(
                execution=second_outcome.terminal,
                subagent=single,
                employee_id=state["employee_id"],
                session_id=state["session_id"],
                thread_id=state["thread_id"],
                request_context=state.get("request_context", {}),
                workflow_results=second_outcome.results,
            )
            for workflow_index in range(len(workflow_results) - 1, -1, -1):
                workflow_result = workflow_results[workflow_index]
                if (
                    workflow_result.request_id == second_result.request_id
                    and workflow_result.workflow_handler_code
                    == second_handler_code
                ):
                    workflow_results[workflow_index] = second_result
                    break
            results[result_index] = second_result
            logger.info(
                "======== RAG 2차 문서 검색 완료\n"
                "에이전트=%s\n순번=%d\n세부시나리오=%s\n"
                "결과상태=%s\n2차문서글자수=%d\n최종답변결과=2차검색",
                subagent.agent_code,
                match_index,
                match.detail_scenario_code,
                second_result.outcome,
                character_counter(second_result),
            )
            logger.info(
                "FLOW RAG 2차 검색 완료 | 에이전트=%s | 세부시나리오=%s | "
                "상태=%s | 전처리후글자수=%d",
                subagent.agent_code,
                match.detail_scenario_code,
                second_result.outcome,
                character_counter(second_result),
            )
            if not second_result.succeeded or second_result.outcome == "ERROR":
                break

        self._record_mcp_workflow_result(
            state=state,
            workflow_results=workflow_results,
            stage="RAG2차문서검색완료",
        )

    @timed("MCP 도구 실행")
    async def _call_mcp(self, state: MasterState) -> MasterState:
        """세부 시나리오별 Python handler로 MCP 도구를 호출한다."""

        subagent_data = state.get("subagent")
        if subagent_data is None:
            return {"mcp": None}
        subagent = SubagentResult.model_validate(subagent_data)
        classification_data = state.get("classification")
        refined_query = (
            IntentClassification.model_validate(classification_data).refined_query
            if classification_data is not None
            else str(state.get("message", "")).strip()
        )
        started_at = perf_counter()
        results = [
            McpExecutionResult.model_validate(item)
            for item in state.get("mcp_results", [])
        ]
        workflow_results = [
            McpExecutionResult.model_validate(item)
            for item in state.get("mcp_workflow_results", [])
        ]
        start_index = int(state.get("mcp_start_index", 0))
        if start_index < 0 or start_index > len(subagent.matches):
            raise ValueError(f"MCP 시작 순번이 올바르지 않습니다: {start_index}")
        logger.info(
            "======== MCP 실행 범위 | 전체매칭=%d | 시작순번=%d | "
            "기완료최종결과=%d | 기완료workflow단계=%d",
            len(subagent.matches),
            start_index,
            len(results),
            len(workflow_results),
        )
        try:
            for match_index in range(start_index, len(subagent.matches)):
                match = subagent.matches[match_index]
                fixed_response = get_subagent_fixed_response(
                    subagent.agent_code,
                    match.detail_scenario_code,
                )
                if fixed_response is not None:
                    request_context = state.get("request_context", {})
                    recruitment_org_type_code = str(
                        request_context.get("recruitment_org_type_code", "")
                        if isinstance(request_context, Mapping)
                        else ""
                    ).strip()
                    selected_message = fixed_response.message_for(
                        recruitment_org_type_code,
                        user_name=(
                            request_context.get("user", {}).get("name")
                            if isinstance(request_context.get("user"), Mapping)
                            else None
                        ),
                    )
                    logger.info(
                        "======== 서브에이전트 모집인 구분별 고정 답변 선택\n"
                        "순번=%d\n에이전트=%s\n세부시나리오=%s\n"
                        "유치조직구분코드=%s\nMCP호출=생략\n고정답변길이=%d",
                        match_index,
                        subagent.agent_code,
                        match.detail_scenario_code,
                        recruitment_org_type_code or "없음(default_message 사용)",
                        len(selected_message),
                    )
                    continue
                # 각 매칭은 registry에 등록된 Python handler가 독립적으로 실행한다.
                single = SubagentResult(
                    agent_code=subagent.agent_code,
                    prompt_version=subagent.prompt_version,
                    scenario_code=match.scenario_code,
                    scenario_name=match.scenario_name,
                    detail_scenario_code=match.detail_scenario_code,
                    detail_scenario_name=match.detail_scenario_name,
                    parameters=match.parameters,
                    matches=[match],
                )
                function_handler = get_scenario_handler_spec(
                    single.agent_code,
                    single.detail_scenario_code,
                )
                if function_handler is None:
                    raise RuntimeError(
                        "등록된 MCP 시나리오 handler가 없습니다: "
                        f"agent={single.agent_code}, "
                        f"detail={single.detail_scenario_code}"
                    )
                logger.info(
                    "======== MCP 개별 실행 시작 | 순번=%d | 시나리오=%s | "
                    "세부시나리오=%s | 실행방식=Python함수 | "
                    "handler=%s | 파라미터키=%s",
                    match_index,
                    match.scenario_code,
                    match.detail_scenario_code,
                    function_handler.code,
                    sorted(match.parameters),
                )
                scenario_workflow_results: list[McpExecutionResult] = []
                result: McpExecutionResult | None = None
                handler_context: ScenarioMcpHandlerContext | None = None
                try:
                    self._trace_recorder.record(
                        "MCP함수핸들러시작",
                        {
                            **state,
                            "mcp_handler_trace": {
                                "handlerCode": function_handler.code,
                                "agentCode": single.agent_code,
                                "detailScenarioCode": single.detail_scenario_code,
                                "codeLocation": (
                                    f"{function_handler.handler.__module__}:"
                                    f"{function_handler.handler.__name__}"
                                ),
                            },
                        },
                    )

                    def record_function_trace(
                        stage: str,
                        payload: dict[str, Any],
                        partial_results: Any,
                    ) -> None:
                        self._trace_recorder.record(
                            stage,
                            {
                                **state,
                                "mcp_handler_trace": payload,
                                "mcp_workflow_results": [
                                    item.model_dump(mode="json")
                                    for item in (
                                        *workflow_results,
                                        *partial_results,
                                    )
                                ],
                            },
                        )

                    restored_handler_results = [
                        item
                        for item in workflow_results
                        if item.workflow_handler_code == function_handler.code
                    ]
                    handler_context = ScenarioMcpHandlerContext(
                        handler_code=function_handler.code,
                        executor=self._mcp_executor,
                        subagent=single,
                        employee_id=state["employee_id"],
                        session_id=state["session_id"],
                        thread_id=state["thread_id"],
                        refined_query=refined_query,
                        request_context=state.get("request_context", {}),
                        initial_results=restored_handler_results,
                        trace_callback=record_function_trace,
                    )
                    handler_outcome = await run_scenario_handler(
                        spec=function_handler,
                        context=handler_context,
                    )
                    scenario_workflow_results.extend(handler_outcome.results)
                    workflow_results = [
                        item
                        for item in workflow_results
                        if item.workflow_handler_code != function_handler.code
                    ]
                    workflow_results.extend(handler_outcome.results)
                    result = handler_outcome.terminal
                    logger.info(
                        "======== MCP Python handler 완료 | handler=%s | "
                        "세부시나리오=%s | step수=%d | 원장결과수=%d | "
                        "terminal=%s | outcome=%s",
                        function_handler.code,
                        single.detail_scenario_code,
                        max(
                            (
                                item.workflow_step_count
                                for item in handler_outcome.results
                            ),
                            default=0,
                        ),
                        len(handler_outcome.results),
                        result.workflow_step_code,
                        result.outcome,
                    )
                except ScenarioActionRequired as required:
                    if handler_context is not None:
                        # MCP 뒤 action이 발생해도 그 직전까지의 결과를 Redis에
                        # 체크포인트로 남긴다. 재진입 시 동일 step은 재호출하지 않는다.
                        workflow_results = [
                            item
                            for item in workflow_results
                            if item.workflow_handler_code != function_handler.code
                        ]
                        workflow_results.extend(handler_context.results)
                    action_code = required.definition.action_code
                    action_fields = required.definition.frontend_fields()
                    initial_errors = required.errors
                    scenario_action_code = required.definition.action_code
                    interrupt = build_hitl_request(
                        hitl_type="MCP_PARAMETER_REQUIRED",
                        action_code=action_code,
                        message=required.message,
                        fields=action_fields,
                        context={
                            "agent_code": subagent.agent_code,
                            "scenario_code": match.scenario_code,
                            "detail_scenario_code": (match.detail_scenario_code),
                            "match_index": match_index,
                            "input_code": required.input_code,
                            "parameter_name": required.parameter_name,
                            "expected_value": required.expected_value,
                            "pattern": required.pattern,
                            "min_length": required.min_length,
                            "max_length": required.max_length,
                            "allowed_values": required.allowed_values,
                            "validation_message": (required.validation_message),
                            "sensitive": required.sensitive,
                            "scenario_action_code": scenario_action_code,
                            "action_source": "python_scenario_handler",
                            "handler_code": function_handler.code,
                            "code_location": (
                                f"{function_handler.handler.__module__}:"
                                f"{function_handler.handler.__name__}"
                            ),
                            **required.context,
                        },
                        errors=initial_errors,
                    )
                    serialized_results = [
                        item.model_dump(mode="json") for item in results
                    ]
                    serialized_workflow_results = [
                        item.model_dump(mode="json") for item in workflow_results
                    ]
                    update: MasterState = {
                        "status": "INPUT_REQUIRED",
                        "approved": False,
                        "interrupt": interrupt,
                        "subagent": subagent.model_dump(mode="json"),
                        "mcp_start_index": match_index,
                        "mcp": (serialized_results[0] if serialized_results else None),
                        "mcp_workflow_results": serialized_workflow_results,
                        "mcp_results": serialized_results,
                    }
                    logger.info(
                        "======== MCP 필수 파라미터 누락 | 순번=%d | "
                        "세부시나리오=%s | 입력코드=%s | 파라미터=%s | "
                        "안내문길이=%d",
                        match_index,
                        match.detail_scenario_code,
                        required.input_code,
                        required.parameter_name,
                        len(required.message),
                    )
                    logger.info(
                        "FLOW HITL 요청 | 유형=MCP_PARAMETER_REQUIRED | "
                        "에이전트=%s | 세부시나리오=%s | action=%s",
                        subagent.agent_code,
                        match.detail_scenario_code,
                        action_code,
                    )
                    self._trace_recorder.record(
                        "MCP파라미터입력요청",
                        {**state, **update},
                    )
                    return update
                if result is not None:
                    # 원본 structuredContent는 result에 유지하고 조회형 시나리오는
                    # 별도의 query.v1 정제 결과와 고정 답변을 생성한다. RAG는
                    # raw.rag로 표시하고 원본 문서 구조를 그대로 다음 단계에 넘긴다.
                    try:
                        result = adapt_mcp_result(
                            execution=result,
                            subagent=single,
                            employee_id=state["employee_id"],
                            session_id=state["session_id"],
                            thread_id=state["thread_id"],
                            request_context=state.get("request_context", {}),
                            workflow_results=scenario_workflow_results,
                        )
                    except Exception as exc:
                        # MCP 호출은 성공했더라도 결과 컬럼·data 형식 또는 포맷
                        # 함수에서 오류가 날 수 있다. 원인은 로그에 상세히 남기고
                        # 해당 시나리오는 안전한 고정 답변으로 계속 진행한다.
                        log_failure_diagnostic(
                            stage="MCP 조회 결과 정제",
                            code_location=(
                                "app/graph.py:MasterIntentGraph._call_mcp -> "
                                "app/mcp/result_adapters.py:adapt_mcp_result"
                            ),
                            exc=exc,
                            likely_cause=(
                                "MCP 원본 결과 구조 또는 세부 시나리오 output handler의 "
                                "전처리·답변·renderable 반환 계약 불일치"
                            ),
                            corrective_action=(
                                "app/mcp/scenarios/registry.py의 output_handler와 "
                                "해당 app/mcp/scenarios 파일의 output 함수를 확인하세요."
                            ),
                            retry_count=0,
                            context={
                                "agent_code": single.agent_code,
                                "detail_scenario_code": (single.detail_scenario_code),
                                "tool_name": result.tool_name,
                                "mcp_request_id": result.request_id,
                            },
                        )
                        result = result.model_copy(
                            update={
                                "succeeded": False,
                                "outcome": "ERROR",
                                "user_message": MCP_SAFE_ERROR_MESSAGE,
                                "formatted_result": None,
                                "error": str(exc),
                            }
                        )
                    else:
                        formatted = result.formatted_result
                        output_handler_code = (
                            formatted.get("output_handler_code")
                            if isinstance(formatted, Mapping)
                            else None
                        )
                        self._trace_recorder.record(
                            "MCP함수결과전처리완료",
                            {
                                **state,
                                "mcp_output_trace": {
                                    "agentCode": single.agent_code,
                                    "detailScenarioCode": (
                                        single.detail_scenario_code
                                    ),
                                    "outputHandlerCode": output_handler_code,
                                    "resultFormat": result.result_format,
                                    "preprocessedData": (
                                        formatted.get("data")
                                        if isinstance(formatted, Mapping)
                                        else None
                                    ),
                                    "renderableCount": (
                                        len(formatted.get("renderables", []))
                                        if isinstance(formatted, Mapping)
                                        and isinstance(
                                            formatted.get("renderables"), list
                                        )
                                        else 0
                                    ),
                                },
                                "mcp_workflow_results": [
                                    item.model_dump(mode="json")
                                    for item in workflow_results
                                ],
                                "mcp_results": [
                                    item.model_dump(mode="json")
                                    for item in (*results, result)
                                ],
                            },
                        )
                    # workflow 추적의 마지막 step도 정제 결과로 교체하되,
                    # 답변용 results에는 세부 시나리오당 terminal 결과 하나만 넣는다.
                    if workflow_results and (
                        workflow_results[-1].request_id == result.request_id
                    ):
                        workflow_results[-1] = result
                    results.append(result)
                    logger.info(
                        "======== MCP 개별 실행 완료 | 도구=%s | 성공=%s | "
                        "추적ID=%s | 결과형식=%s | outcome=%s | "
                        "정제결과로그=생략 | 오류유형=%s | "
                        "원본결과로그=생략",
                        result.tool_name,
                        result.succeeded,
                        result.request_id,
                        result.result_format,
                        result.outcome,
                        "없음" if not result.error else "MCP_RESULT_ERROR",
                    )
            # 모든 detail의 1차 검색이 끝난 뒤에만 RAG 문서 전체 길이를 합산한다.
            # 임계값을 초과하면 등록된 RAG detail 전체를 2차 검색하고 답변용
            # terminal 결과를 2차 결과로 교체한다. 일반 조회 agent는 즉시 생략된다.
            await self._apply_rag_batch_second_search(
                state=state,
                subagent=subagent,
                refined_query=refined_query,
                results=results,
                workflow_results=workflow_results,
            )
        except Exception as exc:
            log_failure_diagnostic(
                stage="LangGraph MCP 다중 도구 실행 노드",
                code_location="app/graph.py:MasterIntentGraph._call_mcp",
                exc=exc,
                likely_cause=(
                    "MCP 요청 조립, GenOS HTTP 호출, 원본 응답 변환 또는 "
                    "조회형 결과 어댑터 처리 실패"
                ),
                corrective_action=(
                    "바로 앞의 MCP 실패 진단과 app/mcp/scenarios의 해당 "
                    "세부 시나리오, app/mcp/client.py와 "
                    "app/mcp/result_adapters.py를 확인하세요."
                ),
                retry_count=0,
                context={
                    "graph_node": "call_mcp",
                    "agent_code": subagent.agent_code,
                    "detail_scenario_codes": [
                        match.detail_scenario_code for match in subagent.matches
                    ],
                    "start_index": start_index,
                    "completed_result_count": len(results),
                },
            )
            self._trace_recorder.record(
                "MCP도구호출오류",
                {
                    **state,
                    "mcp_workflow_results": [
                        item.model_dump(mode="json") for item in workflow_results
                    ],
                    "mcp_results": [
                        item.model_dump(mode="json") for item in results
                    ],
                },
                elapsed_seconds=perf_counter() - started_at,
                error=exc,
            )
            raise
        if not results:
            logger.info(
                "======== MCP 실행 결과 없음 | 에이전트=%s | 매칭개수=%d",
                subagent.agent_code,
                len(subagent.matches),
            )
            return {
                "mcp": None,
                "mcp_workflow_results": [
                    item.model_dump(mode="json") for item in workflow_results
                ],
                "mcp_results": [],
            }
        logger.info(
            "======== MCP 다중 실행 완료 | 최종결과개수=%d | "
            "workflow단계개수=%d | 도구=%s | 성공=%s",
            len(results),
            len(workflow_results),
            [result.tool_name for result in results],
            [result.succeeded for result in results],
        )
        serialized_results = [result.model_dump(mode="json") for result in results]
        serialized_workflow_results = [
            result.model_dump(mode="json") for result in workflow_results
        ]
        update = {
            "mcp": serialized_results[0],
            "mcp_workflow_results": serialized_workflow_results,
            "mcp_results": serialized_results,
            "mcp_start_index": len(subagent.matches),
            "status": "PASS",
            "approved": True,
            "interrupt": None,
            "post_answer_interrupt": _post_answer_interrupt_for_results(
                subagent,
                results,
            ),
        }
        # 코드 기반 executor가 실제 tool_name을 결정하므로 실행 결과가 만들어진
        # 직후 선택 행을 남겨 정확한 도구명이 CSV에 포함되게 한다.
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
    ) -> Literal["save_hitl_state", "save_post_answer_hitl_state", "clear_hitl_state", "end"]:
        """추가 입력이면 저장하고, MCP 완료면 실행을 끝낸다."""

        if state.get("status") == "INPUT_REQUIRED":
            return "save_hitl_state"
        if isinstance(state.get("post_answer_interrupt"), dict):
            return "save_post_answer_hitl_state"
        return MasterIntentGraph._completion_route(state)

    async def _save_post_answer_hitl_state(self, state: MasterState) -> MasterState:
        """답변 뒤 action을 위한 최소 재개 상태를 Redis에 저장한다."""
        interrupt = state.get("post_answer_interrupt")
        if not isinstance(interrupt, dict):
            raise ValueError("답변 뒤 저장할 HITL action이 없습니다.")
        action_context = dict(interrupt.get("context", {}))
        subagent = SubagentResult.model_validate(state["subagent"])
        match = subagent.matches[int(action_context.get("match_index", 0))]
        # 다음 페이지는 해당 조회 하나만 재개한다. 원본 no1Grid, 과거 페이지,
        # 토큰은 저장하지 않고 라우팅 정보와 다음 조회 인자만 남긴다.
        match.parameters = {}
        subagent.matches = [match]
        subagent.synchronize_primary_match()
        interrupt = {**interrupt, "context": {**action_context, "match_index": 0}}
        await self._save_hitl_state({
            **state,
            "subagent": subagent.model_dump(mode="json"),
            "mcp": None,
            "mcp_results": [],
            "mcp_workflow_results": [],
            "status": "INPUT_REQUIRED",
            "approved": False,
            "interrupt": interrupt,
            "mcp_start_index": 0,
        })
        logger.info("======== 답변 후 NEXT_PAGE HITL 상태 저장 완료")
        return {"post_answer_interrupt": interrupt}

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

        await self._hitl_store.delete(
            state["thread_id"],
            project_code=_request_project_code(state),
        )
        # 삭제 자체는 부수효과이므로 현재 thread_id를 유지하는 상태 쓰기를
        # 반환해 LangGraph의 빈 업데이트 오류를 방지한다.
        return {"thread_id": state["thread_id"]}

    @timed(
        "LangGraph 신규 실행",
        expected_exceptions=(HitlStateStoreUnavailableError,),
    )
    async def start(
        self,
        *,
        thread_id: str,
        employee_id: str,
        session_id: str,
        message: str,
        frontend_agent_code: str | None,
        request_context: dict[str, Any] | None = None,
    ) -> MasterResult:
        """새 질문에 대한 stateless 그래프 실행을 시작한다."""

        logger.info(
            "======== LangGraph 신규 시작 | 질문길이=%d | 프론트에이전트=%s",
            len(message),
            frontend_agent_code or "선택하지 않음",
        )
        input_state: MasterState = {
            "entry_stage": "NEW_CHAT",
            "thread_id": thread_id,
            "message": message,
            "message_id": thread_id,
            "employee_id": employee_id,
            "session_id": session_id,
            "frontend_agent_code": (
                frontend_agent_code.upper() if frontend_agent_code is not None else None
            ),
            "request_context": dict(request_context or {}),
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
    async def resume(
        self,
        *,
        thread_id: str,
        value: Any,
        expected_employee_id: str | None = None,
        expected_session_id: str | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> MasterResult:
        """일반 Redis 상태를 복원해 HITL 검증 Edge부터 새로 실행한다.

        스트리밍 API는 요청의 사원번호와 session_id를 함께 전달한다. Redis에
        저장된 원래 요청 범위와 다르면 상태 존재 여부를 노출하지 않고 찾을 수
        없는 상태와 동일하게 처리한다. 기존 JSON API는 호환을 위해 두 검증값을
        생략할 수 있다.
        """

        logger.info(
            "======== Redis HITL 재개 시작 | 입력코드=%s",
            sorted(value) if isinstance(value, dict) else type(value).__name__,
        )
        request_project_code = str(
            (request_context or {}).get("endpoint", "")
        ).strip() or None
        entry = await self._hitl_store.get(
            thread_id,
            project_code=request_project_code,
        )
        if entry is None:
            raise HitlStateNotFoundError(thread_id)

        stored_employee_id = entry.graph_state.get("employee_id")
        stored_session_id = entry.graph_state.get("session_id")
        if (
            expected_employee_id is not None
            and stored_employee_id != expected_employee_id
        ) or (
            expected_session_id is not None and stored_session_id != expected_session_id
        ):
            logger.info(
                "======== Redis HITL 범위 불일치 | "
                "사원일치=%s | session일치=%s",
                expected_employee_id == stored_employee_id,
                expected_session_id == stored_session_id,
            )
            raise HitlStateNotFoundError(thread_id)

        restored_state: MasterState = dict(entry.graph_state)
        restored_state.update(
            {
                "entry_stage": "HITL_RESUME",
                "hitl_type": entry.hitl_type,
                "thread_id": thread_id,
                "human_input": value,
                "interrupt": entry.interrupt,
                # 최초 요청의 토큰을 Redis에서 복원하지 않는다. 현재 HITL 요청에
                # 포함된 새 토큰과 사용자 정보만 MCP 단계까지 전달한다.
                "request_context": dict(request_context or {}),
            }
        )
        self._trace_recorder.record("HITL재진입", restored_state)
        logger.info(
            "======== Redis HITL 그래프 상태 복원 | 유형=%s | "
            "진입단계=%s | 복원상태키=%s | 요청컨텍스트키=%s",
            entry.hitl_type,
            restored_state["entry_stage"],
            sorted(key for key in restored_state if key != "request_context"),
            sorted(restored_state.get("request_context", {})),
        )
        state = await self._graph.ainvoke(restored_state)
        self._trace_recorder.record("요청처리완료", state)
        return self._to_result(thread_id, state)

    @staticmethod
    def _to_result(thread_id: str, state: MasterState) -> MasterResult:
        """일반 Redis HITL 요청을 포함한 그래프 상태를 API 결과로 변환한다."""

        classification = IntentClassification.model_validate(state["classification"])
        status = state["status"]
        interrupt = state.get("interrupt")
        subagent_data = state.get("subagent")
        mcp_data = state.get("mcp")
        mcp_workflow_results_data = state.get("mcp_workflow_results", [])
        mcp_results_data = state.get("mcp_results", [])
        logger.info(
            "======== 그래프 결과 변환 | 상태=%s",
            status,
        )
        request_context = state.get("request_context", {})
        recruitment_org_type_code = str(
            request_context.get("recruitment_org_type_code", "")
            if isinstance(request_context, Mapping)
            else ""
        ).strip()
        request_user_source = (
            request_context.get("user", {})
            if isinstance(request_context, Mapping)
            else {}
        )
        request_user = (
            {
                "id": request_user_source.get("id"),
                "name": request_user_source.get("name"),
                "deptcode": request_user_source.get("deptcode"),
                "deptname": request_user_source.get("deptname"),
            }
            if isinstance(request_user_source, Mapping)
            else {}
        )
        return MasterResult(
            status=status,
            thread_id=thread_id,
            classification=classification,
            interrupt=interrupt if status == "INPUT_REQUIRED" else None,
            post_answer_interrupt=(
                state.get("post_answer_interrupt")
                if isinstance(state.get("post_answer_interrupt"), dict)
                else None
            ),
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
            mcp_workflow_results=[
                McpExecutionResult.model_validate(item)
                for item in mcp_workflow_results_data
            ],
            mcp_results=[
                McpExecutionResult.model_validate(item) for item in mcp_results_data
            ],
            direct_answer=state.get("direct_answer"),
            chat_history=list(state.get("chat_history", [])),
            recruitment_org_type_code=recruitment_org_type_code,
            request_user=request_user,
            agent_switch_cancelled=bool(
                state.get("agent_switch_cancelled", False)
            ),
        )
