"""답변 본문과 별도로 전송할 화면 확장 데이터의 공통 모델과 생성 함수."""

from collections.abc import Mapping, Sequence
from html import escape
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MessageRenderable(BaseModel):
    """프론트가 메시지 아래에 렌더링할 수 있는 확장 데이터 한 건.

    ``type``과 ``format``은 문자열로 열어 두어 table 외에도 chart, card, file,
    link 등 새로운 표현을 공통 메시지 계약을 바꾸지 않고 추가할 수 있다.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    type: str
    format: str
    title: str = ""
    content: str | None = None
    data: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScenarioAnswer(BaseModel):
    """세부 시나리오 포맷 함수가 반환하는 본문과 확장 데이터."""

    model_config = ConfigDict(extra="forbid")

    text: str
    renderables: list[MessageRenderable] = Field(default_factory=list)


def extract_value(
    source: Any,
    obj_id: str,
    default: Any = "",
    *,
    occurrence: int = 0,
) -> Any:
    """MCP 결과에서 원하는 objId의 objVal을 한 건 꺼낸다.

    다음 입력을 모두 지원한다.

    - 필터링된 ``list[dict]``
    - ``structuredContent`` 형태의 ``{"data": [...]}``
    - JSON-RPC 전체 응답의 ``result.structuredContent.data``

    같은 objId가 여러 번 있으면 ``occurrence``로 순번을 선택한다.
    """

    target = str(obj_id).strip()
    if not target:
        return default

    values = [
        item.get("objVal")
        for item in extract_data_items(source)
        if str(item.get("objId", "")).strip() == target
    ]
    try:
        return values[occurrence]
    except IndexError:
        return default


def extract_data_items(source: Any) -> list[dict[str, Any]]:
    """여러 MCP 응답 형태에서 최종 data 항목 배열을 공통 추출한다.

    직접 list, structuredContent, JSON-RPC result, JSON 문자열과 흔히 사용하는
    items/rows/records 래퍼를 지원한다. 원본 값은 오류에 포함하지 않는다.
    """

    found = _find_data_items(source, depth=0)
    if found is None:
        raise ValueError(
            "MCP structuredContent.data는 list 형식이어야 합니다. "
            f"수신구조={_describe_shape(source)}"
        )
    return found


def create_markdown_table(
    columns: Sequence[Any],
    rows: Sequence[Sequence[Any]],
) -> str:
    """열과 행만 받아 Markdown 테이블 문자열을 만든다."""

    normalized_columns, normalized_rows = _normalize_table(columns, rows)
    header = (
        "| " + " | ".join(_markdown_cell(value) for value in normalized_columns) + " |"
    )
    separator = "| " + " | ".join("---" for _ in normalized_columns) + " |"
    body = [
        "| " + " | ".join(_markdown_cell(value) for value in row) + " |"
        for row in normalized_rows
    ]
    return "\n".join([header, separator, *body])


def create_html_table(
    columns: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    *,
    table_class: str = "agent-result-table",
) -> str:
    """열과 행만 받아 동적 값을 escape한 HTML 테이블 문자열을 만든다."""

    normalized_columns, normalized_rows = _normalize_table(columns, rows)
    header = "".join(
        f"<th>{escape(_display_text(value), quote=True)}</th>"
        for value in normalized_columns
    )
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(_display_text(value), quote=True)}</td>" for value in row
        )
        + "</tr>"
        for row in normalized_rows
    )
    safe_class = escape(table_class, quote=True)
    return (
        f'<table class="{safe_class}">'
        f"<thead><tr>{header}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table>"
    )


def create_table_renderable(
    *,
    code: str,
    columns: Sequence[Any],
    rows: Sequence[Sequence[Any]],
    title: str = "",
    format: str = "markdown",
    metadata: Mapping[str, Any] | None = None,
) -> MessageRenderable:
    """구조화된 열·행과 Markdown/HTML 문자열을 함께 가진 table을 만든다."""

    normalized_columns, normalized_rows = _normalize_table(columns, rows)
    normalized_format = str(format).strip().lower()
    if normalized_format == "markdown":
        content = create_markdown_table(normalized_columns, normalized_rows)
    elif normalized_format == "html":
        content = create_html_table(normalized_columns, normalized_rows)
    else:
        raise ValueError(
            f"table renderable format은 markdown 또는 html이어야 합니다: {format}"
        )
    return MessageRenderable(
        code=code,
        type="table",
        format=normalized_format,
        title=title,
        content=content,
        data={
            "columns": normalized_columns,
            "rows": normalized_rows,
        },
        metadata=dict(metadata or {}),
    )


def create_renderable(
    *,
    code: str,
    type: str,
    format: str,
    title: str = "",
    content: str | None = None,
    data: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> MessageRenderable:
    """표가 아닌 차트·카드·파일 등 범용 확장 데이터를 간단히 만든다."""

    return MessageRenderable(
        code=code,
        type=type,
        format=format,
        title=title,
        content=content,
        data=data,
        metadata=dict(metadata or {}),
    )


def normalize_scenario_answer(
    value: Any,
    *,
    default_renderable_code: str,
) -> ScenarioAnswer:
    """신규 모델과 기존 문자열·``return answer, table``을 공통 모델로 바꾼다.

    권장 반환형은 ``ScenarioAnswer``이다. 다만 운영자가 간단히
    ``return answer, markdown_table``을 작성해도 동작하도록 호환한다.
    """

    if isinstance(value, ScenarioAnswer):
        return value
    if isinstance(value, str):
        return ScenarioAnswer(text=value)
    if not isinstance(value, tuple) or len(value) != 2:
        raise TypeError(
            "답변 포맷 함수는 str, ScenarioAnswer 또는 "
            "(answer, renderables)를 반환해야 합니다."
        )

    text, raw_renderables = value
    if not isinstance(text, str):
        raise TypeError("답변 포맷 함수의 answer는 str이어야 합니다.")

    if isinstance(raw_renderables, str):
        renderables = [
            MessageRenderable(
                code=default_renderable_code,
                type="table",
                format="markdown",
                content=raw_renderables,
            )
        ]
    elif isinstance(raw_renderables, (MessageRenderable, Mapping)):
        renderables = [MessageRenderable.model_validate(raw_renderables)]
    elif isinstance(raw_renderables, Sequence):
        renderables = [
            MessageRenderable.model_validate(item) for item in raw_renderables
        ]
    else:
        raise TypeError("renderables는 문자열, 객체 또는 객체 배열이어야 합니다.")
    return ScenarioAnswer(text=text, renderables=renderables)


def _find_data_items(
    source: Any,
    *,
    depth: int,
) -> list[dict[str, Any]] | None:
    """재귀 깊이를 제한해 data 목록을 찾고 각 항목을 dict로 검증한다."""

    if depth > 8:
        return None
    if isinstance(source, str):
        stripped = source.strip()
        if not stripped or stripped[0] not in "[{":
            return None
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        return _find_data_items(decoded, depth=depth + 1)
    if isinstance(source, list):
        items: list[dict[str, Any]] = []
        for index, item in enumerate(source):
            if not isinstance(item, Mapping):
                raise ValueError(
                    "MCP data 배열의 각 항목은 object 형식이어야 합니다. "
                    f"index={index}, item_type={type(item).__name__}"
                )
            items.append(dict(item))
        return items
    if not isinstance(source, Mapping):
        return None
    if "objId" in source:
        return [dict(source)]

    saw_wrapper = False
    for key in (
        "data",
        "structuredContent",
        "result",
        "items",
        "rows",
        "records",
    ):
        if key not in source:
            continue
        saw_wrapper = True
        found = _find_data_items(source[key], depth=depth + 1)
        if found is not None:
            return found

    # 일부 MCP 도구는 data를 objId/objVal 배열 대신 {컬럼명: 값} 객체로
    # 반환한다. 이 경우에도 이후 컬럼 필터와 formatter가 동일하게 동작하도록
    # 표준 objId/objType/objVal 항목 배열로 변환한다.
    if source and not saw_wrapper:
        return [
            {
                "objId": str(key),
                "objType": _mcp_value_type(value),
                "objVal": value,
            }
            for key, value in source.items()
        ]
    if not source and not saw_wrapper:
        return []
    return None


def _describe_shape(source: Any) -> dict[str, Any]:
    """민감한 실제 값을 제외하고 타입과 최상위 키만 오류 진단에 사용한다."""

    description: dict[str, Any] = {"type": type(source).__name__}
    if isinstance(source, Mapping):
        description["keys"] = sorted(str(key) for key in source.keys())
        if "data" in source:
            data = source.get("data")
            description["data_type"] = type(data).__name__
            if isinstance(data, Mapping):
                description["data_keys"] = sorted(str(key) for key in data.keys())
    elif isinstance(source, list):
        description["length"] = len(source)
    elif isinstance(source, str):
        description["string_length"] = len(source)
    return description


def _mcp_value_type(value: Any) -> str:
    """객체형 data를 표준 항목 배열로 바꿀 때 사용할 간단한 타입명."""

    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "string"


def _normalize_table(
    columns: Sequence[Any],
    rows: Sequence[Sequence[Any]],
) -> tuple[list[Any], list[list[Any]]]:
    """테이블 열과 행 개수를 검증하고 JSON 직렬화 가능한 list로 바꾼다."""

    normalized_columns = list(columns)
    if not normalized_columns:
        raise ValueError("columns는 한 개 이상이어야 합니다.")

    normalized_rows: list[list[Any]] = []
    for index, row in enumerate(rows):
        normalized_row = list(row)
        if len(normalized_row) != len(normalized_columns):
            raise ValueError(
                f"rows[{index}]의 값 개수가 열 개수와 다릅니다. "
                f"열={len(normalized_columns)}, 값={len(normalized_row)}"
            )
        normalized_rows.append(normalized_row)
    return normalized_columns, normalized_rows


def _markdown_cell(value: Any) -> str:
    """테이블 값을 Markdown 셀에서 안전한 문자열로 만든다."""

    return (
        _display_text(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
    )


def _display_text(value: Any) -> str:
    """None과 복합값을 화면용 문자열로 변환한다."""

    if value is None:
        return ""
    return str(value)
