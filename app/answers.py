"""MCP 결과와 예외 분류를 최종 사용자 답변 스트림으로 변환한다."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.graph import MasterResult
from app.observability import logger, timed
from app.streaming import split_text


@dataclass(frozen=True)
class AnswerPromptBundle:
    """최종 답변 생성 방식과 RAG 프롬프트 설정."""

    version: str
    rag_system_prompt: str
    temperature: float
    agent_response_modes: dict[str, str]
    default_response_mode: str
    exception_answers: dict[str, str]


@dataclass(frozen=True)
class PreparedAnswer:
    """SSE 전송 직전에 준비된 출처 문서와 비동기 텍스트 스트림."""

    mode: str
    source_documents: list[dict[str, Any]]
    tokens: AsyncIterator[str]


class AnswerService(Protocol):
    """API가 답변 생성 방식의 세부 구현에 의존하지 않게 하는 인터페이스."""

    def prepare(self, result: MasterResult) -> PreparedAnswer: ...


class AnswerPromptLoader:
    """버전 관리되는 최종 답변 프롬프트와 응답 모드를 로드한다."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (
            root
            or Path(__file__).resolve().parents[1]
            / "prompts"
            / "answer-generation"
        )

    @timed("최종 답변 프롬프트 로딩")
    def load(self, requested_version: str | None = None) -> AnswerPromptBundle:
        active = yaml.safe_load(
            (self._root / "active.yaml").read_text(encoding="utf-8")
        )
        version = requested_version or str(active["active_version"])
        version_root = self._root / version
        manifest = yaml.safe_load(
            (version_root / "manifest.yaml").read_text(encoding="utf-8")
        )
        prompt_path = version_root / str(manifest["rag_system_prompt"])
        modes = {
            str(code).upper(): str(mode)
            for code, mode in manifest.get("agent_response_modes", {}).items()
        }
        allowed_modes = {"fixed_data", "rag"}
        invalid_modes = set(modes.values()) - allowed_modes
        default_mode = str(manifest.get("default_response_mode", "fixed_data"))
        if default_mode not in allowed_modes or invalid_modes:
            raise ValueError(
                "지원하지 않는 최종 답변 모드가 있습니다: "
                f"{sorted(invalid_modes | {default_mode} - allowed_modes)}"
            )
        bundle = AnswerPromptBundle(
            version=version,
            rag_system_prompt=prompt_path.read_text(encoding="utf-8").strip(),
            temperature=float(manifest.get("model", {}).get("temperature", 0)),
            agent_response_modes=modes,
            default_response_mode=default_mode,
            exception_answers={
                str(code): str(answer)
                for code, answer in manifest.get("exception_answers", {}).items()
            },
        )
        logger.info(
            "======== 최종 답변 프롬프트 로딩 완료 | 버전=%s | 모드=%s",
            bundle.version,
            bundle.agent_response_modes,
        )
        return bundle


