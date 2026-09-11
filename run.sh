#!/usr/bin/env bash
# Start OilTrace AI. Usage: ./run.sh [port]
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
echo "OilTrace AI -> http://127.0.0.1:${PORT}"
exec .venv/bin/uvicorn backend.main:app --reload --port "${PORT}"
