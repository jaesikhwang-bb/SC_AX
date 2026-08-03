#!/usr/bin/env bash
set -euo pipefail

# WSL에서 Windows 마운트 경로(/mnt/c)를 watchfiles의 네이티브 watcher로
# 감시하면 권한 오류가 발생할 수 있다. polling 방식은 파일 변경을 주기적으로
# 확인하므로 같은 경로에서도 안정적으로 --reload를 사용할 수 있다.
export WATCHFILES_FORCE_POLLING=true

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

ENV_FILE="${ENV_FILE:-.env}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"

exec uvicorn main:app \
  --env-file "${ENV_FILE}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --reload \
  --reload-exclude ".pytest_cache" \
  --reload-exclude ".pytest_cache/*"
