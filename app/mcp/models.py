"""MCP JSON-RPC 호출 결과에 사용하는 공통 Pydantic 모델."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class McpExecutionResult(BaseModel):
    """GenOS MCP 도구 호출 정보와 structuredContent 원본을 담는 결과."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    tool_name: str
    request_id: str
    arguments: dict[str, Any]
    succeeded: bool
    # guide.ipynb와 동일하게 JSON-RPC 응답의
    # result.structuredContent만 추출한다. MCP 도구가 반환한 dict를
    # 별도의 문자열 변환이나 답변 생성 없이 그대로 API에 전달한다.
    result: dict[str, Any] | None = None
    error: str | None = None
