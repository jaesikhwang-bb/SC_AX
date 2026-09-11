"""마스터·서브에이전트·MCP 전체 흐름을 CSV로 평가하는 모듈."""

import asyncio
import csv
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from types import TracebackType
from typing import Protocol, TextIO
from uuid import uuid4

from app.evaluation_metrics import (
    compute_classification_metrics,
    compute_latency_breakdown,
    compute_latency_metrics,
)
from app.evaluation_logging import evaluation_logger as logger
from app.graph import MasterResult


REQUIRED_COLUMNS = (
    "message",
    "expected_classification_type",
    "expected_agent_code",
)
OPTIONAL_EXPECTED_COLUMNS = (
    "expected_status",
    "expected_scenario_code",
    "expected_detail_scenario_code",
    "expected_mcp_tool_name",
    "expected_mcp_succeeded",
)
RESULT_COLUMNS = (
    "actual_status",
    "actual_classification_type",
    "actual_agent_code",
    "actual_refined_query",
    "actual_subagent_agent_code",
    "actual_scenario_code",
    "actual_detail_scenario_code",
    "actual_parameter_names",
    "actual_mcp_backend",
    "actual_mcp_tool_name",
    "actual_mcp_succeeded",
    "actual_mcp_result_present",
    "latency_seconds",
    "test_result",
    "failure_reason",
    "error_message",
    "tested_at",
)
EXCLUDED_OUTPUT_COLUMNS = {"difficulty"}
KOREAN_OUTPUT_COLUMN_NAMES = {
    "test_id": "테스트_ID",
    "message": "질문",
    "expected_classification_type": "기대_분류유형",
    "expected_agent_code": "기대_에이전트코드",
    "category": "문의유형",
    "expected_status": "기대_처리상태",
    "expected_scenario_code": "기대_시나리오코드",
    "expected_detail_scenario_code": "기대_세부시나리오코드",
    "expected_mcp_tool_name": "기대_MCP도구명",
    "expected_mcp_succeeded": "기대_MCP성공여부",
    "actual_status": "실제_처리상태",
    "actual_classification_type": "실제_분류유형",
    "actual_agent_code": "실제_에이전트코드",
    "actual_refined_query": "실제_정제질문",
    "actual_subagent_agent_code": "실제_서브에이전트코드",
    "actual_scenario_code": "실제_시나리오코드",
    "actual_detail_scenario_code": "실제_세부시나리오코드",
    "actual_parameter_names": "실제_파라미터명",
    "actual_mcp_backend": "실제_MCP백엔드",
    "actual_mcp_tool_name": "실제_MCP도구명",
    "actual_mcp_succeeded": "실제_MCP성공여부",
    "actual_mcp_result_present": "실제_MCP결과존재여부",
    "latency_seconds": "지연시간_초",
    "test_result": "테스트결과",
    "failure_reason": "실패사유",
    "error_message": "오류메시지",
    "tested_at": "테스트일시",
}
CLASSIFICATION_TYPES = {"AGENT", "EMPTY_QUERY", "OUT_OF_SCOPE"}
STATUSES = {"PASS", "INPUT_REQUIRED", "EXCEPTION"}
# 평가 단계. 실행하지 않은 단계의 기대값은 판정에서 제외해야 하므로 단계마다
# 비교할 기대 항목을 함께 정의한다.
EVALUATION_STAGES = ("master", "subagent", "full")
_MASTER_FIELDS = frozenset({"status", "classification_type", "agent_code"})
_SUBAGENT_FIELDS = _MASTER_FIELDS | {"scenario_code", "detail_scenario_code"}
STAGE_COMPARED_FIELDS = {
    "master": _MASTER_FIELDS,
    "subagent": _SUBAGENT_FIELDS,
    "full": _SUBAGENT_FIELDS | {"mcp_tool_name", "mcp_succeeded"},
}
# 기대값 자체가 잘못된 행은 어떤 의도 라벨에도 속하지 않으므로 응답 속도
# 분석에서만 별도 그룹으로 모은다.
INPUT_ERROR_LABEL = "INPUT_ERROR"
# 멀티턴 범위를 가리키는 인자 이름은 그래프 버전에 따라 다르다. 두 이름은
# 같은 값을 뜻하므로(app/models.py 참고) 실제 선언된 이름으로 전달한다.
CONVERSATION_PARAMETER_ALIASES = ("conversation_id", "session_id")


