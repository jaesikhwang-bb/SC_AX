"""Databricks 검색 문서를 GenOS reranker로 재정렬하는 선택적 단계."""

import json
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.observability import (
    async_timed_block,
    log_failure_diagnostic,
    logger,
    timed,
)


class RerankingService(Protocol):
    """답변 서비스가 구체적인 reranker 전송 방식과 무관하게 쓰는 계약."""

    enabled: bool

    async def rerank(
        self,
        *,
        query: str,
        documents: list[dict[str, Any]],
        top_n: int | None = None,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


class DisabledRerankingService:
    """reranking을 끈 경우 검색 순서를 그대로 유지한다."""

    enabled = False

    async def rerank(
        self,
        *,
        query: str,
        documents: list[dict[str, Any]],
        top_n: int | None = None,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        logger.info(
            "======== Reranking 생략\n"
            "활성화=false\n검색문길이=%d\n문서개수=%d\n"
            "시나리오요청top_n=%s\n시나리오요청임계점수=%.3f",
            len(query),
            len(documents),
            top_n,
            score_threshold,
        )
        logger.info(
            "FLOW Reranking | 활성=false | 입력문서수=%d | 출력문서수=%d",
            len(documents),
            len(documents),
        )
        return documents

    async def aclose(self) -> None:
        return None


class GenosRerankingService:
    """GenOS serving의 rerank API로 문서 관련도 순서를 다시 계산한다.

    요청은 일반 LLM과 같은 GenOS serving URL·Bearer 인증 규칙을 사용한다.
    생성형 chat completion이 아니라 reranker 표준 계약에 맞춰 ``/v1/rerank``에
    query와 documents를 한 번에 보내고 JSON 결과 전체를 받은 뒤 정렬한다.
    실제 GenOS 배포 경로가 다르면 ``RERANKING_ENDPOINT_PATH``만 변경한다.
    """

    enabled = True

    @timed("GenOS Reranking 서비스 초기화")
    def __init__(self, settings: Settings) -> None:
        if not settings.reranking_bearer_token:
            raise ValueError(
                "RERANKING_ENABLED=true인 경우 RERANKING_BEARER_TOKEN이 필요합니다."
            )
        self._url = settings.genos_reranking_url
        self._model = settings.reranking_model
        self._top_n = settings.reranking_top_n
        self._timeout_seconds = settings.reranking_timeout_seconds
        self._client = httpx.AsyncClient(
            timeout=settings.reranking_timeout_seconds,
            transport=httpx.AsyncHTTPTransport(retries=0),
            headers={
                "Authorization": (f"Bearer {settings.reranking_bearer_token}"),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        logger.info(
            "======== GenOS Reranking 서비스 준비 | 활성화=true | "
            "serving_id=%d | 모델=%s | URL=%s | top_n=%d | "
            "타임아웃=%.3f초 | 자동재시도=0회 | 토큰로그=생략",
            settings.reranking_serving_id,
            self._model,
            self._url,
            self._top_n,
            self._timeout_seconds,
        )

    async def rerank(
        self,
        *,
        query: str,
        documents: list[dict[str, Any]],
        top_n: int | None = None,
        score_threshold: float = 0.0,
    ) -> list[dict[str, Any]]:
        """검색 문서를 재정렬하고 시나리오별 점수·개수 정책을 적용한다."""

        if not documents:
            logger.info("======== Reranking 생략 | 이유=검색문서없음")
            logger.info("FLOW Reranking | 활성=true | 입력문서수=0 | 출력문서수=0")
            return []

        effective_top_n = top_n if top_n is not None else self._top_n
        if effective_top_n < 1:
            raise ValueError("Reranking top_n은 1 이상이어야 합니다.")
        if not 0 <= score_threshold <= 1:
            raise ValueError("Reranking score_threshold는 0 이상 1 이하이어야 합니다.")

        document_texts = [_document_text(document) for document in documents]
        payload = {
            "model": self._model,
            "query": query,
            "documents": document_texts,
            "top_n": min(effective_top_n, len(documents)),
            "return_documents": False,
        }
        logger.info(
            "======== GenOS Reranking 호출 시작\n"
            "URL=%s\n모델=%s\n검색문길이=%d\n입력문서개수=%d\n"
            "top_n=%d\n임계점수=%.3f\n문서본문로그=생략\n자동재시도=0회",
            self._url,
            self._model,
            len(query),
            len(documents),
            payload["top_n"],
            score_threshold,
        )
        try:
            async with async_timed_block("GenOS Reranking 응답 대기"):
                response = await self._client.post(self._url, json=payload)
                logger.info(
                    "======== GenOS Reranking HTTP 응답 | 상태코드=%d | "
                    "Content-Type=%s | 응답길이=%d | 원본응답로그=생략",
                    response.status_code,
                    response.headers.get("content-type"),
                    len(response.text),
                )
                response.raise_for_status()
            rankings = _parse_reranking_response(response.json())
            reranked = _apply_rankings(
                documents=documents,
                rankings=rankings,
                model=self._model,
                top_n=int(payload["top_n"]),
                score_threshold=score_threshold,
            )
        except Exception as exc:
            log_failure_diagnostic(
                stage="GenOS 문서 Reranking",
                code_location="app/reranking.py:GenosRerankingService.rerank",
                exc=exc,
                likely_cause=(
                    "GenOS reranker 연결·인증 오류, /v1/rerank 요청 계약 불일치, "
                    "모델명 오류 또는 결과 index/score 형식 불일치"
                ),
                corrective_action=(
                    ".env의 RERANKING_* 설정과 GenOS serving 226의 실제 API "
                    "경로·요청/응답 스키마를 확인하세요. 끄려면 "
                    "RERANKING_ENABLED=false로 설정하세요."
                ),
                retry_count=0,
                context={
                    "url": self._url,
                    "model": self._model,
                    "query_length": len(query),
                    "document_count": len(documents),
                    "top_n": payload["top_n"],
                    "score_threshold": score_threshold,
                },
            )
            raise

        logger.info(
            "======== GenOS Reranking 완료\n"
            "모델=%s\n임계점수=%.3f\n입력문서개수=%d\n출력문서개수=%d",
            self._model,
            score_threshold,
            len(documents),
            len(reranked),
        )
        logger.info(
            "FLOW Reranking | 활성=true | 입력문서수=%d | 출력문서수=%d | "
            "임계점수=%.3f",
            len(documents),
            len(reranked),
            score_threshold,
        )
        return reranked

    async def aclose(self) -> None:
        await self._client.aclose()
        logger.info("======== GenOS Reranking HTTP 클라이언트 종료 완료")


def _document_text(document: dict[str, Any]) -> str:
    """reranker 입력 한 건을 제목과 본문 문자열로 만든다."""

    title = str(document.get("title") or "").strip()
    content = document.get("content")
    if isinstance(content, str):
        content_text = content.strip()
    else:
        content_text = json.dumps(content, ensure_ascii=False, default=str)
    return f"{title}\n{content_text}".strip()


def _parse_reranking_response(value: Any) -> list[dict[str, Any]]:
    """대표적인 rerank 응답의 index와 score를 공통 구조로 변환한다."""

    if not isinstance(value, dict):
        raise ValueError("Reranking 응답은 object 형식이어야 합니다.")

    rows = value.get("results")
    if not isinstance(rows, list):
        rows = value.get("data")

    result_wrapper = value.get("result")
    if not isinstance(rows, list) and isinstance(result_wrapper, dict):
        return _parse_reranking_response(result_wrapper)

    # 일부 OpenAI 호환 게이트웨이는 결과 JSON을 message.content로 감싼다.
    if not isinstance(rows, list):
        choices = value.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            message = first.get("message") if isinstance(first, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                parsed_content = json.loads(content)
                return _parse_reranking_response(parsed_content)

    if not isinstance(rows, list) or not rows:
        raise ValueError(
            "Reranking 응답에 비어 있지 않은 results 또는 data 배열이 필요합니다."
        )

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Reranking 결과 항목은 object 형식이어야 합니다.")
        index = row.get("index")
        score = row.get("relevance_score", row.get("score"))
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("Reranking 결과 index는 integer여야 합니다.")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            raise ValueError("Reranking 결과 score는 number여야 합니다.")
        normalized.append({"index": index, "score": float(score)})
    return normalized


def _apply_rankings(
    *,
    documents: list[dict[str, Any]],
    rankings: Sequence[dict[str, Any]],
    model: str,
    top_n: int,
    score_threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """중복·범위 오류를 검증하고 score 순서로 문서 metadata를 갱신한다."""

    ordered = sorted(rankings, key=lambda item: item["score"], reverse=True)
    seen: set[int] = set()
    output: list[dict[str, Any]] = []
    filtered = [item for item in ordered if float(item["score"]) >= score_threshold]
    for rank, item in enumerate(filtered[:top_n], start=1):
        index = int(item["index"])
        if index in seen:
            raise ValueError(f"Reranking 결과 index가 중복되었습니다: {index}")
        if index < 0 or index >= len(documents):
            raise ValueError(
                "Reranking 결과 index가 입력 문서 범위를 벗어났습니다: "
                f"index={index}, document_count={len(documents)}"
            )
        seen.add(index)
        document = documents[index]
        metadata = dict(document.get("metadata") or {})
        metadata["reranking"] = {
            "enabled": True,
            "model": model,
            "original_rank": index + 1,
            "rank": rank,
            "score": float(item["score"]),
        }
        output.append({**document, "metadata": metadata})
    logger.info(
        "======== Reranking 임계값 적용 완료\n"
        "임계점수=%.3f\n모델결과개수=%d\n통과문서개수=%d\n"
        "제외문서개수=%d",
        score_threshold,
        len(ordered),
        len(output),
        len(ordered) - len(filtered),
    )
    return output


@timed("Reranking 서비스 생성")
def create_reranking_service(settings: Settings) -> RerankingService:
    """환경설정에 따라 실제 GenOS 또는 비활성 구현을 선택한다."""

    if not settings.reranking_enabled:
        logger.info("======== Reranking 서비스 비활성화 | 설정=false")
        return DisabledRerankingService()
    return GenosRerankingService(settings)
