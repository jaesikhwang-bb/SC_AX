"""단계별 로그와 실행시간 측정을 위한 공통 도구."""

import inspect
import logging
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import AsyncIterator, Callable, Iterator, ParamSpec, TypeVar


P = ParamSpec("P")
R = TypeVar("R")

# 애플리케이션 전체에서 같은 이름의 로거를 사용한다.
logger = logging.getLogger("master_agent")


def configure_logging(level: str = "INFO") -> None:
    """애플리케이션 시작 시 UTF-8 출력과 공통 로그 형식을 설정한다."""

    # Windows PowerShell의 기본 코드페이지와 Python 출력 인코딩이 다르면 한글
    # 로그가 깨질 수 있다. 지원되는 스트림은 UTF-8로 명시해 그대로 출력한다.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def timed(
    stage: str,
    *,
    expected_exceptions: tuple[type[BaseException], ...] = (),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """동기·비동기 함수의 시작, 완료, 중단, 실패와 소요시간을 기록한다.

    예외는 로그를 남긴 뒤 그대로 다시 발생시킨다. 따라서 이 데코레이터를
    적용해도 기존 비즈니스 동작이나 예외 처리 흐름은 바뀌지 않는다.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs):
                started = time.perf_counter()
                logger.info(
                    "======== 단계 시작 | %s | 함수=%s",
                    stage,
                    func.__qualname__,
                )
                try:
                    result = await func(*args, **kwargs)
                except expected_exceptions:
                    logger.info(
                        "======== 단계 중단 | %s | 함수=%s | 소요시간=%.3f초",
                        stage,
                        func.__qualname__,
                        _elapsed_seconds(started),
                    )
                    raise
                except Exception:
                    logger.exception(
                        "======== 단계 실패 | %s | 함수=%s | 소요시간=%.3f초",
                        stage,
                        func.__qualname__,
                        _elapsed_seconds(started),
                    )
                    raise
                logger.info(
                    "======== 단계 완료 | %s | 함수=%s | 소요시간=%.3f초",
                    stage,
                    func.__qualname__,
                    _elapsed_seconds(started),
                )
                return result

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs):
            started = time.perf_counter()
            logger.info(
                "======== 단계 시작 | %s | 함수=%s",
                stage,
                func.__qualname__,
            )
            try:
                result = func(*args, **kwargs)
            except expected_exceptions:
                logger.info(
                    "======== 단계 중단 | %s | 함수=%s | 소요시간=%.3f초",
                    stage,
                    func.__qualname__,
                    _elapsed_seconds(started),
                )
                raise
            except Exception:
                logger.exception(
                    "======== 단계 실패 | %s | 함수=%s | 소요시간=%.3f초",
                    stage,
                    func.__qualname__,
                    _elapsed_seconds(started),
                )
                raise
            logger.info(
                "======== 단계 완료 | %s | 함수=%s | 소요시간=%.3f초",
                stage,
                func.__qualname__,
                _elapsed_seconds(started),
            )
            return result

        return sync_wrapper

    return decorator


@contextmanager
def timed_block(stage: str) -> Iterator[None]:
    """함수 내부의 특정 동기 코드 구간만 별도로 측정한다."""

    started = time.perf_counter()
    logger.info("======== 구간 시작 | %s", stage)
    try:
        yield
    except Exception:
        logger.exception(
            "======== 구간 실패 | %s | 소요시간=%.3f초",
            stage,
            _elapsed_seconds(started),
        )
        raise
    logger.info(
        "======== 구간 완료 | %s | 소요시간=%.3f초",
        stage,
        _elapsed_seconds(started),
    )


@asynccontextmanager
async def async_timed_block(stage: str) -> AsyncIterator[None]:
    """LLM·MCP처럼 비동기로 실행되는 특정 코드 구간을 측정한다."""

    started = time.perf_counter()
    logger.info("======== 구간 시작 | %s", stage)
    try:
        yield
    except Exception:
        logger.exception(
            "======== 구간 실패 | %s | 소요시간=%.3f초",
            stage,
            _elapsed_seconds(started),
        )
        raise
    logger.info(
        "======== 구간 완료 | %s | 소요시간=%.3f초",
        stage,
        _elapsed_seconds(started),
    )


def _elapsed_seconds(started: float) -> float:
    """perf_counter 시작값을 기준으로 경과시간을 초 단위로 반환한다."""

    return time.perf_counter() - started
