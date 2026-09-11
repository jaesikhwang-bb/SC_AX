"""검색 문서만으로 사용자 질문에 답변 가능한지 판별하는 단계."""

import json
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.observability import (
    create_llm_usage_callback,
    log_failure_diagnostic,
    logger,
    timed,
)


class AnswerabilityDecision(BaseModel):
    """LLM이 반드시 반환해야 하는 최소 구조화 출력."""

    model_config = ConfigDict(extra="forbid")

    answerable: bool


class AnswerabilityService(Protocol):
    """답변 서비스와 실제 판별 모델을 분리하는 계약."""

    async def is_answerable(
        self,
        *,
        query: str,
        documents: list[dict[str, Any]],
    ) -> bool: ...


class AssumeAnswerableService:
    """토큰이 없는 로컬 테스트에서 외부 호출 없이 다음 단계로 진행한다."""

    async def is_answerable(
        self,
        *,
        query: str,
        documents: list[dict[str, Any]],
    ) -> bool:
        logger.info(
            "======== 문서 답변 가능성 판별 생략\n"
            "이유=GenOS 토큰 없음 또는 테스트 대체 구현\n"
            "질문길이=%d\n문서개수=%d",
            len(query),
            len(documents),
        )
        logger.info(
            "FLOW RAG 답변 가능성 | 판별=생략 | 문서수=%d | 결과=가능",
            len(documents),
        )
        return True


class GenosAnswerabilityService:
    """GenOS LLM의 Pydantic 구조화 출력으로 문서 근거 존재 여부를 판별한다."""

    @timed("문서 답변 가능성 판별 서비스 초기화")
    def __init__(self, settings: Settings, system_prompt: str) -> None:
        llm = ChatOpenAI(
            base_url=settings.genos_openai_base_url,
            model=settings.genos_model,
            api_key=settings.genos_bearer_token,
            temperature=0,
            callbacks=[
                create_llm_usage_callback(
                    stage="rag.answerability",
                    model=settings.genos_model,
                )
            ],
            **settings.llm_client_options,
        )
        self._chain = llm.with_structured_output(
            AnswerabilityDecision,
            method="json_schema",
            strict=True,
        )
        self._system_prompt = system_prompt
        self._endpoint = settings.genos_openai_base_url
        self._model = settings.genos_model
        self._max_retries = settings.llm_max_retries

    async def is_answerable(
        self,
        *,
        query: str,
        documents: list[dict[str, Any]],
    ) -> bool:
        document_text = json.dumps(documents, ensure_ascii=False, default=str)
        logger.info(
            "======== 문서 답변 가능성 LLM 판별 시작\n"
            "코드위치=app/answerability.py:GenosAnswerabilityService.is_answerable\n"
            "엔드포인트=%s\n모델=%s\n질문길이=%d\n문서개수=%d\n"
            "문서JSON길이=%d\n자동재시도=%d회",
            self._endpoint,
            self._model,
            len(query),
            len(documents),
            len(document_text),
            self._max_retries,
        )
        try:
            decision = await self._chain.ainvoke(
                [
                    SystemMessage(content=self._system_prompt),
                    HumanMessage(
                        content=(
                            f"사용자 질문:\n{query}\n\n검색 문서:\n{document_text}"
                        )
                    ),
                ]
            )
        except Exception as exc:
            log_failure_diagnostic(
                stage="문서 답변 가능성 LLM 판별",
                code_location=(
                    "app/answerability.py:GenosAnswerabilityService.is_answerable"
                ),
                exc=exc,
                likely_cause=(
                    "GenOS 연결·인증·모델 오류 또는 json_schema 구조화 출력 미지원"
                ),
                corrective_action=(
                    "GenOS serving의 structured output 지원 여부와 GENOS_* "
                    "환경변수를 확인하세요."
                ),
                retry_count=self._max_retries,
                context={
                    "endpoint": self._endpoint,
                    "model": self._model,
                    "query_length": len(query),
                    "document_count": len(documents),
                },
            )
            raise

        answerable = bool(decision.answerable)
        logger.info(
            "======== 문서 답변 가능성 LLM 판별 완료\n"
            "질문길이=%d\n문서개수=%d\n답변가능=%s",
            len(query),
            len(documents),
            answerable,
        )
        logger.info(
            "FLOW RAG 답변 가능성 | 판별=LLM | 문서수=%d | 결과=%s",
            len(documents),
            "가능" if answerable else "불가",
        )
        return answerable


def create_answerability_service(
    settings: Settings,
    system_prompt: str,
) -> AnswerabilityService:
    """GenOS 토큰 유무에 맞는 판별 서비스를 생성한다."""

    if not settings.genos_bearer_token:
        return AssumeAnswerableService()
    return GenosAnswerabilityService(settings, system_prompt)
