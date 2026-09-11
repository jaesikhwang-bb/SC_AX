"""LangChain을 통해 GenOS LLM의 1차 의도분류를 호출하는 모듈."""

from typing import Any, Literal, Protocol

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.domain import (
    IntentClassification,
    QueryRefinement,
    QueryRefinementStatus,
    create_structured_output_model,
)
from app.observability import (
    async_timed_block,
    create_llm_usage_callback,
    log_failure_diagnostic,
    logger,
    timed,
)
from app.prompt_loader import PromptBundle
from app.query_period import active_month, has_period, single_month


class IntentClassifier(Protocol):
    """LangGraph가 의존하는 의도분류기 인터페이스."""

    async def classify(
        self,
        message: str,
        history: list[dict[str, Any]],
        context_mode: Literal["CURRENT_ONLY", "HISTORY_ALLOWED"] = "CURRENT_ONLY",
    ) -> IntentClassification: ...


class GenOSIntentClassifier:
    """LangChain ChatOpenAI로 GenOS의 OpenAI 호환 엔드포인트를 호출한다."""

    @timed("GenOS 분류기 초기화")
    def __init__(self, settings: Settings, prompt: PromptBundle) -> None:
        # 토큰이 없을 때 키워드 분류기로 대체하지 않는다. 운영 요청은 반드시
        # 설정된 LLM을 호출해야 하므로 시작 단계에서 명확하게 실패시킨다.
        if not settings.genos_bearer_token:
            raise ValueError("GENOS_BEARER_TOKEN 환경변수가 필요합니다.")

        # 질문 보정과 에이전트 분류를 서로 다른 구조화 출력 호출로 분리한다.
        # 분류 결과가 보정 문장을 역으로 바꾸는 문제를 막기 위해 보정 체인의
        # 결과만 최종 refined_query로 사용한다.
        refinement_prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=prompt.refinement_prompt),
                (
                    "human",
                    "보정 모드:\n{context_mode}\n\n"
                    "이전 대화:\n{history}\n\n"
                    "같은 대화의 최근 명시 조회 월 후보:\n{active_period}\n\n"
                    "현재 사용자 질문:\n{message}",
                ),
            ]
        )

        refinement_llm = ChatOpenAI(
            base_url=settings.genos_openai_base_url,
            model=settings.genos_model,
            api_key=settings.genos_bearer_token,
            temperature=0,
            callbacks=[
                create_llm_usage_callback(
                    stage="master.query_refinement",
                    model=settings.genos_model,
                )
            ],
            **settings.llm_client_options,
        )
        self._refinement_chain = refinement_prompt | refinement_llm.with_structured_output(
            QueryRefinement,
            method="json_schema",
            strict=True,
        )

        # manifest.yaml의 에이전트 코드로 Enum과 JSON Schema를 동적으로 만든다.
        # 새 에이전트를 추가해도 이 Python 파일을 변경할 필요가 없다.
        output_model = create_structured_output_model(prompt.agent_codes)

        # 결합된 Markdown 프롬프트는 SystemMessage에 일반 문자열로 전달한다.
        # 이렇게 하면 프롬프트 내부 JSON 중괄호가 템플릿 변수로 해석되지 않는다.
        classification_prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=prompt.system_prompt),
                (
                    "human",
                    "질문 보정 상태: {refinement_status}\n"
                    "대화 이력 존재 여부: {history_available}\n\n"
                    "질문의 의미를 다시 쓰지 말고 최종 분류를 판단하세요. "
                    "보정 상태가 COMPLETE가 아니더라도 현재 질문 자체에 업무 대상과 "
                    "요청 행위가 드러나면 담당 에이전트를 선택하세요. 대화 이력이 "
                    "없다는 이유만으로 EMPTY_QUERY를 선택하지 마세요. 구조화 출력의 "
                    "refined_query에는 전달받은 문장을 그대로 복사하세요.\n\n"
                    "보정된 사용자 질문:\n{message}",
                ),
            ]
        )

        # base_url은 GenOS OpenAI 호환 API의 /v1 경로까지 포함한다.
        # 제한시간·재시도·최대 토큰은 응답이 끝나지 않는 상황을 막기 위해
        # 환경설정으로 명시한다. 상세 값은 Settings.llm_client_options 참고.
        classification_llm = ChatOpenAI(
            base_url=settings.genos_openai_base_url,
            model=settings.genos_model,
            api_key=settings.genos_bearer_token,
            temperature=prompt.temperature,
            callbacks=[
                create_llm_usage_callback(
                    stage="master.intent_classification",
                    model=settings.genos_model,
                )
            ],
            **settings.llm_client_options,
        )

        # strict JSON Schema를 적용해 LLM 응답을 Pydantic 모델로 직접 받는다.
        # 별도의 수동 JSON 파싱이나 하드코딩된 키워드 분류는 사용하지 않는다.
        self._classification_chain = classification_prompt | classification_llm.with_structured_output(
            output_model,
            method="json_schema",
            strict=True,
        )
        # 실제 호출 직전에 전체 전달 내용을 로그로 확인할 수 있도록 결합된
        # 시스템 프롬프트를 보관한다. 토큰이나 API 키는 포함되지 않는다.
        self._system_prompt = prompt.system_prompt
        self._model = settings.genos_model
        self._endpoint = settings.genos_openai_base_url
        self._max_retries = settings.llm_max_retries
        logger.info(
            "======== LLM 준비 완료 | 모델=%s | 프롬프트버전=%s | "
            "에이전트개수=%d | 자동재시도=%d회 | "
            "코드위치=app/classifier.py:GenOSIntentClassifier.classify",
            settings.genos_model,
            prompt.version,
            len(prompt.agent_codes),
            settings.llm_max_retries,
        )

    @timed("1차 의도분류")
    async def classify(
        self,
        message: str,
        history: list[dict[str, Any]],
        context_mode: Literal["CURRENT_ONLY", "HISTORY_ALLOWED"] = "CURRENT_ONLY",
    ) -> IntentClassification:
        """현재 질문 또는 같은 에이전트 범위 이력을 사용하는 분류를 실행한다.

        프론트 선택 agent_code는 의도분류 LLM에 전달하지 않는다. 그 값은 그래프의
        ``_verify_selection``에서 실제 분류 코드와 비교하는 용도로만 사용한다.
        """

        if context_mode not in {"CURRENT_ONLY", "HISTORY_ALLOWED"}:
            raise ValueError(f"지원하지 않는 마스터 분류 모드입니다: {context_mode}")
        effective_history = history if context_mode == "HISTORY_ALLOWED" else []
        period = active_month(effective_history)
        stage_name = (
            "마스터문맥보정의도분류"
            if context_mode == "HISTORY_ALLOWED"
            else "마스터독립의도분류"
        )

        # Redis가 반환한 role/content 딕셔너리를 대화 순서대로 문자열화한다.
        history_text = "\n".join(
            _history_item_for_prompt(item) for item in effective_history
        )
        # 1단계: 의미 보존 질문 보정. 최종 업무 분류와 분리한다.
        try:
            async with async_timed_block("질문 의미 보존 보정 LLM"):
                refinement = await self._refinement_chain.ainvoke(
                    {
                        "history": history_text or "(이전 대화 없음)",
                        "message": message,
                        "context_mode": context_mode,
                        "active_period": period or "(단일 조회 월 없음)",
                    }
                )
        except Exception as exc:
            log_failure_diagnostic(
                stage=f"{stage_name} 질문 보정 LLM 호출",
                code_location=("app/classifier.py:GenOSIntentClassifier.classify"),
                exc=exc,
                likely_cause=(
                    "GenOS LLM 연결·인증·모델 설정 오류, 응답 시간 초과, 또는 "
                    "LLM 응답이 마스터 Structured Output JSON Schema와 불일치"
                ),
                corrective_action=(
                    ".env의 GENOS_URL/GENOS_SERVING_ID/GENOS_MODEL/"
                    "GENOS_BEARER_TOKEN을 확인하고 prompts/intent-classification의 "
                    "활성 프롬프트와 app/domain.py 출력 스키마를 확인하세요."
                ),
                retry_count=self._max_retries,
                context={
                    "endpoint": self._endpoint,
                    "model": self._model,
                    "history_count": len(effective_history),
                    "context_mode": context_mode,
                    "message_length": len(message),
                },
            )
            raise

        # 미확정 보정 결과의 추측 문장은 버린다. 이력이 없는데 사용했다고
        # 주장한 결과도 원문으로 되돌려 분류한다. 추가 LLM 재시도는 하지 않는다.
        if refinement.status != QueryRefinementStatus.COMPLETE or (
            refinement.used_history and not effective_history
        ):
            refinement = QueryRefinement(
                refined_query=message,
                status=QueryRefinementStatus.UNRESOLVED,
                used_history=False,
                inherit_active_period=False,
            )

        if (
            refinement.status == QueryRefinementStatus.COMPLETE
            and refinement.inherit_active_period
            and period
            and not has_period(message)
        ):
            # 기간 적용 여부는 의미 보정기가 판단하고, 값은 사용자 이력에서만
            # 가져온다. LLM이 기간 삽입을 누락하거나 당월을 발명해도 그대로 쓰지 않는다.
            refined = refinement.refined_query
            if single_month(refined) != period:
                refined = f"{period} {message if has_period(refined) else refined}"
            refinement = refinement.model_copy(update={
                "refined_query": refined,
                "used_history": True,
            })
            logger.info("FLOW 활성 조회 월 유지 | 기간=%s | 근거=동일범위사용자이력", period)

        logger.info(
            "FLOW 질문 보정 완료 | 원본질문(가드레일처리후)=%s | "
            "보정질문(가드레일처리후)=%s | "
            "문맥상태=%s | 이력사용=%s",
            message,
            refinement.refined_query,
            refinement.status.value,
            refinement.used_history,
        )

        # 보정 1회 → 최종 분류 1회. 보정 상태는 업무 지원 여부가 아니다.
        # 불완전 상태라도 원문 자체로 식별 가능한 업무/명확한 예외는 라우터가
        # 그대로 판정한다. 라우터에는 이력을 주지 않아 조건을 재발명하지 않는다.
        try:
            async with async_timed_block("마스터 에이전트 의도분류 LLM"):
                structured = await self._classification_chain.ainvoke(
                    {
                        "message": refinement.refined_query,
                        "refinement_status": refinement.status.value,
                        "history_available": bool(effective_history),
                    }
                )
        except Exception as exc:
            log_failure_diagnostic(
                stage=f"{stage_name} 에이전트 분류 LLM 호출",
                code_location="app/classifier.py:GenOSIntentClassifier.classify",
                exc=exc,
                likely_cause=(
                    "GenOS LLM 연결·인증·모델 설정 오류 또는 마스터 분류 "
                    "Structured Output 불일치"
                ),
                corrective_action=(
                    "GenOS 설정과 prompts/intent-classification의 에이전트 정의를 "
                    "확인하세요."
                ),
                retry_count=self._max_retries,
                context={
                    "endpoint": self._endpoint,
                    "model": self._model,
                    "refined_query_length": len(refinement.refined_query),
                },
            )
            raise

        # 동적으로 생성된 Enum은 API와 그래프에서 쓰기 쉬운 문자열로 변환한다.
        result = IntentClassification(
            # 분류 LLM이 반환한 문장은 의도적으로 사용하지 않는다. 질문 의미는
            # 오직 앞선 보정 단계의 결과만이 결정한다.
            refined_query=refinement.refined_query,
            classification_type=structured.classification_type,
            agent_code=(
                structured.agent_code.value
                if structured.agent_code is not None
                else None
            ),
        )
        logger.info(
            "FLOW 마스터 분류 완료 | 단계=%s | 분류유형=%s | "
            "에이전트코드=%s | 보정질문(가드레일처리후)=%s",
            stage_name,
            result.classification_type.value,
            result.agent_code,
            result.refined_query,
        )
        return result


@timed("의도분류기 생성")
def create_classifier(
    settings: Settings,
    prompt: PromptBundle,
) -> IntentClassifier:
    """운영 환경에서 사용할 실제 GenOS 의도분류기를 생성한다."""

    return GenOSIntentClassifier(settings, prompt)


def _history_item_for_prompt(item: dict[str, Any]) -> str:
    """실제 대화 본문만 전달한다. 추천질문 metadata는 보정에 사용하지 않는다."""

    role = str(item.get("role", "unknown"))
    content = str(item.get("content", ""))
    return f"{role}: {content}"
