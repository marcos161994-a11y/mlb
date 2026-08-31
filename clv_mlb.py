"""Closing Line Value (CLV) vs Pinnacle — métrica sharp para MLB."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from inteligencia_mlb import odds_justas_sin_vig


def cuotas_pinnacle(juego: dict[str, Any]) -> tuple[float | None, float | None]:
    """Decimal away/home de Pinnacle si está en lineas_libros o fuente principal."""
    libros = juego.get("lineas_libros") if isinstance(juego.get("lineas_libros"), list) else []
    for b in libros:
        if not isinstance(b, dict):
            continue
        if str(b.get("casa") or "").lower() != "pinnacle":
            continue
        try:
            away = float(b.get("away") or 0)
            home = float(b.get("home") or 0)
        except (TypeError, ValueError):
            continue
        if away > 1.0 and home > 1.0:
            return away, home
    fuente = str(juego.get("lineas_fuente") or "").lower()
    if fuente == "pinnacle":
        try:
            away = float(juego.get("odds_away_decimal") or juego.get("odds") or 0)
            home = float(juego.get("odds_home_decimal") or 0)
        except (TypeError, ValueError):
            return None, None
        if away > 1.0 and home > 1.0:
            return away, home
    return None, None


def cuota_pick_decimal(
    pick: str,
    visitante: str,
    home: str,
    dec_away: float,
    dec_home: float,
) -> float | None:
    p = (pick or "").strip()
    if visitante and visitante in p:
        return dec_away
    if home and home in p:
        return dec_home
    return None


def clv_pct(
    odds_entrada: float,
    dec_away: float,
    dec_home: float,
    pick: str,
    visitante: str,
    home: str,
) -> float | None:
    """
    CLV% = (odds_entrada / fair_decimal_cierre - 1) × 100
    fair_decimal = cuota justa sin vig del lado apostado (Pinnacle cierre).
    """
    try:
        ent = float(odds_entrada)
        da = float(dec_away)
        dh = float(dec_home)
    except (TypeError, ValueError):
        return None
    if ent <= 1.0 or da <= 1.0 or dh <= 1.0:
        return None
    fair = odds_justas_sin_vig(da, dh)
    if not fair.get("ok"):
        return None
    p = (pick or "").strip()
    if visitante and visitante in p:
        fair_dec = float(fair["dec_away_fair"])
    elif home and home in p:
        fair_dec = float(fair["dec_home_fair"])
    else:
        return None
    if fair_dec <= 1.0:
        return None
    return round((ent / fair_dec - 1.0) * 100.0, 2)


def actualizar_clv_registro(
    reg: dict[str, Any],
    juego: dict[str, Any],
    *,
    fase: str = "entrada",
) -> bool:
    """
    fase=entrada: congela odds de entrada vs Pinnacle del momento.
    fase=cierre: snapshot pre-partido (T-45/T-30 o último refresh).
    """
    if not isinstance(reg, dict) or not isinstance(juego, dict):
        return False
    away, home = cuotas_pinnacle(juego)
    if not away or not home:
        return False
    pick = reg.get("pick") or juego.get("pick") or ""
    visitante = reg.get("visitante") or juego.get("visitante") or ""
    home_name = reg.get("home") or juego.get("home") or ""
    now = datetime.now().isoformat()

    if fase == "entrada":
        odds_in = reg.get("odds") or reg.get("odds_congelada") or juego.get("odds")
        try:
            odds_in_f = float(odds_in or 0)
        except (TypeError, ValueError):
            return False
        if odds_in_f <= 1.0:
            return False
        reg["clv_odds_entrada"] = odds_in_f
        reg["clv_pin_entrada_away"] = away
        reg["clv_pin_entrada_home"] = home
        reg["clv_entrada_en"] = now
        clv = clv_pct(odds_in_f, away, home, pick, visitante, home_name)
        if clv is not None:
            reg["clv_entrada_pct"] = clv
        return True

    if fase == "cierre":
        reg["clv_pin_cierre_away"] = away
        reg["clv_pin_cierre_home"] = home
        reg["clv_cierre_en"] = now
        odds_in = reg.get("clv_odds_entrada") or reg.get("odds") or reg.get("odds_congelada")
        try:
            odds_in_f = float(odds_in or 0)
        except (TypeError, ValueError):
            return False
        if odds_in_f <= 1.0:
            return True
        clv = clv_pct(odds_in_f, away, home, pick, visitante, home_name)
        if clv is not None:
            reg["clv_pct"] = clv
        return True

    return False


def resumen_clv_memoria(memoria: dict) -> dict[str, Any]:
    """Promedio CLV cierre y entrada para panel."""
    cierre: list[float] = []
    entrada: list[float] = []
    con_dinero: list[float] = []

    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        for reg in (dia.get("predicciones") or []) + (dia.get("apuestas") or []):
            if not isinstance(reg, dict):
                continue
            if reg.get("clv_pct") is not None:
                try:
                    v = float(reg["clv_pct"])
                    cierre.append(v)
                    if reg.get("con_dinero") or reg.get("estado") in ("ganada", "perdida", "pendiente"):
                        if reg in (dia.get("apuestas") or []):
                            con_dinero.append(v)
                except (TypeError, ValueError):
                    pass
            if reg.get("clv_entrada_pct") is not None:
                try:
                    entrada.append(float(reg["clv_entrada_pct"]))
                except (TypeError, ValueError):
                    pass

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 2) if xs else None

    avg = _avg(cierre)
    return {
        "muestras_cierre": len(cierre),
        "muestras_entrada": len(entrada),
        "muestras_dinero": len(con_dinero),
        "clv_promedio": avg,
        "clv_entrada_promedio": _avg(entrada),
        "clv_dinero_promedio": _avg(con_dinero),
        "clv_positivo_pct": round(100 * sum(1 for x in cierre if x > 0) / len(cierre), 1) if cierre else None,
        "nivel": (
            "ok" if avg is not None and avg >= 1.5
            else "aviso" if avg is not None and avg >= 0
            else "alerta" if avg is not None
            else "sin_datos"
        ),
        "mensaje": _mensaje_clv(avg, len(cierre), len(con_dinero)),
    }


def _mensaje_clv(avg: float | None, n: int, n_din: int) -> str:
    if avg is None or n == 0:
        return "CLV: sin muestras Pinnacle aún"
    sign = "+" if avg >= 0 else ""
    base = f"CLV medio {sign}{avg:.1f}% ({n} picks"
    if n_din:
        base += f", {n_din} dinero"
    base += ") vs cierre Pinnacle"
    if avg >= 2.0:
        base += " · edge sharp"
    elif avg < 0:
        base += " · peor que el cierre"
    return base
