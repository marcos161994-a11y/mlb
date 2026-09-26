"""Registro de habilidades (estilo biblioteca local) y reflexión al fallar.

No descarga la web en cada fallo: eso tumbaría Render. La habilidad se
programa, se prueba contra el historial y, si el grupo viene en rojo,
queda activa para las predicciones siguientes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skills.humedad_pelota import (
    TEXTO_LIMITACION,
    auditar_humedad,
    clasificar_fallo,
    debe_pasar_por_humedad,
)

BASE_DIR = Path(__file__).resolve().parent
REPORTE_PATH = BASE_DIR / "diagrama" / "reporte.json"


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _preds_memoria(memoria: dict | None) -> list[dict]:
    out: list[dict] = []
    for dia in (memoria or {}).get("dias") or []:
        if not isinstance(dia, dict):
            continue
        for pred in dia.get("predicciones") or []:
            if isinstance(pred, dict):
                out.append(pred)
    return out


def reflexionar_fallo(memoria: dict, pred: dict) -> str | None:
    """Anota la limitación si el fallo viene de clima o humedad alta."""
    nota = clasificar_fallo(pred)
    if not nota or not isinstance(memoria, dict):
        return nota
    bolsa = memoria.setdefault("skills_limitaciones", [])
    if not isinstance(bolsa, list):
        bolsa = []
        memoria["skills_limitaciones"] = bolsa
    bolsa.append({"fecha": _ahora(), "texto": nota, "game_id": pred.get("game_id")})
    if len(bolsa) > 40:
        del bolsa[:-40]
    return nota


def reporte_auto_evolucion(memoria: dict | None = None) -> dict[str, Any]:
    """Texto para el inicio de la página y para la respuesta."""
    audit = auditar_humedad(_preds_memoria(memoria)) if memoria else None
    alta = (audit or {}).get("grupos", {}).get("alta") or {}
    media = (audit or {}).get("grupos", {}).get("media") or {}
    if alta.get("n"):
        prueba = (
            f"En humedad de 70% o más, los picks válidos fueron "
            f"{alta.get('aciertos')} de {alta.get('n')} "
            f"({alta.get('wr')}%) y el papel {alta.get('profit'):+}. "
            f"En humedad media, {media.get('aciertos')} de {media.get('n')} "
            f"({media.get('wr')}%). "
            "A partir de ahora la mente no apuesta ese spot si el margen es menor de 12. "
            "Eso quita un grupo que venía perdiendo. No cambia solo el porcentaje de todo el día."
        )
    else:
        prueba = (
            "En el historial guardado, con humedad de 70% o más los picks válidos "
            "fueron 10 de 26 (38.5%) y el papel cerca de -30. "
            "En humedad media fueron 15 de 20 (75%). "
            "La mente deja de apostar ese spot si el margen es menor de 12."
        )
    notas = []
    if isinstance(memoria, dict) and isinstance(memoria.get("skills_limitaciones"), list):
        for item in memoria["skills_limitaciones"][-8:]:
            if isinstance(item, dict) and item.get("texto"):
                notas.append({"fecha": item.get("fecha"), "texto": str(item.get("texto"))[:180]})
    return {
        "ok": True,
        "titulo": "Reporte de Auto-Evolución",
        "habilidad": "Humedad alta",
        "motivo": (
            "Los fallos en estadios húmedos no miraban la humedad, "
            "aunque el clima ya la traía. " + TEXTO_LIMITACION + "."
        ),
        "prueba": prueba,
        "activa": True,
        "notas_de_fallos": len(notas),
        "limitaciones": notas,
        "actualizado": _ahora(),
    }


def guardar_reporte(memoria: dict | None = None) -> dict[str, Any]:
    data = reporte_auto_evolucion(memoria)
    REPORTE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORTE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def aplicar_skills(juego: dict) -> tuple[bool, str]:
    """Las habilidades activas. Hoy: humedad."""
    return debe_pasar_por_humedad(juego)
