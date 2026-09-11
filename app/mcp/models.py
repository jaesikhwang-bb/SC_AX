"""MCP JSON-RPC 호출 결과에 사용하는 공통 Pydantic 모델."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


# MCP 내부 오류 원문과 사용자에게 보여 줄 문구를 분리한다. 이 문구는
# PERFORMANCE_FEE뿐 아니라 향후 모든 조회형·RAG MCP에서 공통으로 사용한다.
MCP_NO_DATA_MESSAGE = "조회 결과가 없습니다."
MCP_SAFE_ERROR_MESSAGE = "조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

McpOutcome = Literal["SUCCESS", "NO_DATA", "ERROR"]


class McpExecutionResult(BaseModel):
    """GenOS MCP 도구 호출 정보와 structuredContent 원본을 담는 결과."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    tool_name: str
    request_id: str
    arguments: dict[str, Any]
    succeeded: bool
    # succeeded는 MCP 전송 자체의 성공 여부를 유지하고, outcome은 업무 응답의
    # 의미를 구분한다. code=1001은 HTTP/MCP 호출은 성공했지만 데이터가 없는
    # NO_DATA이며, 파싱·HTTP·도구 오류는 ERROR다.
    outcome: McpOutcome = "SUCCESS"
    business_code: str | None = None
    # 프론트에 전달해도 안전한 고정 문구다. error의 내부 오류 원문과 분리한다.
    user_message: str | None = None
    # guide.ipynb와 동일하게 JSON-RPC 응답의
    # result.structuredContent만 추출해 원본 진단 데이터로 보존한다.
    # 사용자 답변에는 아래 formatted_result를 사용한다.
    result: dict[str, Any] | None = None
    # 조회형 MCP는 원본 structuredContent를 보존한 채 별도의 결과 어댑터가
    # 화면 답변에 사용할 정규화 결과를 만든다. RAG는 raw.rag 형식으로 원본을
    # 그대로 사용하며 향후 문서 전용 어댑터를 독립적으로 추가할 수 있다.
    result_format: str = "raw"
    formatted_result: dict[str, Any] | None = None
    # 서버 로그와 로컬 추적을 위한 내부 오류다. 최종 답변에 직접 노출하지 않는다.
    error: str | None = None
    # 조회 결과를 답변으로 전송한 뒤 추가로 표시할 HITL action이다. MCP 원본과
    # 분리된 최소 메타만 보관하며, 현재 RP 아파트 다음 페이지 조회에 사용한다.
    post_answer_action: dict[str, Any] | None = None
    # 한 세부 시나리오에서 MCP가 여러 번 순차 호출될 때의 추적 메타데이터다.
    # mcp_results에는 시나리오별 terminal 결과만, graph state의
    # mcp_workflow_results에는 아래 메타데이터를 가진 모든 step 결과를 보관한다.
    workflow_step_code: str = "FINAL"
    workflow_step_index: int = 0
    workflow_step_count: int = 1
    workflow_is_final: bool = True
    # for_each 또는 mapped step은 같은 workflow step을 0~N회 호출한다. 개별
    # 호출과 fan-in 합성 결과를 tester와 다음 step에서 구분하기 위한 값이다.
    workflow_execution_mode: Literal[
        "function",
        "function_many",
        "pagination",
    ] = "function"
    workflow_item_index: int | None = None
    workflow_item_count: int | None = None
    workflow_is_aggregate: bool = False
    # YAML step DSL이 아니라 등록된 Python 시나리오 함수가 실행한 호출인지
    # tester와 formatter에서 역추적할 수 있는 안정적인 registry code다.
    workflow_handler_code: str | None = None