class EvaluationInputError(ValueError):
    """평가 CSV의 헤더나 행 값이 계약에 맞지 않을 때 발생한다."""


class PipelineRunner(Protocol):
    """평가기가 사용하는 전체 그래프의 최소 실행 인터페이스.

    ``conversation_id``를 받지 않는 그래프 버전도 있다. 평가기는 실행 전에
    시그니처를 확인해 지원할 때만 전달하므로 두 버전 모두에서 동작한다.
    """

    async def start(
        self,
        *,
        thread_id: str,
        employee_id: str,
        conversation_id: str,
        message: str,
        frontend_agent_code: str | None,
    ) -> MasterResult: ...


class Clock(Protocol):
    """테스트에서 평가 시각을 고정하기 위한 최소 시계 인터페이스."""

    def __call__(self) -> datetime: ...


@dataclass(frozen=True)
class EvaluationSummary:
    """CSV 평가 완료 후 CLI가 출력할 집계 결과."""

    output_path: Path
    stage: str
    total: int
    passed: int
    failed: int
    errors: int
    classification_metrics: dict
    timing_metrics: dict[str, float]
    latency_by_label: dict[str, dict[str, float]]
    latency_by_result: dict[str, dict[str, float]]


@dataclass(frozen=True)
class _StartCallPlan:
    """그래프 버전에 맞춘 start() 호출 인자 구성."""

    # 실제로 선언된 멀티턴 식별자 인자 이름. 받지 않는 버전이면 None이다.
    conversation_parameter: str | None
    send_frontend_agent_code: bool

    def argument_names(self) -> tuple[str, ...]:
        names = ["thread_id", "employee_id", "message"]
        if self.conversation_parameter is not None:
            names.append(self.conversation_parameter)
        if self.send_frontend_agent_code:
            names.append("frontend_agent_code")
        return tuple(names)

    def build(
        self,
        *,
        execution_id: str,
        employee_id: str,
        message: str,
    ) -> dict[str, str | None]:
        arguments: dict[str, str | None] = {
            "thread_id": execution_id,
            "employee_id": employee_id,
            "message": message,
        }
        if self.conversation_parameter is not None:
            arguments[self.conversation_parameter] = f"csv-evaluation-{execution_id}"
        if self.send_frontend_agent_code:
            # 프론트 선택 코드를 전달하지 않아 HITL 없이 마스터 분류 결과로
            # 서브에이전트와 MCP까지 자동 진행한다.
            arguments["frontend_agent_code"] = None
        return arguments

    def describe(self) -> str:
        return self.conversation_parameter or "전달안함(그래프 미지원)"


@dataclass(frozen=True)
class _RowEvaluation:
    """CSV 출력 결과와 마스터 분류 지표용 라벨 쌍."""

    result: dict[str, str]
    classification_pair: tuple[str, str] | None
    expected_label: str


