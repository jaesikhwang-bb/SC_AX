"""같은 범위의 사용자 발화에서 명시한 단일 조회 월만 추출한다."""

import re
from datetime import date
from typing import Any


_MONTH = re.compile(
    r"(?<!\d)(?:(?P<year>\d{4})\s*년\s*)?(?P<month>1[0-2]|0?[1-9])\s*월"
    r"|(?<!\d)(?P<compact_year>(?:19|20)\d{2})(?P<compact_month>0[1-9]|1[0-2])(?!\d)"
)
_OTHER_PERIOD = re.compile(
    r"\d+\s*(?:년|월|일|개월|분기)|\d{4}[-./]\d|"
    r"오늘|어제|내일|이번\s*(?:달|월|주|분기|해)|지난\s*(?:달|월|주|분기|해)|"
    r"저번\s*(?:달|월|주)|다음\s*(?:달|월|주)|당월|전월|금월|작년|올해|내년|"
    r"최근|현재|지금|요즘|전체\s*기간|기간\s*(?:없이|무관|초기화)|"
    r"연간|상반기|하반기|전년도|금년|전년|지난해|금일|전일|월별|연도별|"
    r"말일|초일|부터|까지|이후|이전|~"
)


def has_period(text: str) -> bool:
    return bool(_MONTH.search(text) or _OTHER_PERIOD.search(text))


def single_month(text: str) -> str | None:
    """범위·상대기간·일자와 섞인 표현은 단일 월로 축약하지 않는다."""
    matches = list(_MONTH.finditer(text))
    if len(matches) != 1 or _OTHER_PERIOD.search(_MONTH.sub("", text)):
        return None
    match = matches[0]
    year = match.group("year") or match.group("compact_year")
    month = int(match.group("month") or match.group("compact_month"))
    return f"{year}년 {month}월" if year else f"{month}월"


def active_month(history: list[dict[str, Any]]) -> str | None:
    """최신 사용자 지정 기간을 찾는다. 다른 기간 표현을 만나면 이전 월로 돌아가지 않는다."""
    for item in reversed(history):
        if str(item.get("role", "")).casefold() != "user":
            continue
        content = str(item.get("content", ""))
        if has_period(content):
            return single_month(content)
    return None


def month_parameter(query: str, today: date) -> str | None:
    period = single_month(query)
    if period is None:
        return None
    match = _MONTH.fullmatch(period)
    assert match is not None
    year = int(match.group("year") or today.year)
    return f"{year:04d}{int(match.group('month')):02d}"
