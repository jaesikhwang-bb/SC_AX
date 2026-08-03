"""로컬 CSV 단계 추적기의 파일 분리와 append 동작 테스트."""

import csv
from pathlib import Path

from app.csv_trace import LocalCsvTraceRecorder


def test_same_thread_updates_one_row_in_one_csv(tmp_path: Path) -> None:
    recorder = LocalCsvTraceRecorder(tmp_path, "acqsc")
    base = {
        "thread_id": "thread-1",
        "conversation_id": "conversation-1",
        "employee_id": "10001",
        "message": "이번 달 실적 알려줘",
    }

    recorder.record("요청도착", base)
    recorder.record(
        "마스터의도분류완료",
        {
            **base,
            "classification": {
                "classification_type": "AGENT",
                "agent_code": "PERFORMANCE_FEE",
                "refined_query": "이번 달 나의 실적을 알려줘",
            },
        },
        elapsed_seconds=1.245,
    )

    files = list(tmp_path.glob("*.csv"))
    assert len(files) == 1
    with files[0].open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 1
    assert rows[0]["현재단계"] == "마스터의도분류완료"
    assert rows[0]["마스터에이전트코드"] == "PERFORMANCE_FEE"
    assert rows[0]["마스터분류소요시간_초"] == "1.245"
    assert rows[0]["최초요청일시"]
    assert rows[0]["최종갱신일시"]
    assert "요청도착" in rows[0]["처리단계이력_JSON"]
    assert "마스터의도분류완료" in rows[0]["처리단계이력_JSON"]


def test_different_threads_create_rows_in_same_file(tmp_path: Path) -> None:
    recorder = LocalCsvTraceRecorder(tmp_path, "acqsc")
    recorder.record("요청도착", {"thread_id": "thread-1"})
    recorder.record("요청도착", {"thread_id": "thread-2"})

    files = list(tmp_path.glob("*.csv"))
    assert [path.name for path in files] == [
        "intent_classification_trace.csv"
    ]
    with files[0].open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [row["thread_id"] for row in rows] == ["thread-1", "thread-2"]