async def evaluate_pipeline_csv(
    *,
    input_path: Path,
    output_path: Path,
    runner: PipelineRunner,
    employee_id: str,
    allowed_agent_codes: tuple[str, ...],
    stage: str = "full",
    overwrite: bool = False,
    clock: Clock | None = None,
    row_timeout_seconds: float = 180.0,
) -> EvaluationSummary:
    """CSV 각 행을 독립된 그래프 요청으로 실행하고 결과를 기록한다.

    ``stage``는 어디까지 실행한 평가인지를 뜻한다. 실행하지 않은 단계의
    기대값은 판정에서 제외하므로, 서브에이전트나 MCP를 끄고 돌려도 해당
    기대 컬럼 때문에 FAIL이 나지 않는다. 실제 실행 범위는 호출자가 주입한
    ``runner``의 구성으로 결정되며 이 값은 판정 기준과 기록용이다.
    """

    if stage not in EVALUATION_STAGES:
        raise EvaluationInputError(
            f"평가 단계가 유효하지 않습니다(허용값: {', '.join(EVALUATION_STAGES)})."
        )
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if input_path == output_path:
        raise EvaluationInputError("입력 CSV와 출력 CSV는 달라야 합니다.")
    if not input_path.is_file():
        raise EvaluationInputError(f"입력 CSV가 없습니다: {input_path}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"출력 CSV가 이미 있습니다: {output_path} "
            "(덮어쓰려면 --overwrite를 사용하세요.)"
        )
    if not output_path.parent.is_dir():
        raise EvaluationInputError(f"출력 디렉터리가 없습니다: {output_path.parent}")
    if row_timeout_seconds <= 0:
        raise EvaluationInputError("행 제한시간은 0보다 커야 합니다.")
    # 그래프 버전마다 start() 인자 구성이 달라 실행 전에 한 번만 확인한다.
    start_plan = _plan_start_call(runner)

    rows, fieldnames = _read_rows(input_path)
    output_fieldnames = [
        name
        for name in [*fieldnames, *RESULT_COLUMNS]
        if name not in EXCLUDED_OUTPUT_COLUMNS
    ]
    localized_output_fieldnames = [
        KOREAN_OUTPUT_COLUMN_NAMES.get(name, name) for name in output_fieldnames
    ]
    now = clock or (lambda: datetime.now(timezone.utc))
    classification_pairs: list[tuple[str, str]] = []
    latencies_seconds: list[float] = []
    label_latency_samples: list[tuple[str, float]] = []
    result_latency_samples: list[tuple[str, float]] = []
    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    total = len(rows)
    # wall 시간은 CSV 전체 평가의 실제 경과시간이다. perf_counter는 시스템
    # 시각 변경의 영향을 받지 않아 실행시간 측정에 적합하다.
    wall_started_at = perf_counter()

    logger.info(
        "평가 시작 | 데이터셋=%s | 단계=%s | 행수=%d | 대화식별자인자=%s | 결과파일=%s",
        input_path.name,
        stage,
        total,
        start_plan.describe(),
        output_path.name,
    )
    # 완료된 행을 즉시 이어 쓰므로 중단되더라도 결과가 남고, 매 행마다 전체
    # 파일을 다시 쓰던 O(n²) 재작성도 사라진다.
    with _ResultCsvWriter(
        output_path=output_path,
        fieldnames=localized_output_fieldnames,
    ) as result_writer:
        for index, row in enumerate(rows, start=1):
            row_number = index + 1
            test_id = _safe_test_id(row.get("test_id"), row_number)
            logger.debug(
                "행 실행 시작 | %d/%d | test_id=%s",
                index,
                total,
                test_id,
            )
            # 입력 검증부터 그래프 응답 비교까지 한 행의 전체 처리시간을
            # 측정한다. 따라서 제한시간 초과나 예외 행도 지연시간 통계에
            # 포함된다.
            row_started_at = perf_counter()
            evaluation = await _evaluate_row(
                row=row,
                row_number=row_number,
                runner=runner,
                employee_id=employee_id,
                allowed_agent_codes=allowed_agent_codes,
                stage=stage,
                tested_at=now().isoformat(),
                row_timeout_seconds=row_timeout_seconds,
                start_plan=start_plan,
            )
            result = evaluation.result
            latency_seconds = perf_counter() - row_started_at
            result["latency_seconds"] = f"{latency_seconds:.4f}"
            latencies_seconds.append(latency_seconds)
            label_latency_samples.append((evaluation.expected_label, latency_seconds))
            result_latency_samples.append((result["test_result"], latency_seconds))
            if evaluation.classification_pair is not None:
                classification_pairs.append(evaluation.classification_pair)
            counts[result["test_result"]] += 1
            merged_row = {**row, **result}
            result_writer.write(
                {
                    KOREAN_OUTPUT_COLUMN_NAMES.get(name, name): (merged_row[name])
                    for name in output_fieldnames
                }
            )
            logger.info(
                "[%*d/%d %5.1f%%] %-5s %-8s %-20s %s | 누적 P%d/F%d/E%d | 잔여 ~%s%s",
                len(str(total)),
                index,
                total,
                _progress_percent(index, total),
                result["test_result"],
                f"{latency_seconds:.2f}s",
                (result["actual_agent_code"] or "-")[:20],
                test_id,
                counts["PASS"],
                counts["FAIL"],
                counts["ERROR"],
                _remaining_text(
                    elapsed_seconds=perf_counter() - wall_started_at,
                    completed=index,
                    total=total,
                ),
                _diagnosis_text(result),
            )

    classification_metrics = compute_classification_metrics(
        classification_pairs,
        labels=[
            *allowed_agent_codes,
            "EMPTY_QUERY",
            "OUT_OF_SCOPE",
        ],
    )
    # 행별 분포(p50/p95 등)와 실제 전체 처리량을 한 번에 계산한다.
    timing_metrics = compute_latency_metrics(
        latencies_seconds,
        wall_seconds=perf_counter() - wall_started_at,
    )
    # 어떤 의도와 어떤 판정에서 응답이 느린지 따로 확인할 수 있게 한다.
    latency_by_label = compute_latency_breakdown(label_latency_samples)
    latency_by_result = compute_latency_breakdown(result_latency_samples)
    logger.info(
        "평가 완료 | 단계=%s | 전체=%d | PASS=%d | FAIL=%d | ERROR=%d | "
        "accuracy=%.4f | macro_f1=%.4f | weighted_f1=%.4f | "
        "평균응답=%.2f초 | p95=%.2f초 | 총소요=%.1f초 | 결과파일=%s",
        stage,
        total,
        counts["PASS"],
        counts["FAIL"],
        counts["ERROR"],
        classification_metrics["accuracy"],
        classification_metrics["macro_avg"]["f1"],
        classification_metrics["weighted_avg"]["f1"],
        timing_metrics["latency_mean_s"],
        timing_metrics["latency_p95_s"],
        timing_metrics["wall_s"],
        output_path.name,
    )
    return EvaluationSummary(
        output_path=output_path,
        stage=stage,
        total=total,
        passed=counts["PASS"],
        failed=counts["FAIL"],
        errors=counts["ERROR"],
        classification_metrics=classification_metrics,
        timing_metrics=timing_metrics,
        latency_by_label=latency_by_label,
        latency_by_result=latency_by_result,
    )


