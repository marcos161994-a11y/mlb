#!/usr/bin/env bash
# Despierta mlb-1 antes de un cron. El plan free de Render puede tardar
# cerca de un minuto en responder si el servicio estaba dormido.
set -euo pipefail

url="${1:-${RENDER_URL:-}}"
url="${url%/}"
if [ -z "$url" ]; then
  echo "::error::RENDER_URL vacía"
  exit 1
fi

echo "Despertar Render ${url}"
ok=0
for i in 1 2 3 4 5 6 7 8; do
  if curl -fsS --retry 2 --retry-all-errors --retry-delay 5 --max-time 45 \
      "${url}/api/health" -o /tmp/mlb-health.json; then
    echo "Health OK en intento ${i}"
    python3 - <<'PY' || true
import json
d = json.load(open("/tmp/mlb-health.json", encoding="utf-8"))
p = d.get("persistencia") or {}
print(f"backend={p.get('backend')} durable={p.get('durable')} aviso={p.get('aviso') or ''}")
PY
    ok=1
    break
  fi
  echo "Intento ${i}: servicio dormido, esperando arranque"
  sleep 15
done

if [ "$ok" != "1" ]; then
  echo "::error::El servicio no despertó."
  exit 1
fi
