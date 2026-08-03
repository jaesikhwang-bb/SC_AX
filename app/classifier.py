"""LangChain을 통해 GenOS LLM의 1차 의도분류를 호출하는 모듈."""

from typing import Protocol

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.domain import IntentClassification, create_structured_output_model
from app.observability import async_timed_block, logger, timed
from app.prompt_loader import PromptBundle


class IntentClassifier(Protocol):
    """LangGraph가 의존하는 의도분류기 인터페이스."""

    async def classify(
        self,
        message: str,
        history: list[dict[str, str]],
        frontend_agent_code: str | None = None,
    ) -> IntentClassification: ...


class GenOSIntentClassifier:
    """LangChain ChatOpenAI로 GenOS의 OpenAI 호환 엔드포인트를 호출한다."""

    @timed("GenOS 분류기 초기화")
    def __init__(self, settings: Settings, prompt: PromptBundle) -> None:
        # 토큰이 없을 때 키워드 분류기로 대체하지 않는다. 운영 요청은 반드시
        # 설정된 LLM을 호출해야 하므로 시작 단계에서 명확하게 실패시킨다.
        if not settings.genos_bearer_token:
            raise ValueError("GENOS_BEARER_TOKEN 환경변수가 필요합니다.")

        # manifest.yaml의 에이전트 코드로 Enum과 JSON Schema를 동적으로 만든다.
        # 새 에이전트를 추가해도 이 Python 파일을 변경할 필요가 없다.
        output_model = create_structured_output_model(prompt.agent_codes)

        # 결합된 Markdown 프롬프트는 SystemMessage에 일반 문자열로 전달한다.
        # 이렇게 하면 프롬프트 내부 JSON 중괄호가 템플릿 변수로 해석되지 않는다.
        chat_prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=prompt.system_prompt),
                (
                    "human",
                    "프론트에서 선택한 에이전트:\n{frontend_agent_code}\n\n"
                    "이전 대화:\n{history}\n\n현재 사용자 질문:\n{message}",
                ),
            ]
        )

        # base_url은 GenOS OpenAI 호환 API의 /v1 경로까지 포함한다.
        llm = ChatOpenAI(
            base_url=settings.genos_openai_base_url,
            model=settings.genos_model,
            api_key=settings.genos_bearer_token,
            temperature=prompt.temperature,
        )

        # strict JSON Schema를 적용해 LLM 응답을 Pydantic 모델로 직접 받는다.
        # 별도의 수동 JSON 파싱이나 하드코딩된 키워드 분류는 사용하지 않는다.
        self._chain = chat_prompt | llm.with_structured_output(
            output_model,
            method="json_schema",
            strict=True,
        )
        # 실제 호출 직전에 전체 전달 내용을 로그로 확인할 수 있도록 결합된
        # 시스템 프롬프트를 보관한다. 토큰이나 API 키는 포함되지 않는다.
        self._system_prompt = prompt.system_prompt
        logger.info(
            "======== LLM 준비 완료 | 모델=%s | 프롬프트버전=%s | "
            "에이전트개수=%d",
            settings.genos_model,
            prompt.version,
            len(prompt.agent_codes),
        )

    @timed("1차 의도분류")
    async def classify(
        self,
        message: str,
        history: list[dict[str, str]],
        frontend_agent_code: str | None = None,
    ) -> IntentClassification:
        """선택 에이전트·동일 범위 이력·현재 질문을 LLM에 전달한다."""

        # Redis가 반환한 role/content 딕셔너리를 대화 순서대로 문자열화한다.
        history_text = "\n".join(
            f"{item['role']}: {item['content']}" for item in history
        )
        logger.info(
            "======== LLM 전달 준비 | 대화이력개수=%d | 프론트에이전트=%s",
            len(history),
            frontend_agent_code or "선택하지 않음",
        )

        # 네트워크 및 모델 응답 시간만 별도로 확인할 수 있도록 내부 구간을 잰다.
        async with async_timed_block("LLM 전달 및 응답 대기"):
            logger.info(
                "======== LLM 전달 | 대화이력개수=%d | 질문길이=%d",
                len(history),
                len(message),
            )
            # 전체 프롬프트와 대화 내용을 운영 로그에 출력하지 않도록 주석
            # 처리한다. 문제 분석을 위해 다시 확인해야 할 때만 아래 블록의
            # 주석을 해제한다. 모델명, 이력 개수, 질문 길이 로그는 유지된다.
            # logger.info(
            #     "\n"
            #     "======== LLM 전체 프롬프트 시작 ========\n"
            #     "[시스템 프롬프트]\n%s\n\n"
            #     "[이전 대화]\n%s\n\n"
            #     "[현재 사용자 질문]\n%s\n"
            #     "======== LLM 전체 프롬프트 끝 ========",
            #     self._system_prompt,
            #     history_text or "(이전 대화 없음)",
            #     message,
            # )
            structured = await self._chain.ainvoke(
                {
                    "history": history_text or "(이전 대화 없음)",
                    "message": message,
                    "frontend_agent_code": (
                        frontend_agent_code or "(선택하지 않음)"
                    ),
                }
            )

        # 동적으로 생성된 Enum은 API와 그래프에서 쓰기 쉬운 문자열로 변환한다.
        result = IntentClassification(
            refined_query=structured.refined_query,
            classification_type=structured.classification_type,
            agent_code=(
                structured.agent_code.value
                if structured.agent_code is not None
                else None
            ),
        )
        logger.info(
            "======== 1차 의도분류 결과 | 분류유형=%s | 에이전트코드=%s",
            result.classification_type.value,
            result.agent_code,
        )
        return result


@timed("의도분류기 생성")
def create_classifier(
    settings: Settings,
    prompt: PromptBundle,
) -> IntentClassifier:
    """운영 환경에서 사용할 실제 GenOS 의도분류기를 생성한다."""

    return GenOSIntentClassifier(settings, prompt)