def _plan_start_call(runner: PipelineRunner) -> _StartCallPlan:
    """그래프 버전마다 다른 start() 시그니처에 맞춰 호출 계획을 세운다.

    멀티턴 범위를 가리키는 인자는 버전에 따라 ``conversation_id`` 또는
    ``session_id``로 불린다. 두 이름은 같은 값을 뜻하므로 실제로 선언된
    이름을 찾아 그 이름으로 전달한다. 아예 없는 버전이면 전달하지 않는다.
    평가는 빈 대화이력 저장소를 쓰기 때문에 이 값이 이력 격리에 관여하지
    않고 MCP 추적 ID와 로그 문자열에만 쓰인다.
    """

    try:
        parameters = inspect.signature(runner.start).parameters
    except (TypeError, ValueError):
        # 시그니처를 읽을 수 없는 구현은 최신 계약을 따른다고 본다.
        return _StartCallPlan(
            conversation_parameter=CONVERSATION_PARAMETER_ALIASES[0],
            send_frontend_agent_code=True,
        )

    accepts_any_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    conversation_parameter = next(
        (name for name in CONVERSATION_PARAMETER_ALIASES if name in parameters),
        CONVERSATION_PARAMETER_ALIASES[0] if accepts_any_keyword else None,
    )
    plan = _StartCallPlan(
        conversation_parameter=conversation_parameter,
        send_frontend_agent_code=(
            accepts_any_keyword or "frontend_agent_code" in parameters
        ),
    )
    if accepts_any_keyword:
        return plan

    # 평가기가 채우지 못하는 필수 인자가 남아 있으면 첫 행에서 알기 어려운
    # TypeError로 죽는 대신, 어떤 인자가 문제인지 먼저 알려 준다.
    provided = {"self", *plan.argument_names()}
    missing = [
        name
        for name, parameter in parameters.items()
        if name not in provided
        and parameter.default is inspect.Parameter.empty
        and parameter.kind
        in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    if missing:
        raise EvaluationInputError(
            "그래프 start()가 요구하는 인자를 평가기가 채우지 못했습니다: "
            + ", ".join(missing)
            + ". app/pipeline_evaluator.py의 _StartCallPlan을 확인하세요."
        )
    return plan


def _read_rows(input_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """UTF-8과 Excel용 UTF-8 BOM CSV를 읽고 헤더를 검증한다."""

    try:
        with input_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as csv_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None:
                raise EvaluationInputError("CSV 헤더가 없습니다.")

            original_fieldnames = list(reader.fieldnames)
            fieldnames = [name.strip() for name in original_fieldnames]
            if any(not name for name in fieldnames):
                raise EvaluationInputError("CSV 헤더에 빈 컬럼명이 있습니다.")
            if len(set(fieldnames)) != len(fieldnames):
                raise EvaluationInputError("CSV 헤더에 중복 컬럼명이 있습니다.")

            missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
            if missing:
                raise EvaluationInputError(
                    "필수 CSV 컬럼이 없습니다: " + ", ".join(missing)
                )
            collisions = [name for name in RESULT_COLUMNS if name in fieldnames]
            if collisions:
                raise EvaluationInputError(
                    "입력 CSV에 결과용 컬럼이 이미 있습니다: " + ", ".join(collisions)
                )

            rows: list[dict[str, str]] = []
            for row_number, raw_row in enumerate(reader, start=2):
                if None in raw_row:
                    raise EvaluationInputError(
                        f"{row_number}행에 헤더보다 많은 값이 있습니다."
                    )
                rows.append(
                    {
                        fieldnames[index]: (raw_row[name] or "")
                        for index, name in enumerate(original_fieldnames)
                    }
                )
            return rows, fieldnames
    except UnicodeDecodeError as exc:
        raise EvaluationInputError(
            "CSV는 UTF-8 또는 UTF-8 BOM 인코딩이어야 합니다."
        ) from exc


async def _evaluate_row(
    *,
    row: dict[str, str],
    row_number: int,
    runner: PipelineRunner,
    employee_id: str,
    allowed_agent_codes: tuple[str, ...],
    stage: str,
    tested_at: str,
    row_timeout_seconds: float,
    start_plan: _StartCallPlan,
) -> _RowEvaluation:
    """한 행의 기대값을 검증하고 그래프 결과와 비교한다."""

    try:
        expected = _parse_expected_values(
            row=row,
            row_number=row_number,
            allowed_agent_codes=allowed_agent_codes,
        )
    except EvaluationInputError as exc:
        return _RowEvaluation(
            result=_error_result(str(exc), tested_at),
            classification_pair=None,
            expected_label=INPUT_ERROR_LABEL,
        )

    expected_label = _classification_label(
        str(expected["classification_type"]),
        str(expected["agent_code"]),
    )

    try:
        execution_id = str(uuid4())
        actual = await asyncio.wait_for(
            runner.start(
                **start_plan.build(
                    execution_id=execution_id,
                    employee_id=employee_id,
                    message=str(expected["message"]),
                )
            ),
            timeout=row_timeout_seconds,
        )
    except TimeoutError:
        return _RowEvaluation(
            result=_error_result(
                f"전체 흐름 행 제한시간 초과({row_timeout_seconds:g}초)",
                tested_at,
            ),
            classification_pair=(expected_label, "ERROR"),
            expected_label=expected_label,
        )
    except Exception as exc:
        return _RowEvaluation(
            result=_error_result(
                f"전체 흐름 실행 오류({type(exc).__name__})",
                tested_at,
            ),
            classification_pair=(expected_label, "ERROR"),
            expected_label=expected_label,
        )

    actual_values = _actual_values(actual)
    failure_reason = _failure_reason(expected, actual_values, stage=stage)
    actual_label = _classification_label(
        actual_values["actual_classification_type"],
        actual_values["actual_agent_code"],
    )
    return _RowEvaluation(
        result={
            **actual_values,
            "test_result": "FAIL" if failure_reason else "PASS",
            "failure_reason": failure_reason,
            "error_message": "",
            "tested_at": tested_at,
        },
        classification_pair=(expected_label, actual_label),
        expected_label=expected_label,
    )


def _parse_expected_values(
    *,
    row: dict[str, str],
    row_number: int,
    allowed_agent_codes: tuple[str, ...],
) -> dict[str, str | bool | None]:
    message = row["message"].strip()
    classification_type = row["expected_classification_type"].strip().upper()
    agent_code = row["expected_agent_code"].strip().upper()
    if classification_type not in CLASSIFICATION_TYPES:
        raise EvaluationInputError(
            f"{row_number}행 expected_classification_type이 유효하지 "
            f"않습니다(허용값: {', '.join(sorted(CLASSIFICATION_TYPES))})."
        )
    if classification_type == "EMPTY_QUERY":
        if message:
            raise EvaluationInputError(
                f"{row_number}행 EMPTY_QUERY의 message는 비어 있어야 합니다."
            )
    elif not message:
        raise EvaluationInputError(f"{row_number}행 message가 비어 있습니다.")

    normalized_codes = {code.upper() for code in allowed_agent_codes}
    if classification_type == "AGENT":
        if not agent_code:
            raise EvaluationInputError(
                f"{row_number}행 AGENT에는 expected_agent_code가 필요합니다."
            )
        if agent_code not in normalized_codes:
            raise EvaluationInputError(
                f"{row_number}행 expected_agent_code가 활성 마스터 프롬프트에 "
                f"등록되지 않았습니다(허용값: "
                f"{', '.join(sorted(normalized_codes))})."
            )
    elif agent_code:
        raise EvaluationInputError(
            f"{row_number}행 예외 분류의 expected_agent_code는 비어 있어야 합니다."
        )

    default_status = "PASS" if classification_type == "AGENT" else "EXCEPTION"
    expected_status = row.get("expected_status", "").strip().upper()
    expected_status = expected_status or default_status
    if expected_status not in STATUSES:
        raise EvaluationInputError(
            f"{row_number}행 expected_status가 유효하지 않습니다"
            f"(허용값: {', '.join(sorted(STATUSES))})."
        )

    expected_mcp_succeeded = _parse_optional_bool(
        row.get("expected_mcp_succeeded", ""),
        row_number=row_number,
        column="expected_mcp_succeeded",
    )
    return {
        "message": message,
        "classification_type": classification_type,
        "agent_code": agent_code,
        "status": expected_status,
        "scenario_code": row.get("expected_scenario_code", "").strip(),
        "detail_scenario_code": row.get("expected_detail_scenario_code", "").strip(),
        "mcp_tool_name": row.get("expected_mcp_tool_name", "").strip(),
        "mcp_succeeded": expected_mcp_succeeded,
    }


def _parse_optional_bool(
    value: str,
    *,
    row_number: int,
    column: str,
) -> bool | None:
    normalized = value.strip().casefold()
    if not normalized:
        return None
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise EvaluationInputError(f"{row_number}행 {column}은 true 또는 false여야 합니다.")


def _actual_values(actual: MasterResult) -> dict[str, str]:
    subagent = actual.subagent
    mcp = actual.mcp
    return {
        "actual_status": actual.status,
        "actual_classification_type": (actual.classification.classification_type.value),
        "actual_agent_code": actual.classification.agent_code or "",
        "actual_refined_query": actual.classification.refined_query,
        "actual_subagent_agent_code": subagent.agent_code if subagent else "",
        "actual_scenario_code": subagent.scenario_code if subagent else "",
        "actual_detail_scenario_code": (
            subagent.detail_scenario_code if subagent else ""
        ),
        "actual_parameter_names": (
            ",".join(sorted(subagent.parameters)) if subagent else ""
        ),
        "actual_mcp_backend": mcp.backend if mcp else "",
        "actual_mcp_tool_name": mcp.tool_name if mcp else "",
        "actual_mcp_succeeded": (_bool_text(mcp.succeeded) if mcp else ""),
        # 개인정보가 포함될 수 있는 MCP 결과 본문은 저장하지 않는다.
        "actual_mcp_result_present": (
            _bool_text(mcp.result is not None) if mcp else ""
        ),
    }


def _failure_reason(
    expected: dict[str, str | bool | None],
    actual: dict[str, str],
    *,
    stage: str = "full",
) -> str:
    comparisons = (
        ("status", "actual_status"),
        ("classification_type", "actual_classification_type"),
        ("agent_code", "actual_agent_code"),
        ("scenario_code", "actual_scenario_code"),
        ("detail_scenario_code", "actual_detail_scenario_code"),
        ("mcp_tool_name", "actual_mcp_tool_name"),
    )
    # 이번 실행에서 아예 돌리지 않은 단계의 기대값은 판정 대상이 아니다.
    compared_fields = STAGE_COMPARED_FIELDS[stage]
    reasons: list[str] = []
    for expected_name, actual_name in comparisons:
        if expected_name not in compared_fields:
            continue
        expected_value = expected[expected_name]
        # 선택 기대값은 입력했을 때만 판정한다.
        if (
            expected_name
            in {
                "scenario_code",
                "detail_scenario_code",
                "mcp_tool_name",
            }
            and not expected_value
        ):
            continue
        actual_value = actual[actual_name]
        if expected_value != actual_value:
            reasons.append(
                f"{expected_name} 불일치"
                f"(expected={expected_value}, actual={actual_value})"
            )

    expected_mcp = expected["mcp_succeeded"]
    if expected_mcp is not None and "mcp_succeeded" in compared_fields:
        actual_mcp = actual["actual_mcp_succeeded"]
        expected_text = _bool_text(expected_mcp)
        if expected_text != actual_mcp:
            reasons.append(
                f"mcp_succeeded 불일치(expected={expected_text}, actual={actual_mcp})"
            )
    return "; ".join(reasons)


def _error_result(message: str, tested_at: str) -> dict[str, str]:
    return {
        **{column: "" for column in RESULT_COLUMNS},
        "test_result": "ERROR",
        "error_message": message,
        "tested_at": tested_at,
    }


class _ResultCsvWriter:
    """완료된 행만 즉시 이어 쓰는 결과 CSV 기록기.

    한글이 포함된 결과를 Excel에서 바로 열 수 있도록 UTF-8 BOM으로 저장한다.
    행마다 flush하므로 평가가 중단돼도 그때까지 완료된 행은 그대로 남는다.
    """

    def __init__(self, *, output_path: Path, fieldnames: list[str]) -> None:
        self._output_path = output_path
        self._fieldnames = fieldnames
        self._file: TextIO | None = None
        self._writer: csv.DictWriter | None = None

    def __enter__(self) -> "_ResultCsvWriter":
        self._file = self._output_path.open(
            "w",
            encoding="utf-8-sig",
            newline="",
        )
        self._writer = csv.DictWriter(
            self._file,
            fieldnames=self._fieldnames,
            extrasaction="raise",
        )
        # 입력이 비어 루프가 한 번도 돌지 않아도 헤더는 남는다.
        self._writer.writeheader()
        self._file.flush()
        return self

    def write(self, row: dict[str, str]) -> None:
        if self._writer is None or self._file is None:
            raise RuntimeError("결과 CSV 기록기가 열려 있지 않습니다.")
        self._writer.writerow(row)
        self._file.flush()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _classification_label(
    classification_type: str,
    agent_code: str,
) -> str:
    """분리된 분류유형·에이전트 코드를 단일 의도 라벨로 만든다."""

    return agent_code if classification_type == "AGENT" else classification_type


def _safe_test_id(value: str | None, row_number: int) -> str:
    if value is None or not value.strip():
        return f"CSV_ROW_{row_number}"
    return " ".join(value.split())[:100]


def _progress_percent(completed: int, total: int) -> float:
    if total == 0:
        return 100.0
    return completed / total * 100


def _remaining_text(
    *,
    elapsed_seconds: float,
    completed: int,
    total: int,
) -> str:
    """지금까지의 평균 처리속도로 남은 시간을 mm:ss 또는 hh:mm:ss로 만든다."""

    if completed <= 0 or completed >= total:
        return "00:00"
    remaining = elapsed_seconds / completed * (total - completed)
    minutes, seconds = divmod(int(remaining), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _diagnosis_text(result: dict[str, str]) -> str:
    """PASS가 아닌 행만 원인을 한 줄 안에서 짧게 덧붙인다."""

    if result["test_result"] == "PASS":
        return ""
    reason = result["failure_reason"] or result["error_message"]
    if not reason:
        return ""
    compact = " ".join(reason.split())
    if len(compact) > 120:
        compact = f"{compact[:117]}..."
    return f" | 사유={compact}"
