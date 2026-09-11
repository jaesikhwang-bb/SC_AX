"""한 번의 질의 처리 과정을 로컬 CSV 파일에 단계별로 기록하는 모듈."""

import csv
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from app.observability import (
    emit_developer_trace,
    error_code_for_exception,
    logger,
    sanitize_log_text,
)


CSV_COLUMNS = (
    "최초요청일시",
    "최종갱신일시",
    "현재단계",
    "처리단계이력_JSON",
    "프로젝트코드",
    "thread_id",
    "session_id",
    "사원번호",
    "message_id",
    "원본질문",
    "프론트선택에이전트",
    "대화이력개수",
    "보정질문",
    "마스터분류유형",
    "마스터에이전트코드",
    "서브에이전트코드",
    "시나리오코드",
    "시나리오명",
    "세부시나리오코드",
    "세부시나리오명",
    "추출파라미터_JSON",
    "MCP도구명",
    "MCP추적ID",
    "MCP요청파라미터_JSON",
    "MCP성공여부",
    "MCP결과_JSON",
    "MCP결과형식",
    "MCP정제결과_JSON",
    "처리상태",
    "HITL유형",
    "마스터분류소요시간_초",
    "서브분류소요시간_초",
    "MCP호출소요시간_초",
    "오류유형",
    "오류내용",
)


class TraceRecorder(Protocol):
    """그래프가 CSV 구현 세부사항에 의존하지 않도록 하는 기록기 규약."""

    def record(
        self,
        stage: str,
        state: Mapping[str, Any],
        *,
        elapsed_seconds: float | None = None,
        error: Exception | None = None,
    ) -> None:
        """현재 처리 상태를 하나의 CSV 행으로 추가한다."""


class EmptyTraceRecorder:
    """CSV 기록이 비활성화됐을 때 사용하는 무동작 구현."""

    def record(
        self,
        stage: str,
        state: Mapping[str, Any],
        *,
        elapsed_seconds: float | None = None,
        error: Exception | None = None,
    ) -> None:
        _emit_developer_state_trace(
            stage=stage,
            state=state,
            elapsed_seconds=elapsed_seconds,
            error=error,
        )
        return None


