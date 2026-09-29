"""Fichas de aprendizaje.

Cada situación que el pick ya guarda (descanso, serie, bullpen, viento)
tiene su propia cuenta: cuántos, cuántos aciertos y cuánto papel.
Solo se vuelve regla de PASAR si el grupo es grande y viene peor que el resto.
El cierre (CLV) se anota en la ficha; no se usa para bloquear antes del juego,
porque todavía no existe.
"""

from __future__ import annotations

from typing import Any, Callable

MIN_N = 20
MIN_RESTO = 20
WR_MAX = 45.0
BRECHA_MIN = 8.0
EDGE_PARA_SEGUIR = 12.0
FATIGA_VIAJE = 0.6
FATIGA_BULLPEN = 0.7
VIENTO_MPH = 12.0


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _lado(reg: dict) -> str | None:
    pick = str(reg.get("pick") or "")
    visitante = str(reg.get("visitante") or "")
    home = str(reg.get("home") or "")
    if visitante and visitante in pick:
        return "away"
    if home and home in pick:
        return "home"
    return None


def _humanos_lado(reg: dict) -> dict:
    humanos = reg.get("factores_humanos") if isinstance(reg.get("factores_humanos"), dict) else {}
    lado = _lado(reg)
    if lado and isinstance(humanos.get(lado), dict):
        return humanos[lado]
    feats = humanos.get("features_away" if lado == "away" else "features_home")
    return feats if isinstance(feats, dict) else {}


def _serie(reg: dict) -> dict:
    humanos = reg.get("factores_humanos") if isinstance(reg.get("factores_humanos"), dict) else {}
    serie = humanos.get("serie") if isinstance(humanos.get("serie"), dict) else {}
    return serie


def es_descanso_corto(reg: dict) -> bool:
    """Jugó ayer y cruzó de zona. El día seguido, sin viaje, es el calendario normal."""
    lado = _humanos_lado(reg)
    b2b = bool(lado.get("back_to_back"))
    dias = _num(lado.get("dias_descanso"))
    if dias is not None and dias <= 0:
        b2b = True
    cambio = _num(lado.get("cambio_zona")) or 0.0
    return b2b and cambio >= 2


def es_viaje_largo(reg: dict) -> bool:
    lado = _humanos_lado(reg)
    fatiga = _num(lado.get("fatiga_viaje"))
    return fatiga is not None and fatiga >= FATIGA_VIAJE


def es_getaway_visita(reg: dict) -> bool:
    if _lado(reg) != "away":
        return False
    return bool(_serie(reg).get("getaway"))


def es_bullpen_cansado(reg: dict) -> bool:
    feats = reg.get("ml_features") if isinstance(reg.get("ml_features"), dict) else {}
    fatiga = _num(feats.get("fatiga_bullpen"))
    return fatiga is not None and fatiga >= FATIGA_BULLPEN


def es_viento_fuerte(reg: dict) -> bool:
    clima = reg.get("clima") if isinstance(reg.get("clima"), dict) else {}
    viento = _num(clima.get("viento_mph"))
    return viento is not None and viento >= VIENTO_MPH


FICHAS: tuple[dict[str, Any], ...] = (
    {
        "id": "descanso_corto",
        "nombre": "Descanso corto",
        "texto": "El equipo del pick jugó ayer y cruzó al menos dos husos.",
        "coincide": es_descanso_corto,
    },
    {
        "id": "viaje_largo",
        "nombre": "Viaje largo",
        "texto": "El equipo del pick llega con fatiga de viaje.",
        "coincide": es_viaje_largo,
    },
    {
        "id": "getaway_visita",
        "nombre": "Último de serie de visita",
        "texto": "El pick es la visita en el último juego de la serie.",
        "coincide": es_getaway_visita,
    },
    {
        "id": "bullpen_cansado",
        "nombre": "Bullpen cansado",
        "texto": "El bullpen del lado del pick viene cargado.",
        "coincide": es_bullpen_cansado,
    },
    {
        "id": "viento_fuerte",
        "nombre": "Viento fuerte",
        "texto": "En el estadio el viento pasa de 12 mph.",
        "coincide": es_viento_fuerte,
    },
)