class DefaultAnswerService:
    """고정 데이터 답변과 문서 기반 RAG 답변을 하나의 인터페이스로 제공한다."""

    def __init__(
        self,
        settings: Settings,
        prompt: AnswerPromptBundle,
        llm: Any | None = None,
    ) -> None:
        self._prompt = prompt
        # 테스트에서는 토큰이 없으므로 네트워크를 호출하지 않는 RAG 대체 답변을
        # 사용한다. 운영에서는 마스터와 같은 GenOS OpenAI 호환 엔드포인트다.
        self._llm = llm
        if self._llm is None and settings.genos_bearer_token:
            self._llm = ChatOpenAI(
                base_url=settings.genos_openai_base_url,
                model=settings.genos_model,
                api_key=settings.genos_bearer_token,
                temperature=prompt.temperature,
            )

    def prepare(self, result: MasterResult) -> PreparedAnswer:
        """그래프 결과에 맞는 답변 모드와 스트림을 선택한다."""

        if result.status == "EXCEPTION":
            code = result.classification.classification_type.value
            text = self._prompt.exception_answers.get(
                code,
                "요청을 처리할 수 없습니다. 질문 내용을 확인해 주세요.",
            )
            logger.info("======== 고정 예외 답변 선택 | 예외유형=%s", code)
            return PreparedAnswer("exception", [], _stream_fixed(text))

        agent_code = result.classification.agent_code or ""
        mode = self._prompt.agent_response_modes.get(
            agent_code,
            self._prompt.default_response_mode,
        )
        documents = _build_source_documents(result) if mode == "rag" else []
        if mode == "rag":
            logger.info(
                "======== RAG 답변 선택 | 에이전트=%s | 문서개수=%d",
                agent_code,
                len(documents),
            )
            return PreparedAnswer(
                mode,
                documents,
                self._stream_rag(result, documents),
            )

        text = _build_fixed_data_answer(result)
        logger.info("======== 고정 데이터 답변 선택 | 에이전트=%s", agent_code)
        return PreparedAnswer(mode, [], _stream_fixed(text))

    async def _stream_rag(
        self,
        result: MasterResult,
        documents: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        """MCP dict를 문서로 넣어 GenOS 답변 토큰을 스트리밍한다."""

        document_text = json.dumps(documents, ensure_ascii=False, default=str)
        human_prompt = (
            f"사용자 질문:\n{result.classification.refined_query}\n\n"
            f"참고 문서:\n{document_text}"
        )
        if self._llm is None:
            fallback = (
                "테스트 RAG 답변입니다. 조회된 참고 문서를 기반으로 자격기준을 "
                f"안내합니다. 참고 문서: {document_text}"
            )
            for chunk in split_text(fallback):
                yield chunk
            return

        emitted = False
        try:
            async for chunk in self._llm.astream(
                [
                    SystemMessage(content=self._prompt.rag_system_prompt),
                    HumanMessage(content=human_prompt),
                ]
            ):
                text = _message_content_text(getattr(chunk, "content", ""))
                if text:
                    emitted = True
                    yield text
        except Exception as exc:
            logger.exception("======== RAG 스트리밍 실패 | 오류=%s", exc)
            if emitted:
                raise
            # 일부 OpenAI 호환 서버가 stream 옵션을 지원하지 않는 경우 한 번의
            # 일반 호출로 재시도한 뒤 SSE token 조각으로 나누어 전달한다.
            response = await self._llm.ainvoke(
                [
                    SystemMessage(content=self._prompt.rag_system_prompt),
                    HumanMessage(content=human_prompt),
                ]
            )
            text = _message_content_text(getattr(response, "content", ""))
            for chunk in split_text(text):
                yield chunk


async def _stream_fixed(text: str) -> AsyncIterator[str]:
    """고정 문자열을 작은 token 이벤트 단위로 반환한다."""

    for chunk in split_text(text):
        yield chunk


def _build_fixed_data_answer(result: MasterResult) -> str:
    """PERFORMANCE_FEE 등 기간계 조회 결과를 예측 가능한 형식으로 만든다."""

    if not result.mcp_results:
        return "조회 결과가 없습니다. 잠시 후 다시 시도해 주세요."
    rows = []
    for index, mcp in enumerate(result.mcp_results, start=1):
        if mcp.succeeded:
            data = json.dumps(mcp.result or {}, ensure_ascii=False, default=str)
            rows.append(f"{index}. {mcp.tool_name} 조회 결과: {data}")
        else:
            rows.append(f"{index}. {mcp.tool_name} 조회 실패: {mcp.error}")
    return "테스트 고정답변입니다.\n" + "\n".join(rows)


def _build_source_documents(result: MasterResult) -> list[dict[str, Any]]:
    """MCP structuredContent를 프론트와 RAG가 공유하는 문서 배열로 변환한다."""

    documents = []
    for index, mcp in enumerate(result.mcp_results or [], start=1):
        if not mcp.succeeded or mcp.result is None:
            continue
        documents.append(
            {
                "document_id": f"{mcp.request_id}:document:{index}",
                "title": f"{mcp.tool_name} 조회 문서",
                "source": mcp.tool_name,
                "content": mcp.result,
                "metadata": {
                    "mcp_request_id": mcp.request_id,
                    "arguments": mcp.arguments,
                },
            }
        )
    return documents


def _message_content_text(content: Any) -> str:
    """LangChain 모델별 문자열·콘텐츠 블록 응답을 텍스트로 통일한다."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content) if content is not None else ""


@timed("최종 답변 서비스 생성")
def create_answer_service(settings: Settings) -> AnswerService:
    """운영용 고정 데이터·RAG 통합 답변 서비스를 생성한다."""

    return DefaultAnswerService(settings, AnswerPromptLoader().load())
