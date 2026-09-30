#!/usr/bin/env bash
set -euo pipefail
PORT="${PORT:-10000}"
if [ -n "${DATABASE_URL:-}" ]; then
  echo "[start] Quantum MLB · PORT=${PORT} · persistencia=postgres · DATA_DIR=${DATA_DIR:-.}"
else
  echo "[start] Quantum MLB · PORT=${PORT} · persistencia=sqlite · DATA_DIR=${DATA_DIR:-.}"
  echo "[start] Sin DATABASE_URL el disco de Render free no conserva la memoria."
fi
exec uvicorn servidor_mlb:app --host 0.0.0.0 --port "${PORT}" --workers 1 --timeout-keep-alive 75