class LocalCsvTraceRecorder:
    """전체 질의를 CSV 한 개에 저장하고 thread_id당 한 행으로 관리한다."""

    def __init__(self, directory: Path, project_code: str) -> None:
        self._directory = directory
        self._project_code = project_code
        # 동일 프로세스에서 같은 파일을 동시에 쓰는 경우 헤더/행이 섞이지 않게 한다.
        self._lock = threading.Lock()

    def record(
        self,
        stage: str,
        state: Mapping[str, Any],
        *,
        elapsed_seconds: float | None = None,
        error: Exception | None = None,
    ) -> None:
        """기록 실패를 채팅 오류로 전파하지 않고 로그만 남긴다."""

        _emit_developer_state_trace(
            stage=stage,
            state=state,
            elapsed_seconds=elapsed_seconds,
            error=error,
        )

        try:
            thread_id = str(state.get("thread_id") or "thread-id-없음")
            file_path = self._directory / "intent_classification_trace.csv"
            row = _build_row(
                stage=stage,
                state=state,
                project_code=self._project_code,
                elapsed_seconds=elapsed_seconds,
                error=error,
            )
            with self._lock:
                self._directory.mkdir(parents=True, exist_ok=True)
                rows = _read_rows(file_path)
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(rows)
                        if item.get("thread_id") == _safe_cell(thread_id)
                    ),
                    None,
                )
                if existing_index is None:
                    rows.append(row)
                else:
                    rows[existing_index] = _merge_row(
                        rows[existing_index],
                        row,
                    )
                # thread_id 한 행을 갱신해야 하므로 전체 파일을 임시 파일에 쓴 뒤
                # 교체한다. 로컬 진단 파일이 중간 상태로 남는 위험을 줄여 준다.
                temporary_path = file_path.with_suffix(".tmp")
                with temporary_path.open("w", encoding="utf-8-sig", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
                    writer.writeheader()
                    writer.writerows(rows)
                temporary_path.replace(file_path)
            logger.info(
                "======== 로컬 CSV 행 갱신 완료 | 단계=%s | 파일=%s",
                stage,
                file_path,
            )
        except Exception as exc:
            logger.info(
                "======== 로컬 CSV 기록 생략 | 단계=%s | 오류유형=%s",
                stage,
                type(exc).__name__,
            )


_STATE_TRACE_SOURCES: dict[str, tuple[str, str, str]] = {
    "요청도착": (
        "app/graph.py",
        "MasterIntentGraph.start",
        "최초 graph input_state 구성은 app/graph.py의 start를 수정하세요.",
    ),
    "HITL재진입": (
        "app/graph.py",
        "MasterIntentGraph.resume",
        "HITL 복원 state와 현재 요청 병합은 app/graph.py의 resume를 수정하세요.",
    ),
    "대화이력조회완료": (
        "app/graph.py",
        "MasterIntentGraph._load_history",
        "조회 범위·개수는 app/graph.py의 _load_history와 app/history.py를 수정하세요.",
    ),
    "마스터의도분류완료": (
        "app/graph.py",
        "MasterIntentGraph._classify_intent",
        "보정 질문과 1차 분류는 app/classifier.py 및 prompts/intent-classification을 수정하세요.",
    ),
    "마스터의도분류오류": (
        "app/graph.py",
        "MasterIntentGraph._classify_intent",
        "마스터 LLM 요청·구조화 결과는 app/classifier.py를 확인하세요.",
    ),
    "문맥보정이력없음": (
        "app/graph.py",
        "MasterIntentGraph._classify_intent_with_history",
        "CONTEXT_REQUIRED 질문의 동일 에이전트 이력 조회 범위는 app/graph.py의 _load_history를 확인하세요.",
    ),
    "문맥보정의도분류완료": (
        "app/graph.py",
        "MasterIntentGraph._classify_intent_with_history",
        "문맥 의존 질문의 2차 보정 규칙은 app/classifier.py와 master router 프롬프트를 수정하세요.",
    ),
    "문맥보정의도분류오류": (
        "app/graph.py",
        "MasterIntentGraph._classify_intent_with_history",
        "2차 LLM 요청과 동일 에이전트 이력은 app/classifier.py 및 app/history.py를 확인하세요.",
    ),
    "에이전트비교생략": (
        "app/graph.py",
        "MasterIntentGraph._verify_selection",
        "프론트 agent_code 비교 정책은 app/graph.py의 _verify_selection을 수정하세요.",
    ),
    "에이전트코드일치": (
        "app/graph.py",
        "MasterIntentGraph._verify_selection",
        "프론트 agent_code 비교 정책은 app/graph.py의 _verify_selection을 수정하세요.",
    ),
    "HITL입력요청": (
        "app/graph.py",
        "MasterIntentGraph._verify_selection",
        "에이전트 불일치 action은 app/graph.py와 app/hitl.py를 수정하세요.",
    ),
    "서브에이전트의도분류완료": (
        "app/graph.py",
        "MasterIntentGraph._run_subagent",
        "세부 시나리오와 추출 파라미터는 app/subagents/router.py 및 prompts/subagents를 수정하세요.",
    ),
    "시나리오의도분류완료": (
        "app/subagents/router.py",
        "ScenarioSubagent.classify",
        "시나리오 명세·파라미터는 해당 prompts/subagents manifest와 시나리오 파일을 수정하세요.",
    ),
    "서브에이전트의도분류오류": (
        "app/graph.py",
        "MasterIntentGraph._run_subagent",
        "서브에이전트 LLM 및 구조화 출력은 app/subagents/router.py를 확인하세요.",
    ),
    "MCP파라미터입력요청": (
        "app/graph.py",
        "MasterIntentGraph._call_mcp",
        "필수 MCP 파라미터와 action은 app/mcp/scenarios의 해당 handler를 수정하세요.",
    ),
    "MCP도구선택완료": (
        "app/graph.py",
        "MasterIntentGraph._call_mcp",
        "도구명과 요청 arguments는 handlerCode에 연결된 app/mcp/scenarios 함수를 수정하세요.",
    ),
    "MCP도구호출완료": (
        "app/mcp/client.py",
        "GenosMcpToolExecutor.execute",
        "MCP 호출·응답 파싱은 app/mcp/client.py, 정제 출력은 app/mcp/result_adapters.py를 수정하세요.",
    ),
    "MCP함수핸들러시작": (
        "app/mcp/scenarios/registry.py",
        "run_scenario_handler",
        "handler registry 연결과 실제 함수 위치는 mcp_handler_trace.codeLocation을 확인하세요.",
    ),
    "MCP함수호출완료": (
        "app/mcp/scenario_runtime.py",
        "ScenarioMcpHandlerContext.call",
        "도구명·arguments는 표시된 handlerCode에 연결된 app/mcp/scenarios 함수에서 수정하세요.",
    ),
    "MCP함수다중호출완료": (
        "app/mcp/scenario_runtime.py",
        "ScenarioMcpHandlerContext.call_many",
        "N개 arguments 생성은 시나리오 함수, 공통 반복/중단은 call_many를 수정하세요.",
    ),
    "MCP함수다중결과집계완료": (
        "app/mcp/scenario_runtime.py",
        "ScenarioMcpHandlerContext.call_many",
        "호출별 batches와 병합 data는 runtime 집계, 최종 업무 변환은 시나리오 formatter를 수정하세요.",
    ),
    "MCP함수페이지호출완료": (
        "app/mcp/scenario_runtime.py",
        "ScenarioMcpHandlerContext.paginate",
        "페이지 arguments와 원본 결과를 확인하고 해당 도구의 next_arguments 함수에서 key 규칙을 수정하세요.",
    ),
    "MCP함수페이지판정완료": (
        "app/mcp/scenario_runtime.py",
        "ScenarioMcpHandlerContext.paginate",
        "hasNext 판정은 공통 runtime이 호출한 도구별 next_arguments callback을 수정하세요.",
    ),
    "MCP함수페이지결과집계완료": (
        "app/mcp/scenario_runtime.py",
        "ScenarioMcpHandlerContext.paginate",
        "execution.stopReason/pageCount와 batches를 확인하고 max_pages 또는 next-key 함수를 수정하세요.",
    ),
    "MCP함수핸들러완료": (
        "app/mcp/scenarios/registry.py",
        "run_scenario_handler",
        "terminal step과 전체 호출 흐름은 handlerCode에 연결된 Python 함수를 수정하세요.",
    ),
    "MCP함수핸들러오류": (
        "app/mcp/scenarios/registry.py",
        "run_scenario_handler",
        "error와 마지막 성공 원장을 확인한 뒤 handlerCode의 Python 함수/결과 schema를 수정하세요.",
    ),
    "MCP함수결과전처리완료": (
        "app/mcp/result_adapters.py",
        "adapt_mcp_result",
        "outputHandlerCode에 연결된 app/mcp/scenarios 함수에서 원본 결과 파싱, 전처리 data, 답변과 renderable을 수정하세요.",
    ),
    "MCP도구호출오류": (
        "app/graph.py",
        "MasterIntentGraph._call_mcp",
        "payload, HTTP 응답 파싱과 결과 adapter를 순서대로 확인하세요.",
    ),
    "요청처리완료": (
        "app/graph.py",
        "MasterIntentGraph._to_result",
        "최종 graph state 변환은 app/graph.py의 _to_result를 수정하세요.",
    ),
}


def _emit_developer_state_trace(
    *,
    stage: str,
    state: Mapping[str, Any],
    elapsed_seconds: float | None,
    error: Exception | None,
) -> None:
    """CSV와 동일한 graph state를 debug SSE용으로 안전하게 복제한다."""

    source_file, source_function, hint = _STATE_TRACE_SOURCES.get(
        stage,
        (
            "app/graph.py",
            "MasterIntentGraph",
            "이 state 단계명을 app/graph.py에서 검색해 해당 노드를 수정하세요.",
        ),
    )
    safe_state = _sanitize_developer_value(dict(state))
    details = {
        "stateKeys": sorted(str(key) for key in state),
        "focus": _developer_state_focus(safe_state),
    }
    payload: dict[str, Any] = {
        "kind": "state_transition",
        "stageCode": f"STATE::{stage}",
        "stage": stage,
        "phase": "FAILED" if error is not None else "STATE",
        "source": {
            "file": source_file,
            "function": source_function,
            "line": None,
        },
        "durationMs": round((elapsed_seconds or 0.0) * 1000, 3),
        "details": details,
        "customizationHint": hint,
    }
    if error is not None:
        payload["error"] = {
            "code": error_code_for_exception(error),
            "type": type(error).__name__,
            "message": f"<{type(error).__name__}>",
        }
    emit_developer_trace(payload)


def _developer_state_focus(state: Any) -> dict[str, Any]:
    """개발 추적 화면에 본문 없이 분류·MCP 핵심 메타데이터만 제공한다."""

    if not isinstance(state, Mapping):
        return {}
    classification = _mapping(state.get("classification"))
    subagent = _mapping(state.get("subagent"))
    matches = [
        _mapping(item)
        for item in subagent.get("matches", [])
        if isinstance(item, Mapping)
    ]
    mcp_results = [
        _mapping(item)
        for item in state.get("mcp_results", [])
        if isinstance(item, Mapping)
    ]
    workflow_results = [
        _mapping(item)
        for item in state.get("mcp_workflow_results", [])
        if isinstance(item, Mapping)
    ]
    request_results = (
        [item for item in workflow_results if not item.get("workflow_is_aggregate")]
        or mcp_results
    )
    return {
        "questionLength": len(str(state.get("message") or "")),
        "historyCount": len(state.get("history") or []),
        "refinedQuestionLength": len(
            str(classification.get("refined_query") or "")
        ),
        "classification": {
            "classificationType": classification.get("classification_type"),
            "agentCode": classification.get("agent_code"),
        }
        if classification
        else None,
        "scenarioMatches": [
            {
                "scenarioCode": item.get("scenario_code"),
                "detailScenarioCode": item.get("detail_scenario_code"),
                "parameterKeys": sorted(_mapping(item.get("parameters"))),
            }
            for item in matches
        ],
        "mcpRequests": [
            {
                "toolName": item.get("tool_name"),
                "requestId": item.get("request_id"),
                "argumentKeys": sorted(_mapping(item.get("arguments"))),
            }
            for item in request_results
        ],
        "mcpWorkflowResults": [
            {
                "stepCode": item.get("workflow_step_code"),
                "stepIndex": item.get("workflow_step_index"),
                "stepCount": item.get("workflow_step_count"),
                "isFinal": item.get("workflow_is_final"),
                "executionMode": item.get("workflow_execution_mode"),
                "itemIndex": item.get("workflow_item_index"),
                "itemCount": item.get("workflow_item_count"),
                "isAggregate": item.get("workflow_is_aggregate"),
                "handlerCode": item.get("workflow_handler_code"),
                "toolName": item.get("tool_name"),
                "requestId": item.get("request_id"),
                "succeeded": item.get("succeeded"),
                "outcome": item.get("outcome"),
                "dataCount": _mcp_data_count(item),
            }
            for item in workflow_results
        ],
        "mcpResults": [
            {
                "toolName": item.get("tool_name"),
                "succeeded": item.get("succeeded"),
                "outcome": item.get("outcome"),
                "resultFormat": item.get("result_format"),
                "dataCount": _mcp_data_count(item),
            }
            for item in mcp_results
        ],
        "status": state.get("status"),
        "hitlType": _mapping(state.get("interrupt")).get("type"),
        "humanInputCount": len(state.get("human_input") or []),
    }


def _sanitize_developer_value(value: Any, *, key: str | None = None) -> Any:
    """state 구조는 유지하면서 인증·비밀 키 값만 강제로 마스킹한다."""

    normalized_key = (key or "").casefold().replace("-", "_")
    if any(
        marker in normalized_key
        for marker in ("access_token", "authorization", "api_key", "password", "secret")
    ):
        return "***REDACTED***"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_developer_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_developer_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _sanitize_developer_value(model_dump(mode="json"))
    return str(value)


def create_trace_recorder(
    *,
    enabled: bool,
    directory: str,
    project_code: str,
) -> TraceRecorder:
    """환경설정에 따라 실제 CSV 기록기 또는 무동작 기록기를 만든다."""

    if not enabled:
        logger.info("======== 로컬 CSV 추적 비활성화")
        return EmptyTraceRecorder()

    path = Path(directory)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    logger.info("======== 로컬 CSV 추적 활성화 | 저장경로=%s", path)
    return LocalCsvTraceRecorder(path, project_code)


def _build_row(
    *,
    stage: str,
    state: Mapping[str, Any],
    project_code: str,
    elapsed_seconds: float | None,
    error: Exception | None,
) -> dict[str, str]:
    """중첩된 그래프 상태를 분석하기 쉬운 고정 CSV 컬럼으로 펼친다."""

    classification = _mapping(state.get("classification"))
    subagent = _mapping(state.get("subagent"))
    mcp = _mapping(state.get("mcp"))
    scenario_matches = [
        _mapping(item)
        for item in subagent.get("matches", [])
        if isinstance(item, Mapping)
    ]
    if not scenario_matches and subagent:
        scenario_matches = [subagent]
    mcp_results = [
        _mapping(item)
        for item in state.get("mcp_results", [])
        if isinstance(item, Mapping)
    ]
    if not mcp_results and mcp:
        mcp_results = [mcp]
    interrupt = _mapping(state.get("interrupt"))
    values: dict[str, Any] = {
        "최초요청일시": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "최종갱신일시": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "현재단계": stage,
        "처리단계이력_JSON": _json_text([stage]),
        "프로젝트코드": project_code,
        "thread_id": state.get("thread_id"),
        "session_id": state.get("session_id"),
        "사원번호": _fingerprint(state.get("employee_id")),
        "message_id": state.get("message_id"),
        # API 입력 가드레일을 통과한 질문만 graph state에 들어오며, CSV 출력
        # 직전에도 대표 비밀·개인정보 패턴을 한 번 더 제거한다.
        "원본질문": sanitize_log_text(str(state.get("message") or "")),
        "프론트선택에이전트": state.get("frontend_agent_code"),
        "대화이력개수": len(state.get("history") or []),
        "보정질문": sanitize_log_text(
            str(classification.get("refined_query") or "")
        ),
        "마스터분류유형": classification.get("classification_type"),
        "마스터에이전트코드": classification.get("agent_code"),
        "서브에이전트코드": subagent.get("agent_code"),
        "시나리오코드": " | ".join(
            str(item.get("scenario_code", "")) for item in scenario_matches
        ),
        "시나리오명": " | ".join(
            str(item.get("scenario_name", "")) for item in scenario_matches
        ),
        "세부시나리오코드": " | ".join(
            str(item.get("detail_scenario_code", "")) for item in scenario_matches
        ),
        "세부시나리오명": " | ".join(
            str(item.get("detail_scenario_name", "")) for item in scenario_matches
        ),
        "추출파라미터_JSON": _json_text(
            [
                sorted(
                    _redact_match_parameters(
                        item,
                        str(subagent.get("agent_code", "")),
                    )
                )
                for item in scenario_matches
            ]
        ),
        "MCP도구명": " | ".join(str(item.get("tool_name", "")) for item in mcp_results),
        "MCP추적ID": " | ".join(
            str(item.get("request_id", "")) for item in mcp_results
        ),
        "MCP요청파라미터_JSON": _json_text(
            [sorted(_mapping(item.get("arguments"))) for item in mcp_results]
        ),
        "MCP성공여부": " | ".join(
            str(item.get("succeeded", "")) for item in mcp_results
        ),
        "MCP결과_JSON": _json_text(
            [{"data_count": _mcp_data_count(item)} for item in mcp_results]
        ),
        "MCP결과형식": " | ".join(
            str(item.get("result_format", "")) for item in mcp_results
        ),
        "MCP정제결과_JSON": _json_text(
            [
                _formatted_result_summary(item.get("formatted_result"))
                for item in mcp_results
            ]
        ),
        "처리상태": state.get("status"),
        "HITL유형": state.get("hitl_type") or interrupt.get("type"),
        "마스터분류소요시간_초": (
            f"{elapsed_seconds:.3f}"
            if elapsed_seconds is not None and stage.startswith("마스터")
            else ""
        ),
        "서브분류소요시간_초": (
            f"{elapsed_seconds:.3f}"
            if elapsed_seconds is not None and stage.startswith("서브")
            else ""
        ),
        "MCP호출소요시간_초": (
            f"{elapsed_seconds:.3f}"
            if elapsed_seconds is not None and stage.startswith("MCP")
            else ""
        ),
        "오류유형": type(error).__name__ if error is not None else "",
        "오류내용": (
            sanitize_log_text(str(error)) if error is not None else ""
        ),
    }
    return {column: _safe_cell(values.get(column)) for column in CSV_COLUMNS}


def _read_rows(file_path: Path) -> list[dict[str, str]]:
    """기존 단일 CSV 파일을 읽는다. 파일이 없으면 빈 목록을 반환한다."""

    if not file_path.exists() or file_path.stat().st_size == 0:
        return []
    with file_path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def _merge_row(
    existing: Mapping[str, str],
    incoming: Mapping[str, str],
) -> dict[str, str]:
    """기존 thread 행에 새 단계에서 확정된 값만 누적한다."""

    merged = dict(existing)
    for column in CSV_COLUMNS:
        value = incoming.get(column, "")
        if value != "":
            merged[column] = value

    # 최초 요청 시각은 최초 행의 값을 계속 유지한다.
    merged["최초요청일시"] = existing.get(
        "최초요청일시", incoming.get("최초요청일시", "")
    )

    try:
        stages = json.loads(existing.get("처리단계이력_JSON", "[]"))
        if not isinstance(stages, list):
            stages = []
    except (TypeError, json.JSONDecodeError):
        stages = []
    current_stage = incoming.get("현재단계", "")
    if current_stage:
        stages.append(current_stage)
    merged["처리단계이력_JSON"] = _safe_cell(_json_text(stages))
    return {column: merged.get(column, "") for column in CSV_COLUMNS}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _fingerprint(value: Any) -> str:
    """CSV에서도 사번 원문 대신 같은 대상을 연결할 단방향 지문을 쓴다."""

    normalized = str(value or "").strip()
    if not normalized:
        return ""
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def _mcp_data_count(execution: Mapping[str, Any]) -> int:
    """원문을 기록하지 않고 대표 MCP data 배열의 건수만 센다."""

    result = execution.get("result")
    if not isinstance(result, Mapping):
        return 0
    candidates: list[Any] = [result.get("data")]
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        candidates.append(structured.get("data"))
        nested_result = structured.get("result")
        if isinstance(nested_result, Mapping):
            candidates.append(nested_result.get("data"))
    wrapper = result.get("result")
    if isinstance(wrapper, Mapping):
        candidates.append(wrapper.get("data"))
        structured = wrapper.get("structuredContent")
        if isinstance(structured, Mapping):
            candidates.append(structured.get("data"))
    return max(
        (len(candidate) for candidate in candidates if isinstance(candidate, list)),
        default=0,
    )


def _formatted_result_summary(value: Any) -> dict[str, Any]:
    """정제 답변·테이블 본문 대신 구조와 건수만 CSV에 남긴다."""

    formatted = _mapping(value)
    data = formatted.get("data")
    renderables = formatted.get("renderables")
    answer = formatted.get("answer")
    return {
        "data_count": len(data) if isinstance(data, list) else 0,
        "renderable_count": len(renderables) if isinstance(renderables, list) else 0,
        "answer_length": len(answer) if isinstance(answer, str) else 0,
    }


def _redact_match_parameters(
    match: Mapping[str, Any],
    agent_code: str = "",
) -> dict[str, Any]:
    """Python 시나리오 action의 민감 입력을 CSV에서 마스킹한다."""

    from app.scenario_actions import redact_scenario_action_parameters

    parameters = dict(_mapping(match.get("parameters")))
    return redact_scenario_action_parameters(
        agent_code,
        str(match.get("detail_scenario_code", "")),
        parameters,
    )


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _safe_cell(value: Any) -> str:
    """Excel에서 사용자 문자열이 수식으로 실행되는 CSV Injection을 막는다."""

    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text
