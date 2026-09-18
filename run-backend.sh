#!/usr/bin/env bash
# Starts the FastAPI backend (serves API + WebSocket + built frontend on :8090)
# Works on Linux/macOS. Usage:  bash run-backend.sh
set -euo pipefail
cd "$(dirname "$0")/backend"

PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created backend/.env from .env.example — edit it to add credentials."
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8090}"
exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
