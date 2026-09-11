"""평가 결과를 JSON/history.csv/SVG 리포트로 저장한다."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Mapping, Sequence


# 실행 간 비교에 필요한 값만 한 행으로 누적한다. 컬럼 순서는 기존 이력과의
# 스키마 호환성 검사에도 사용되므로 변경 시 마이그레이션을 함께 고려해야 한다.
HISTORY_COLUMNS = (
    "timestamp",
    "eval_type",
    # 마스터 분류만 돌린 실행과 MCP까지 돌린 실행은 응답 속도가 크게 다르므로
    # 추세를 볼 때 반드시 같은 단계끼리만 비교해야 한다.
    "stage",
    "model",
    "prompt_version",
    "dataset",
    "n",
    "passed",
    "failed",
    "errors",
    "accuracy",
    "macro_precision",
    "macro_recall",
    "macro_f1",
    "weighted_f1",
    "latency_mean_s",
    "latency_min_s",
    "latency_p50_s",
    "latency_p90_s",
    "latency_p95_s",
    "latency_p99_s",
    "latency_max_s",
    "latency_stdev_s",
    "latency_total_s",
    "wall_s",
    "throughput_qps",
)
# 폴더명으로 쓸 수 없는 문자만 걸러 내고 한글 데이터셋 이름은 그대로 살린다.
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^0-9A-Za-z가-힣._-]+")


@dataclass(frozen=True)
class EvaluationReportPaths:
    """CLI에 출력할 평가 산출물의 실제 저장 경로."""

    run_dir: Path
    json_path: Path
    history_path: Path
    visualization_path: Path


def save_evaluation_report(
    *,
    report_dir: Path,
    input_path: Path,
    output_path: Path,
    model: str,
    prompt_version: str,
    total: int,
    passed: int,
    failed: int,
    errors: int,
    classification_metrics: Mapping,
    timing_metrics: Mapping[str, float],
    stage: str = "full",
    latency_by_label: Mapping[str, Mapping[str, float]] | None = None,
    latency_by_result: Mapping[str, Mapping[str, float]] | None = None,
    now: datetime | None = None,
) -> EvaluationReportPaths:
    """한 번의 평가 상세와 누적 이력을 저장하고 SVG 대시보드를 만든다.

    산출물은 ``report_dir/<데이터셋 파일명>/`` 아래에 모으고 파일명은
    ``<데이터셋>_<단계>_<시각>`` 형식으로 만든다. 데이터셋이 여러 개여도
    이력과 그래프가 섞이지 않으므로 나중에 폴더 단위로 로그를 분석할 수 있다.
    """

    # 상세 JSON과 SVG는 실행마다 새 파일로 생성하고 history.csv만 누적한다.
    dataset_name = _safe_name(input_path.stem)
    run_dir = (report_dir / dataset_name).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp_text = timestamp.isoformat()
    file_stem = _unique_file_stem(
        run_dir,
        f"{dataset_name}_{_safe_name(stage)}",
        timestamp.strftime("%Y%m%d_%H%M%S"),
    )
    macro = classification_metrics["macro_avg"]
    weighted = classification_metrics["weighted_avg"]
    label_latency = dict(latency_by_label or {})
    result_latency = dict(latency_by_result or {})
    # JSON은 전체 지표를 보존하지만 history.csv에는 추세 분석용 요약값만 쓴다.
    history_row = {
        "timestamp": timestamp_text,
        "eval_type": "pipeline_intent_classification",
        "stage": stage,
        "model": model,
        "prompt_version": prompt_version,
        "dataset": input_path.name,
        "n": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "accuracy": classification_metrics["accuracy"],
        "macro_precision": macro["precision"],
        "macro_recall": macro["recall"],
        "macro_f1": macro["f1"],
        "weighted_f1": weighted["f1"],
        **{
            name: timing_metrics[name]
            for name in HISTORY_COLUMNS
            if name in timing_metrics
        },
    }
    report = {
        "timestamp": timestamp_text,
        "eval_type": history_row["eval_type"],
        "stage": stage,
        "model": model,
        "prompt_version": prompt_version,
        "dataset": str(input_path.resolve()),
        "prediction_csv": str(output_path.resolve()),
        "counts": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
        },
        "timing": dict(timing_metrics),
        "latency_by_label": {
            label: dict(values) for label, values in label_latency.items()
        },
        "latency_by_result": {
            name: dict(values) for name, values in result_latency.items()
        },
        "classification_metrics": dict(classification_metrics),
    }

    json_path = run_dir / f"{file_stem}.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    history_path = run_dir / "history.csv"
    _append_history(history_path, history_row)
    # 방금 추가된 실행을 포함한 전체 이력으로 추세 그래프를 그린다.
    history_rows = _read_history(history_path)
    visualization_path = run_dir / f"{file_stem}.svg"
    visualization_path.write_text(
        _render_svg(
            dataset_name,
            stage,
            classification_metrics,
            timing_metrics,
            label_latency,
            # 단계가 다르면 응답 속도와 판정 기준이 달라 추세를 함께 그릴 수
            # 없다. 같은 단계로 실행한 이력만 남긴다.
            [row for row in history_rows if (row.get("stage") or "full") == stage],
        ),
        encoding="utf-8",
    )
    return EvaluationReportPaths(
        run_dir=run_dir,
        json_path=json_path,
        history_path=history_path,
        visualization_path=visualization_path,
    )


def _safe_name(name: str) -> str:
    """데이터셋 파일명을 폴더·파일 이름으로 쓸 수 있게 정규화한다."""

    cleaned = _UNSAFE_NAME_CHARACTERS.sub("_", name).strip("._ ")
    return cleaned[:80] or "dataset"


def _unique_file_stem(run_dir: Path, dataset_name: str, stamp: str) -> str:
    """같은 초에 두 번 실행돼도 이전 산출물을 덮어쓰지 않도록 한다."""

    stem = f"{dataset_name}_{stamp}"
    suffix = 2
    while (run_dir / f"{stem}.json").exists() or (run_dir / f"{stem}.svg").exists():
        stem = f"{dataset_name}_{stamp}_{suffix}"
        suffix += 1
    return stem


def _append_history(path: Path, row: Mapping) -> None:
    """스키마가 같은 이력 파일에만 새 실행 결과를 추가한다."""

    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as history_file:
            existing_header = next(csv.reader(history_file), [])
        if existing_header != list(HISTORY_COLUMNS):
            raise ValueError(f"평가 이력 CSV 헤더가 현재 형식과 다릅니다: {path}")
    is_new = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as history_file:
        writer = csv.DictWriter(history_file, fieldnames=HISTORY_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in HISTORY_COLUMNS})


def _read_history(path: Path) -> list[dict[str, str]]:
    """SVG 추세 그래프에 사용할 누적 이력을 읽는다."""

    with path.open("r", encoding="utf-8-sig", newline="") as history_file:
        return list(csv.DictReader(history_file))


def _render_svg(
    dataset_name: str,
    stage: str,
    metrics: Mapping,
    timing: Mapping[str, float],
    latency_by_label: Mapping[str, Mapping[str, float]],
    history: Sequence[Mapping[str, str]],
) -> str:
    """품질·라벨별 F1·혼동행렬·응답속도·추세를 하나의 SVG로 구성한다."""

    width, height = 1400, 1650
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,sans-serif;fill:#172033}.title{font-size:26px;font-weight:700}.panel{fill:#fff;stroke:#d9e1ec;stroke-width:1}.heading{font-size:17px;font-weight:700}.small{font-size:12px}.axis{stroke:#9aa9bb;stroke-width:1}.grid{stroke:#e8edf3;stroke-width:1}.value{font-size:12px;font-weight:700}</style>",
        '<rect width="100%" height="100%" fill="#f5f7fa"/>',
        f'<text x="40" y="44" class="title">Pipeline intent evaluation · {escape(dataset_name)} · stage={escape(stage)}</text>',
        f'<text x="40" y="68" class="small">n={metrics["total"]} · mean latency={timing.get("latency_mean_s", 0)}s · p95={timing.get("latency_p95_s", 0)}s · max={timing.get("latency_max_s", 0)}s · wall={timing.get("wall_s", 0)}s · throughput={timing.get("throughput_qps", 0)} q/s</text>',
    ]
    parts.extend(_quality_panel(metrics, 30, 90, 650, 280))
    parts.extend(_label_panel(metrics, 720, 90, 650, 280))
    parts.extend(_confusion_panel(metrics, 30, 400, 650, 620))
    parts.extend(_history_panel(history, 720, 400, 650, 620))
    parts.extend(_latency_panel(timing, 30, 1040, 650, 300))
    parts.extend(_latency_label_panel(latency_by_label, 720, 1040, 650, 300))
    parts.extend(_latency_trend_panel(history, 30, 1360, 1340, 260))
    parts.append("</svg>")
    return "\n".join(parts)


def _quality_panel(metrics: Mapping, x: int, y: int, w: int, h: int) -> list[str]:
    """accuracy, macro F1, weighted F1을 가로 막대로 표시한다."""

    values = [
        ("accuracy", float(metrics["accuracy"]), "#2563eb"),
        ("macro F1", float(metrics["macro_avg"]["f1"]), "#0f9f7a"),
        ("weighted F1", float(metrics["weighted_avg"]["f1"]), "#7c3aed"),
    ]
    parts = [
        _panel(x, y, w, h),
        f'<text x="{x + 20}" y="{y + 30}" class="heading">Quality summary</text>',
    ]
    chart_x, chart_y, chart_w = x + 120, y + 65, w - 155
    for index, (label, value, color) in enumerate(values):
        row_y = chart_y + index * 58
        parts.extend(
            [
                f'<text x="{chart_x - 10}" y="{row_y + 17}" text-anchor="end" class="small">{label}</text>',
                f'<rect x="{chart_x}" y="{row_y}" width="{chart_w}" height="24" rx="4" fill="#e8edf3"/>',
                f'<rect x="{chart_x}" y="{row_y}" width="{chart_w * value:.1f}" height="24" rx="4" fill="{color}"/>',
                f'<text x="{chart_x + chart_w * value + 7:.1f}" y="{row_y + 17}" class="value">{value:.4f}</text>',
            ]
        )
    return parts


def _label_panel(metrics: Mapping, x: int, y: int, w: int, h: int) -> list[str]:
    """정답 support가 있는 라벨의 F1과 표본 수를 표시한다."""

    labels = [
        (label, values)
        for label, values in metrics["per_label"].items()
        if int(values["support"]) > 0
    ]
    parts = [
        _panel(x, y, w, h),
        f'<text x="{x + 20}" y="{y + 30}" class="heading">F1 by true label</text>',
    ]
    if not labels:
        parts.append(
            f'<text x="{x + w / 2}" y="{y + h / 2}" text-anchor="middle">No classified rows</text>'
        )
        return parts
    chart_x, chart_y, chart_w = x + 175, y + 55, w - 215
    row_h = min(34, 190 / len(labels))
    for index, (label, values) in enumerate(labels):
        row_y = chart_y + index * row_h
        score = float(values["f1"])
        parts.extend(
            [
                f'<text x="{chart_x - 8}" y="{row_y + 14}" text-anchor="end" class="small">{escape(str(label))} ({values["support"]})</text>',
                f'<rect x="{chart_x}" y="{row_y}" width="{chart_w}" height="18" rx="3" fill="#e8edf3"/>',
                f'<rect x="{chart_x}" y="{row_y}" width="{chart_w * score:.1f}" height="18" rx="3" fill="#0f9f7a"/>',
                f'<text x="{chart_x + chart_w + 6}" y="{row_y + 14}" class="small">{score:.2f}</text>',
            ]
        )
    return parts


def _confusion_panel(metrics: Mapping, x: int, y: int, w: int, h: int) -> list[str]:
    """실제/예측 라벨 조합을 색 농도와 건수로 표시한다."""

    labels = [
        label
        for label in metrics["labels"]
        if int(metrics["per_label"][label]["support"]) > 0
        or any(
            metrics["confusion"].get(row, {}).get(label, 0) for row in metrics["labels"]
        )
    ]
    parts = [
        _panel(x, y, w, h),
        f'<text x="{x + 20}" y="{y + 30}" class="heading">Confusion matrix · true rows / predicted columns</text>',
    ]
    if not labels:
        parts.append(
            f'<text x="{x + w / 2}" y="{y + h / 2}" text-anchor="middle">No classified rows</text>'
        )
        return parts
    matrix = [
        [int(metrics["confusion"].get(true, {}).get(pred, 0)) for pred in labels]
        for true in labels
    ]
    max_count = max(max(row) for row in matrix) or 1
    cell = min(52, (w - 190) / len(labels), (h - 150) / len(labels))
    start_x, start_y = x + 165, y + 95
    for column, label in enumerate(labels):
        parts.append(
            f'<text x="{start_x + (column + 0.5) * cell:.1f}" y="{start_y - 8}" text-anchor="middle" class="small" transform="rotate(-35 {start_x + (column + 0.5) * cell:.1f} {start_y - 8})">{escape(str(label))}</text>'
        )
    for row, true_label in enumerate(labels):
        parts.append(
            f'<text x="{start_x - 8}" y="{start_y + (row + 0.5) * cell + 4:.1f}" text-anchor="end" class="small">{escape(str(true_label))}</text>'
        )
        for column, count in enumerate(matrix[row]):
            opacity = 0.12 + 0.78 * count / max_count
            cell_x, cell_y = start_x + column * cell, start_y + row * cell
            parts.extend(
                [
                    f'<rect x="{cell_x:.1f}" y="{cell_y:.1f}" width="{cell - 2:.1f}" height="{cell - 2:.1f}" fill="#2563eb" opacity="{opacity:.3f}"/>',
                    f'<text x="{cell_x + cell / 2:.1f}" y="{cell_y + cell / 2 + 4:.1f}" text-anchor="middle" class="value">{count}</text>',
                ]
            )
    return parts


def _latency_panel(
    timing: Mapping[str, float], x: int, y: int, w: int, h: int
) -> list[str]:
    """행별 응답 속도의 분포를 최소·중앙·평균·p90·p95·최대로 표시한다."""

    rows = [
        ("min", float(timing.get("latency_min_s", 0.0)), "#94a3b8"),
        ("p50", float(timing.get("latency_p50_s", 0.0)), "#2563eb"),
        ("mean", float(timing.get("latency_mean_s", 0.0)), "#0f9f7a"),
        ("p90", float(timing.get("latency_p90_s", 0.0)), "#f59e0b"),
        ("p95", float(timing.get("latency_p95_s", 0.0)), "#ef7c1a"),
        ("max", float(timing.get("latency_max_s", 0.0)), "#dc2626"),
    ]
    parts = [
        _panel(x, y, w, h),
        f'<text x="{x + 20}" y="{y + 30}" class="heading">Latency per row (seconds)</text>',
        f'<text x="{x + 20}" y="{y + 50}" class="small">stdev={timing.get("latency_stdev_s", 0)}s · total={timing.get("latency_total_s", 0)}s · wall={timing.get("wall_s", 0)}s</text>',
    ]
    scale = max((value for _, value, _ in rows), default=0.0) or 1.0
    chart_x, chart_y, chart_w = x + 75, y + 66, w - 150
    for index, (label, value, color) in enumerate(rows):
        row_y = chart_y + index * 36
        parts.extend(
            [
                f'<text x="{chart_x - 10}" y="{row_y + 15}" text-anchor="end" class="small">{label}</text>',
                f'<rect x="{chart_x}" y="{row_y}" width="{chart_w}" height="20" rx="3" fill="#e8edf3"/>',
                f'<rect x="{chart_x}" y="{row_y}" width="{max(chart_w * value / scale, 1):.1f}" height="20" rx="3" fill="{color}"/>',
                f'<text x="{chart_x + chart_w + 6}" y="{row_y + 15}" class="small">{value:.2f}s</text>',
            ]
        )
    return parts


def _latency_label_panel(
    latency_by_label: Mapping[str, Mapping[str, float]],
    x: int,
    y: int,
    w: int,
    h: int,
) -> list[str]:
    """의도 라벨별 평균 응답 속도를 느린 순으로 표시한다."""

    parts = [
        _panel(x, y, w, h),
        f'<text x="{x + 20}" y="{y + 30}" class="heading">Mean latency by expected label</text>',
    ]
    if not latency_by_label:
        parts.append(
            f'<text x="{x + w / 2}" y="{y + h / 2}" text-anchor="middle">No latency samples</text>'
        )
        return parts
    # 이미 평균이 느린 순으로 정렬돼 있으므로 상위 구간만 보여 준다.
    entries = list(latency_by_label.items())[:7]
    scale = max(float(values["mean_s"]) for _, values in entries) or 1.0
    # 오른쪽에는 평균과 p95를 함께 적으므로 막대 너비보다 여백을 넉넉히 둔다.
    chart_x, chart_y, chart_w = x + 185, y + 55, w - 310
    row_h = min(33, 220 / len(entries))
    for index, (label, values) in enumerate(entries):
        row_y = chart_y + index * row_h
        mean_seconds = float(values["mean_s"])
        parts.extend(
            [
                f'<text x="{chart_x - 8}" y="{row_y + 14}" text-anchor="end" class="small">{escape(str(label))} ({int(values["count"])})</text>',
                f'<rect x="{chart_x}" y="{row_y}" width="{chart_w}" height="18" rx="3" fill="#e8edf3"/>',
                f'<rect x="{chart_x}" y="{row_y}" width="{max(chart_w * mean_seconds / scale, 1):.1f}" height="18" rx="3" fill="#2563eb"/>',
                f'<text x="{chart_x + chart_w + 6}" y="{row_y + 14}" class="small">{mean_seconds:.2f}s (p95 {float(values["p95_s"]):.1f})</text>',
            ]
        )
    return parts


def _history_panel(
    history: Sequence[Mapping[str, str]], x: int, y: int, w: int, h: int
) -> list[str]:
    """history.csv에 누적된 실행별 품질 지표의 변화를 표시한다."""

    parts = [
        _panel(x, y, w, h),
        f'<text x="{x + 20}" y="{y + 30}" class="heading">Quality trend · {len(history)} run(s) of same stage</text>',
    ]
    chart_x, chart_y = x + 65, y + 65
    chart_w, chart_h = w - 95, h - 115
    for tick in range(6):
        tick_y = chart_y + chart_h - chart_h * tick / 5
        parts.extend(
            [
                f'<line x1="{chart_x}" y1="{tick_y:.1f}" x2="{chart_x + chart_w}" y2="{tick_y:.1f}" class="grid"/>',
                f'<text x="{chart_x - 8}" y="{tick_y + 4:.1f}" text-anchor="end" class="small">{tick / 5:.1f}</text>',
            ]
        )
    parts.extend(
        [
            f'<line x1="{chart_x}" y1="{chart_y}" x2="{chart_x}" y2="{chart_y + chart_h}" class="axis"/>',
            f'<line x1="{chart_x}" y1="{chart_y + chart_h}" x2="{chart_x + chart_w}" y2="{chart_y + chart_h}" class="axis"/>',
        ]
    )
    series = [
        ("accuracy", "#2563eb"),
        ("macro_f1", "#0f9f7a"),
        ("weighted_f1", "#7c3aed"),
    ]
    denominator = max(1, len(history) - 1)
    for series_index, (name, color) in enumerate(series):
        points = []
        for index, row in enumerate(history):
            point_x = chart_x + chart_w * index / denominator
            value = float(row.get(name) or 0)
            point_y = chart_y + chart_h * (1 - value)
            points.append(f"{point_x:.1f},{point_y:.1f}")
        if len(points) == 1:
            px, py = points[0].split(",")
            parts.append(f'<circle cx="{px}" cy="{py}" r="5" fill="{color}"/>')
        else:
            parts.append(
                f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"/>'
            )
        legend_x = chart_x + series_index * 150
        parts.extend(
            [
                f'<line x1="{legend_x}" y1="{y + h - 24}" x2="{legend_x + 25}" y2="{y + h - 24}" stroke="{color}" stroke-width="3"/>',
                f'<text x="{legend_x + 31}" y="{y + h - 20}" class="small">{name}</text>',
            ]
        )
    return parts


def _latency_trend_panel(
    history: Sequence[Mapping[str, str]],
    x: int,
    y: int,
    w: int,
    h: int,
) -> list[str]:
    """실행별 평균·p95·최대 응답 속도의 변화를 초 단위로 표시한다."""

    parts = [
        _panel(x, y, w, h),
        f'<text x="{x + 20}" y="{y + 30}" class="heading">Latency trend · {len(history)} run(s) of same stage</text>',
    ]
    series = [
        ("latency_mean_s", "#0f9f7a"),
        ("latency_p95_s", "#f59e0b"),
        ("latency_max_s", "#dc2626"),
    ]
    values = [[float(row.get(name) or 0) for row in history] for name, _ in series]
    scale = max((value for column in values for value in column), default=0.0) or 1.0
    chart_x, chart_y = x + 75, y + 50
    chart_w, chart_h = w - 110, h - 105
    for tick in range(5):
        tick_y = chart_y + chart_h - chart_h * tick / 4
        parts.extend(
            [
                f'<line x1="{chart_x}" y1="{tick_y:.1f}" x2="{chart_x + chart_w}" y2="{tick_y:.1f}" class="grid"/>',
                f'<text x="{chart_x - 8}" y="{tick_y + 4:.1f}" text-anchor="end" class="small">{scale * tick / 4:.1f}s</text>',
            ]
        )
    parts.extend(
        [
            f'<line x1="{chart_x}" y1="{chart_y}" x2="{chart_x}" y2="{chart_y + chart_h}" class="axis"/>',
            f'<line x1="{chart_x}" y1="{chart_y + chart_h}" x2="{chart_x + chart_w}" y2="{chart_y + chart_h}" class="axis"/>',
        ]
    )
    denominator = max(1, len(history) - 1)
    for series_index, ((name, color), column) in enumerate(zip(series, values)):
        points = []
        for index, value in enumerate(column):
            point_x = chart_x + chart_w * index / denominator
            point_y = chart_y + chart_h * (1 - value / scale)
            points.append(f"{point_x:.1f},{point_y:.1f}")
        if len(points) == 1:
            px, py = points[0].split(",")
            parts.append(f'<circle cx="{px}" cy="{py}" r="5" fill="{color}"/>')
        elif points:
            parts.append(
                f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"/>'
            )
        legend_x = chart_x + series_index * 170
        parts.extend(
            [
                f'<line x1="{legend_x}" y1="{y + h - 22}" x2="{legend_x + 25}" y2="{y + h - 22}" stroke="{color}" stroke-width="3"/>',
                f'<text x="{legend_x + 31}" y="{y + h - 18}" class="small">{name}</text>',
            ]
        )
    return parts


def _panel(x: int, y: int, width: int, height: int) -> str:
    return f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" class="panel"/>'
