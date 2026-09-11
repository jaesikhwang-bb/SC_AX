"""평가 실행 전용 로깅 설정.

평가는 행마다 그래프 전체를 실행하므로 애플리케이션의 단계 로그를 그대로
켜면 행당 수십 줄이 쌓여 진행 상황이 묻힌다. 그래서 평가 진행 로그를 별도
로거로 분리하고, 파이프라인 단계 로그와 외부 라이브러리 로그의 수준을 따로
통제한다.

이 모듈은 ``app.observability``에 의존하지 않는다. 평가 코드를 다른 시점의
애플리케이션 트리에 그대로 얹어도 동작해야 하므로, 애플리케이션 로거는
임포트하지 않고 이름으로만 참조한다.
"""

import logging
import sys


# app/observability.py가 애플리케이션 전체에서 사용하는 로거 이름이다.
# 임포트 대신 이름으로 참조해 평가 코드가 그 모듈의 변경에 영향받지 않게 한다.
APP_LOGGER_NAME = "master_agent"
# 애플리케이션 로거의 자식 로거를 사용한다. 자식은 자신의 레벨로 판정된 뒤
# 상위 핸들러로 전달되므로, 부모를 WARNING으로 낮춰도 진행 로그는 남는다.
EVALUATION_LOGGER_NAME = f"{APP_LOGGER_NAME}.evaluation"

evaluation_logger = logging.getLogger(EVALUATION_LOGGER_NAME)

# 평가처럼 행마다 HTTP 호출이 반복되는 실행에서, 요청 한 건마다 INFO 로그를
# 남겨 진행 상황을 가리는 외부 라이브러리 로거들이다.
NOISY_LIBRARY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "openai",
    "anthropic",
    "asyncio",
    "langchain",
    "langchain_core",
    "langgraph",
)


def configure_evaluation_logging(
    *,
    pipeline_level: str = "WARNING",
    progress_level: str = "INFO",
) -> None:
    """평가 실행의 진행 로그와 파이프라인 단계 로그를 분리해 설정한다."""

    # Windows PowerShell의 기본 코드페이지와 Python 출력 인코딩이 다르면 한글
    # 로그가 깨질 수 있다. 지원되는 스트림은 UTF-8로 명시해 그대로 출력한다.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    # 루트 레벨은 진행 로그가 통과할 수 있는 값이어야 한다.
    logging.basicConfig(
        level=log_level(progress_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger().setLevel(log_level(progress_level))
    logging.getLogger(APP_LOGGER_NAME).setLevel(log_level(pipeline_level))
    evaluation_logger.setLevel(log_level(progress_level))
    # 외부 라이브러리는 파이프라인 로그를 자세히 볼 때만 함께 자세히 남긴다.
    library_level = max(log_level(pipeline_level), logging.WARNING)
    for name in NOISY_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(library_level)


def log_level(level: str) -> int:
    """레벨 이름을 logging 상수로 바꾸고 알 수 없는 값은 INFO로 처리한다."""

    resolved = getattr(logging, str(level).upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO
