"""세부 시나리오가 MCP 입력·결과·화면 출력을 자유롭게 구성하는 계약.

공통 runtime은 MCP 호출 안전성, 인증, 원장과 추적만 담당한다. 특정 업무의
arguments 구성, 여러 도구의 연결, 결과 파싱·집계, 표·카드·문장 생성은 모두
각 시나리오의 Python 함수가 이 계약을 통해 결정한다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.mcp.models import McpExecutionResult
from app.renderables import ScenarioAnswer, extract_data_items
from app.subagents.models import SubagentResult


@dataclass(frozen=True)
class ScenarioMcpOutputContext:
    """세부 시나리오 결과 전처리 함수에 전달하는 안전한 전체 문맥.

    ``execution.result``는 terminal MCP 원본/집계 결과이며, ``workflow_results``는
    앞선 모든 호출의 원장이다. 따라서 출력 함수는 특정 ``objId``만 선택할 수도,
    페이지별 ``batches``·호출 arguments·다단계 도구 결과를 직접 조합할 수도 있다.
    """

    execution: McpExecutionResult
    subagent: SubagentResult
    employee_id: str
    session_id: str
    thread_id: str
    request_context: Mapping[str, Any]
    workflow_results: Sequence[McpExecutionResult] = ()

    @property
    def parameters(self) -> dict[str, Any]:
        """현재 세부 시나리오가 추출·보정한 파라미터 복사본."""

        return dict(self.subagent.parameters)

    @property
    def raw_result(self) -> Any:
        """terminal MCP 결과를 가공하지 않은 형태로 반환한다."""

        return self.execution.result

    def data_items(self) -> list[dict[str, Any]]:
        """원본/집계 결과에서 data 목록을 추출한다.

        이 함수는 편의 기능일 뿐 필수가 아니다. row 기반·비표준 MCP 결과는
        ``raw_result`` 또는 ``workflow_results``를 직접 읽어 시나리오별로 처리한다.
        """

        return extract_data_items(self.execution.result)

    def results_for(self, step_code: str) -> list[McpExecutionResult]:
        """다단계 시나리오에서 특정 step의 호출·집계 결과를 모두 반환한다."""

        normalized = str(step_code).strip()
        return [
            item
            for item in self.workflow_results
            if item.workflow_step_code == normalized
        ]


@dataclass(frozen=True)
class ScenarioMcpOutput:
    """세부 시나리오 출력 전처리 함수의 반환값.

    ``data``에는 프런트·tester에서 확인할 임의의 전처리 결과를 넣는다. 반드시
    ``objId`` 목록일 필요가 없으며, 행 목록·그룹화 결과·차트 데이터·페이지별
    원본 요약 등 시나리오에 맞는 JSON 가능 구조를 자유롭게 사용할 수 있다.
    """

    data: Any
    answer: ScenarioAnswer
    metadata: Mapping[str, Any] = field(default_factory=dict)
    result_format: str = "query.v1"


@dataclass(frozen=True)
class ScenarioRagOutputItem:
    """한 RAG detail의 최종 선택 MCP 결과와 검색 문맥."""

    match_index: int
    execution: McpExecutionResult
    scenario_code: str
    detail_scenario_code: str
    query: str
    keywords: Sequence[str]


@dataclass(frozen=True)
class ScenarioRagOutputContext:
    """한 요청의 모든 RAG detail을 통합 답변 함수에 전달하는 실행 문맥.

    공통 답변 서비스는 LLM·reranker 같은 인프라 객체와 질문 문맥만 전달한다.
    문서 결과 schema, 필드 매핑, 임계값, 답변 가능성 판단 순서와 최종 프롬프트는
    등록된 서브에이전트 Python 파일이 직접 결정한다.
    """

    items: Sequence[ScenarioRagOutputItem]
    agent_code: str
    master_query: str
    chat_history: Sequence[Mapping[str, Any]]
    # 직원정보 MCP 선조회로 보강된 안전한 사용자 문맥이다. access token은 없다.
    request_user: Mapping[str, Any]
    reranker: Any
    answerability: Any
    llm: Any
    system_prompt: str
    endpoint: str
    model: str
    max_retries: int


@dataclass(frozen=True)
class ScenarioRagOutput:
    """전용 RAG handler가 공통 SSE 계층에 넘기는 최소 결과."""

    answer: str | AsyncIterator[str]
    source_documents: list[dict[str, Any]] = field(default_factory=list)
    renderables: list[dict[str, Any]] = field(default_factory=list)
    metadata: Mapping[str, Any] = field(default_factory=dict)