def _valida(pred: dict) -> bool:
    if pred.get("resultado") not in ("acierto", "fallo"):
        return False
    if pred.get("valida_stats") is False or pred.get("invalida_tarde") is True:
        return False
    return True


def _preds(memoria: dict | None) -> list[dict]:
    out: list[dict] = []
    for dia in (memoria or {}).get("dias") or []:
        if not isinstance(dia, dict):
            continue
        for pred in dia.get("predicciones") or []:
            if isinstance(pred, dict) and _valida(pred):
                out.append(pred)
    return out


def _resumen(preds: list[dict]) -> dict[str, Any]:
    n = len(preds)
    aciertos = sum(1 for p in preds if p.get("resultado") == "acierto")
    profit = 0.0
    clv_n = 0
    clv_contra = 0
    for p in preds:
        profit += float(_num(p.get("profit")) or 0.0)
        clv = _num(p.get("clv_pct"))
        if clv is None:
            continue
        clv_n += 1
        if clv < 0:
            clv_contra += 1
    wr = round(100.0 * aciertos / n, 1) if n else None
    return {
        "n": n,
        "aciertos": aciertos,
        "wr": wr,
        "profit": round(profit, 1),
        "clv_n": clv_n,
        "clv_en_contra": clv_contra,
    }


def _empeora(grupo: dict, resto: dict) -> bool:
    if int(grupo["n"]) < MIN_N or int(resto["n"]) < MIN_RESTO:
        return False
    if grupo["wr"] is None or resto["wr"] is None:
        return False
    if float(grupo["wr"]) >= WR_MAX:
        return False
    if float(grupo["profit"]) >= 0:
        return False
    return float(resto["wr"]) - float(grupo["wr"]) >= BRECHA_MIN


def auditar_fichas(memoria: dict | None) -> dict[str, Any]:
    """Cuenta cada situación y marca cuáles están en rojo frente al resto."""
    preds = _preds(memoria)
    base = _resumen(preds)
    fichas: list[dict[str, Any]] = []
    for spec in FICHAS:
        coincide: Callable[[dict], bool] = spec["coincide"]
        dentro = [p for p in preds if coincide(p)]
        fuera = [p for p in preds if not coincide(p)]
        grupo = _resumen(dentro)
        resto = _resumen(fuera)
        activa = _empeora(grupo, resto)
        fichas.append(
            {
                "id": spec["id"],
                "nombre": spec["nombre"],
                "texto": spec["texto"],
                "n": grupo["n"],
                "aciertos": grupo["aciertos"],
                "wr": grupo["wr"],
                "profit": grupo["profit"],
                "resto_n": resto["n"],
                "resto_wr": resto["wr"],
                "clv_n": grupo["clv_n"],
                "clv_en_contra": grupo["clv_en_contra"],
                "activa": activa,
            }
        )
    return {"ok": True, "base": base, "fichas": fichas, "activas": [f for f in fichas if f["activa"]]}


def debe_pasar_por_ficha(juego: dict | None, memoria: dict | None) -> tuple[bool, str]:
    """True si una ficha en rojo cubre este juego y el margen no es grande."""
    if not isinstance(juego, dict):
        return False, ""
    edge = _num(juego.get("edge")) or 0.0
    if edge >= EDGE_PARA_SEGUIR:
        return False, ""
    audit = auditar_fichas(memoria)
    for ficha in audit["activas"]:
        spec = next(s for s in FICHAS if s["id"] == ficha["id"])
        if spec["coincide"](juego):
            wr = ficha["wr"]
            return (
                True,
                f"{ficha['nombre']}: {ficha['aciertos']} de {ficha['n']} ({wr}%), "
                f"el resto va en {ficha['resto_wr']}%.",
            )
    return False, ""
