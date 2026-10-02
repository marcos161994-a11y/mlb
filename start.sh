#!/usr/bin/env bash
set -euo pipefail
# glibc abre una arena por hilo. En el host de Render hay muchos núcleos
# y el pico del historial se queda pegado. Con 2 arenas la RAM vuelve a bajar.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
PORT="${PORT:-10000}"
if [ -n "${DATABASE_URL:-}" ]; then
  echo "[start] Quantum MLB · PORT=${PORT} · persistencia=postgres · DATA_DIR=${DATA_DIR:-.}"
else
  echo "[start] Quantum MLB · PORT=${PORT} · persistencia=sqlite · DATA_DIR=${DATA_DIR:-.}"
  echo "[start] Sin DATABASE_URL el disco de Render free no conserva la memoria."
fi
exec uvicorn servidor_mlb:app --host 0.0.0.0 --port "${PORT}" --workers 1 --timeout-keep-alive 75
