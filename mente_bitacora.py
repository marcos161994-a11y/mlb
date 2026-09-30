"""Bitácora visible de investigación y cambios de la mente.

No decide picks: solo lee qué se investigó y qué se tocó.
La red en /mente usa el conteo. No escribe archivos del diagrama.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
BITACORA_PATH = BASE_DIR / "mente" / "bitacora.json"


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
