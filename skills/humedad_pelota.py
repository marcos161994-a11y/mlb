"""Humedad alta en el estadio.

Open-Meteo ya mandaba la humedad y el modelo no la usaba para decidir.
En los picks válidos ya liquidados, con humedad de 70% o más el quién-gana
fue 10 de 26 (38.5%) y el papel perdió unos 30. Con humedad media fue 15 de 20.

La habilidad no esconde el pick. Solo niega el dinero si el margen no es grande.
"""

from __future__ import annotations

from typing import Any

UMBRAL_HUMEDAD = 70.0
EDGE_MIN_PARA_SEGUIR = 12.0
TEXTO_LIMITACION = "Limitación detectada: Falta de análisis de física climática"


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def humedad_de(juego: dict | None) -> float | None:
    if not isinstance(juego, dict):
        return None
    clima = juego.get("clima") if isinstance(juego.get("clima"), dict) else {}
    return _num(clima.get("humedad"))


def debe_pasar_por_humedad(juego: dict | None) -> tuple[bool, str]:
    """True si la apuesta debe pasar por humedad alta y margen corto."""
    h = humedad_de(juego)
    if h is None or h < UMBRAL_HUMEDAD:
        return False, ""
    edge = _num((juego or {}).get("edge")) or 0.0
    if edge >= EDGE_MIN_PARA_SEGUIR:
        return False, ""
    return True, f"Humedad {h:.0f}%: este patrón viene perdiendo si el margen no es grande"


def clasificar_fallo(pred: dict | None) -> str | None:
    """Si el fallo enseña un hueco de clima, devuelve la limitación. Si no, None."""
    if not isinstance(pred, dict) or pred.get("resultado") != "fallo":
        return None
    clima = pred.get("clima") if isinstance(pred.get("clima"), dict) else {}
    h = _num(clima.get("humedad"))
    if h is not None and h >= UMBRAL_HUMEDAD:
        return TEXTO_LIMITACION
    if not clima.get("ok"):
        return TEXTO_LIMITACION
    return None


def auditar_humedad(predicciones: list[dict]) -> dict[str, Any]:
    """Cuenta aciertos y papel en humedad alta, media y seca."""
    grupos = {
        "alta": {"n": 0, "aciertos": 0, "profit": 0.0},
        "media": {"n": 0, "aciertos": 0, "profit": 0.0},
        "seca": {"n": 0, "aciertos": 0, "profit": 0.0},
    }
    for pred in predicciones or []:
        if not isinstance(pred, dict) or pred.get("resultado") not in ("acierto", "fallo"):
            continue
        if pred.get("valida_stats") is False:
            continue
        h = humedad_de(pred)
        if h is None:
            continue
        if h >= UMBRAL_HUMEDAD:
            key = "alta"
        elif h < 40:
            key = "seca"
        else:
            key = "media"
        g = grupos[key]
        g["n"] += 1
        if pred.get("resultado") == "acierto":
            g["aciertos"] += 1
        g["profit"] = round(g["profit"] + (_num(pred.get("profit")) or 0.0), 2)
    for g in grupos.values():
        g["wr"] = round(100.0 * g["aciertos"] / g["n"], 1) if g["n"] else None
    alta = grupos["alta"]
    activa = bool(alta["n"] >= 20 and alta["wr"] is not None and alta["wr"] < 45)
    return {"grupos": grupos, "activar": activa}
