#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-10000}"
echo "[start] Quantum MLB · PORT=${PORT} · DATA_DIR=${DATA_DIR:-.}"
exec uvicorn servidor_mlb:app --host 0.0.0.0 --port "${PORT}" --workers 1 --timeout-keep-alive 75
