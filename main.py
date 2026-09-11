"""Uvicorn이 불러오는 FastAPI ASGI 진입점."""

import os

import uvicorn

from app.api import create_app


# 그래프 인스턴스는 프로세스마다 하나씩 만들지만, HTTP 요청 사이의 HITL 상태는
# 일반 Redis에 저장한다. 따라서 서버 재시작이나 여러 컨테이너에서도 같은
# Redis를 바라보면 /resume 요청이 대기 상태를 다시 읽을 수 있다.
app = create_app()


if __name__ == "__main__":
    # python main.py로 직접 실행하는 경우에만 내장 Uvicorn 서버를 시작한다.
    app_port = int(os.getenv("PORT", "8080"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=app_port,
    )
