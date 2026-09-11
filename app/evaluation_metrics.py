"""외부 통계 패키지 없이 분류 성능 지표를 계산한다."""

from collections import Counter
from collections.abc import Iterable, Sequence
from statistics import stdev


def compute_latency_metrics(
    latencies_seconds: Sequence[float],
    *,
    wall_seconds: float,
) -> dict[str, float]:
    """행별 지연시간과 전체 경과시간으로 응답 성능 지표를 계산한다."""

    # 백분위수를 계산하려면 관측값이 오름차순이어야 한다. 이 함수는 호출자가
    # 전달한 원본 리스트를 변경하지 않도록 새 리스트를 만든다.
    values = sorted(float(value) for value in latencies_seconds)
    total = sum(values)
    count = len(values)
    return {
        "latency_mean_s": round(total / count, 4) if count else 0.0,
        "latency_min_s": round(values[0], 4) if values else 0.0,
        "latency_p50_s": round(_percentile(values, 50), 4),
        "latency_p90_s": round(_percentile(values, 90), 4),
        "latency_p95_s": round(_percentile(values, 95), 4),
        "latency_p99_s": round(_percentile(values, 99), 4),
        "latency_max_s": round(values[-1], 4) if values else 0.0,
        # 표본 표준편차는 관측값이 2건 이상일 때만 정의된다.
        "latency_stdev_s": round(stdev(values), 4) if count > 1 else 0.0,
        "latency_total_s": round(total, 4),
        "wall_s": round(wall_seconds, 4),
        "throughput_qps": (round(count / wall_seconds, 4) if wall_seconds > 0 else 0.0),
    }


def compute_latency_breakdown(
    samples: Sequence[tuple[str, float]],
) -> dict[str, dict[str, float]]:
    """라벨·판정 같은 그룹별 응답 속도를 평균이 느린 순으로 계산한다.

    어떤 의도나 어떤 판정에서 응답이 느려지는지 확인하기 위한 지표이므로
    전체 분포 지표와 달리 그룹 단위 표본 수(``count``)를 함께 반환한다.
    """

    grouped: dict[str, list[float]] = {}
    for group, seconds in samples:
        grouped.setdefault(group, []).append(float(seconds))

    breakdown: dict[str, dict[str, float]] = {}
    for group, values in grouped.items():
        ordered = sorted(values)
        total = sum(ordered)
        breakdown[group] = {
            "count": len(ordered),
            "mean_s": round(total / len(ordered), 4),
            "p50_s": round(_percentile(ordered, 50), 4),
            "p95_s": round(_percentile(ordered, 95), 4),
            "max_s": round(ordered[-1], 4),
            "total_s": round(total, 4),
        }
    # 느린 그룹을 먼저 보여 주면 개선 대상을 바로 찾을 수 있다.
    return dict(
        sorted(
            breakdown.items(),
            key=lambda item: (-item[1]["mean_s"], item[0]),
        )
    )


def compute_classification_metrics(
    pairs: Sequence[tuple[str, str]],
    *,
    labels: Iterable[str] | None = None,
) -> dict:
    """정답·예측 라벨 쌍으로 accuracy와 precision/recall/f1을 계산한다.

    평균은 원본 모집인용 평가기와 동일하게 정답 support가 1건 이상인
    라벨만 대상으로 계산한다. ``labels`` 밖의 예측값(예: ``ERROR``)도
    accuracy와 정답 라벨의 recall에는 실패로 반영된다.
    """

    # 명시된 라벨 순서는 혼동행렬과 SVG 축의 표시 순서로도 사용된다.
    # dict.fromkeys를 이용해 최초 등장 순서를 유지하면서 중복만 제거한다.
    normalized_labels = (
        list(dict.fromkeys(labels))
        if labels is not None
        else sorted({value for pair in pairs for value in pair})
    )
    total = len(pairs)
    # 라벨마다 전체 쌍을 다시 훑지 않도록 한 번의 집계로 모든 빈도를 구한다.
    pair_counts = Counter(pairs)
    true_counts = Counter(true for true, _ in pairs)
    predicted_counts = Counter(predicted for _, predicted in pairs)
    correct = sum(
        count for (true, predicted), count in pair_counts.items() if true == predicted
    )

    raw_per_label: dict[str, dict[str, float | int]] = {}
    for label in normalized_labels:
        true_positive = pair_counts[(label, label)]
        support = true_counts[label]
        false_positive = predicted_counts[label] - true_positive
        false_negative = support - true_positive
        precision = _safe_divide(
            true_positive,
            true_positive + false_positive,
        )
        recall = _safe_divide(
            true_positive,
            true_positive + false_negative,
        )
        f1 = _safe_divide(2 * precision * recall, precision + recall)
        raw_per_label[label] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }

    per_label = {
        label: {
            "precision": round(float(metrics["precision"]), 4),
            "recall": round(float(metrics["recall"]), 4),
            "f1": round(float(metrics["f1"]), 4),
            "support": int(metrics["support"]),
        }
        for label, metrics in raw_per_label.items()
    }

    # 평가 데이터에 정답으로 한 번도 등장하지 않은 라벨은 macro/weighted
    # 평균에서 제외한다. 원본 모집인용 평가 코드와 동일한 계산 기준이다.
    present_labels = [
        label for label in normalized_labels if raw_per_label[label]["support"] > 0
    ]
    # 0인 셀은 JSON 크기를 줄이기 위해 저장하지 않는 희소 딕셔너리 형태다.
    confusion: dict[str, dict[str, int]] = {}
    for true_label in normalized_labels:
        row: dict[str, int] = {}
        for predicted_label in normalized_labels:
            count = pair_counts[(true_label, predicted_label)]
            if count:
                row[predicted_label] = count
        confusion[true_label] = row

    return {
        "accuracy": round(_safe_divide(correct, total), 4),
        "total": total,
        "labels": normalized_labels,
        "per_label": per_label,
        "macro_avg": _average_metrics(
            raw_per_label,
            present_labels,
            weighted=False,
        ),
        "weighted_avg": _average_metrics(
            raw_per_label,
            present_labels,
            weighted=True,
        ),
        "confusion": confusion,
    }


def _average_metrics(
    per_label: dict[str, dict[str, float | int]],
    labels: list[str],
    *,
    weighted: bool,
) -> dict[str, float]:
    if not labels:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    denominator = (
        sum(int(per_label[label]["support"]) for label in labels)
        if weighted
        else len(labels)
    )
    averages = {}
    for metric_name in ("precision", "recall", "f1"):
        numerator = sum(
            float(per_label[label][metric_name])
            * (int(per_label[label]["support"]) if weighted else 1)
            for label in labels
        )
        averages[metric_name] = round(
            _safe_divide(numerator, denominator),
            4,
        )
    return averages


def _safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    """정렬된 관측값에서 선형 보간 방식의 백분위수를 구한다."""

    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * (percentile / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction
