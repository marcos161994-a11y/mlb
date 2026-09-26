"""Bitácora visible de investigación y cambios de la mente.

No decide picks: solo deja plasmado qué se investigó, qué se tocó
y por qué debería ayudar al acierto. La página /diagrama la muestra.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
BITACORA_PATH = BASE_DIR / "diagrama" / "bitacora.json"


def _ahora_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cargar_bitacora() -> dict[str, Any]:
    if not BITACORA_PATH.exists():
        return {
            "ok": True,
            "titulo": "Bitácora de la mente",
            "actualizado": None,
            "entradas": [],
        }
    try:
        data = json.loads(BITACORA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "ok": False,
            "titulo": "Bitácora de la mente",
            "actualizado": None,
            "entradas": [],
        }
    if not isinstance(data, dict):
        return {"ok": False, "titulo": "Bitácora de la mente", "actualizado": None, "entradas": []}
    ents = data.get("entradas")
    if not isinstance(ents, list):
        ents = []
    return {
        "ok": True,
        "titulo": str(data.get("titulo") or "Bitácora de la mente"),
        "actualizado": data.get("actualizado"),
        "entradas": [e for e in ents if isinstance(e, dict)],
    }


def resumen_bitacora() -> dict[str, Any]:
    data = cargar_bitacora()
    ents = list(data.get("entradas") or [])
    return {
        "ok": bool(data.get("ok")),
        "titulo": data.get("titulo"),
        "actualizado": data.get("actualizado"),
        "total": len(ents),
        "entradas": ents,
    }


def agregar_entrada(
    titulo: str,
    resumen: str,
    *,
    tipo: str = "cambio",
    fuentes: list[str] | None = None,
    archivos: list[str] | None = None,
    efecto: str = "",
) -> dict[str, Any]:
    """Añade una nota al JSON (para siguientes ciclos autónomos)."""
    data = cargar_bitacora()
    entrada = {
        "id": f"b-{_ahora_iso().replace(':', '').replace('-', '')}",
        "fecha": _ahora_iso()[:10],
        "tipo": str(tipo or "cambio")[:24],
        "titulo": str(titulo or "Nota")[:120],
        "resumen": str(resumen or "")[:1200],
        "fuentes": [str(u)[:200] for u in (fuentes or [])][:8],
        "archivos": [str(a)[:80] for a in (archivos or [])][:12],
        "efecto": str(efecto or "")[:280],
    }
    ents = list(data.get("entradas") or [])
    ents.insert(0, entrada)
    out = {
        "titulo": data.get("titulo") or "Bitácora de la mente",
        "actualizado": _ahora_iso()[:10],
        "entradas": ents[:80],
    }
    BITACORA_PATH.parent.mkdir(parents=True, exist_ok=True)
    BITACORA_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return entrada
