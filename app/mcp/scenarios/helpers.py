"""함수형 시나리오에서 공유하는 날짜와 문자열 값 변환 helper."""

from datetime import date, timedelta
import re
from collections.abc import Mapping
from typing import Any

def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def keyword_list(value: Any, *, fallback_query: str) -> list[str]:
    """서브에이전트 keywords를 정규화하고 누락 시 보정 질문으로 대체한다.

    RAG 검색은 사용자 재입력 Action을 사용하지 않는다. 구조화 출력이 빈 배열인
    경우에도 검색이 중단되지 않도록 보정 질문에서 의미 있는 어절을 추출하고,
    그래도 없으면 보정 질문 전체를 하나의 keyword로 사용한다.
    """

    raw_items: list[Any]
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = re.split(r"[,|]", value)
    else:
        raw_items = []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        keyword = str(item).strip()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        normalized.append(keyword)
    if normalized:
        return normalized[:10]

    query = str(fallback_query).strip()
    fallback = [
        token
        for token in re.findall(r"[가-힣A-Za-z0-9]{2,}", query)
        if token not in {"알려줘", "해주세요", "궁금해", "문의"}
    ]
    if fallback:
        return list(dict.fromkeys(fallback))[:10]
    return [query] if query else []


def month_value(
    parameters: Mapping[str, Any],
    *,
    today: date,
    default_month: str,
) -> str:
    extracted = text(parameters.get("closing_year_month"))
    if extracted:
        return extracted
    if default_month == "CURRENT":
        return today.strftime("%Y%m")
    if default_month == "PREVIOUS":
        return (today.replace(day=1) - timedelta(days=1)).strftime("%Y%m")
    raise ValueError(f"지원하지 않는 월 기본값 코드입니다: {default_month}")


def month_and_reference_date(
    parameters: Mapping[str, Any],
    *,
    today: date,
    default_month: str,
) -> tuple[str, str]:
    reference_date = text(parameters.get("reference_date"))
    if reference_date:
        return "", reference_date
    return month_value(
        parameters,
        today=today,
        default_month=default_month,
    ), ""
