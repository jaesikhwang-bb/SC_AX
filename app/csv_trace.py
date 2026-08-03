"""한 번의 질의 처리 과정을 로컬 CSV 파일에 단계별로 기록하는 모듈."""

import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from app.observability import logger


CSV_COLUMNS = (
    "최초요청일시",
    "최종갱신일시",
    "현재단계",
    "처리단계이력_JSON",
    "프로젝트코드",
    "thread_id",
    "conversation_id",
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
                with temporary_path.open(
                    "w", encoding="utf-8-sig", newline=""
                ) as file:
                    writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
                    writer.writeheader()
                    writer.writerows(rows)
                temporary_path.replace(file_path)
            logger.info(
                "======== 로컬 CSV 행 갱신 완료 | 단계=%s | thread_id=%s | 파일=%s",
                stage,
                thread_id,
                file_path,
            )
        except Exception as exc:
            logger.info(
                "======== 로컬 CSV 기록 생략 | 단계=%s | 오류유형=%s | 오류=%s",
                stage,
                type(exc).__name__,
                exc,
            )


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
    interrupt = _mapping(state.get("interrupt"))
    values: dict[str, Any] = {
        "최초요청일시": datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        ),
        "최종갱신일시": datetime.now().astimezone().isoformat(
            timespec="milliseconds"
        ),
        "현재단계": stage,
        "처리단계이력_JSON": _json_text([stage]),
        "프로젝트코드": project_code,
        "thread_id": state.get("thread_id"),
        "conversation_id": state.get("conversation_id"),
        "사원번호": state.get("employee_id"),
        "message_id": state.get("message_id"),
        "원본질문": state.get("message"),
        "프론트선택에이전트": state.get("frontend_agent_code"),
        "대화이력개수": len(state.get("history") or []),
        "보정질문": classification.get("refined_query"),
        "마스터분류유형": classification.get("classification_type"),
        "마스터에이전트코드": classification.get("agent_code"),
        "서브에이전트코드": subagent.get("agent_code"),
        "시나리오코드": subagent.get("scenario_code"),
        "시나리오명": subagent.get("scenario_name"),
        "세부시나리오코드": subagent.get("detail_scenario_code"),
        "세부시나리오명": subagent.get("detail_scenario_name"),
        "추출파라미터_JSON": _json_text(subagent.get("parameters")),
        "MCP도구명": mcp.get("tool_name"),
        "MCP추적ID": mcp.get("request_id"),
        "MCP요청파라미터_JSON": _json_text(mcp.get("arguments")),
        "MCP성공여부": mcp.get("succeeded"),
        "MCP결과_JSON": _json_text(mcp.get("result")),
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
        "오류내용": str(error) if error is not None else "",
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
