"""
Métricas avanzadas de pitcher: FIP, xFIP, K%, BB%.

Fuente principal: MLB StatsAPI (fiable en Render).
FanGraphs/pybaseball queda como overlay opcional (a menudo 403).
"""

from __future__ import annotations

import math
import time
from typing import Any

import requests

# Constante FIP típica MLB reciente
FIP_CONSTANT = 3.10
LG_HR_FB = 0.105  # HR/FB de liga aprox. para xFIP

_session = requests.Session()
_fg_cache: dict[str, Any] | None = None
_fg_ts: float = 0.0
FG_TTL = 6 * 3600


def _ip_a_float(ip_raw: Any) -> float:
    """MLB usa outs en décimas: 96.2 = 96 + 2/3."""
    if ip_raw is None:
        return 0.0
    try:
        s = str(ip_raw)
        if "." in s:
            enteros, dec = s.split(".", 1)
            outs = int(dec[0]) if dec else 0
            return float(enteros) + outs / 3.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def calcular_fip(hr: float, bb: float, hbp: float, k: float, ip: float) -> float | None:
    if ip <= 0:
        return None
    return round((13.0 * hr + 3.0 * (bb + hbp) - 2.0 * k) / ip + FIP_CONSTANT, 2)


def calcular_xfip(
    hr: float,
    bb: float,
    hbp: float,
    k: float,
    ip: float,
    air_outs: float | None = None,
) -> float | None:
    """
    xFIP: reemplaza HR por FB esperados * HR/FB de liga.
    Sin fly balls fiables, estima FB ≈ airOuts*0.55 + HR.
    """
    if ip <= 0:
        return None
    if air_outs is not None and air_outs >= 0:
        fb = max(hr, air_outs * 0.55 + hr)
    else:
        # Sin air outs: usa HR de liga implícito (~1.15 HR/9)
        fb = (ip / 9.0) * (1.15 / LG_HR_FB)
    hr_esperados = fb * LG_HR_FB
    return round((13.0 * hr_esperados + 3.0 * (bb + hbp) - 2.0 * k) / ip + FIP_CONSTANT, 2)


def enriquecer_stats_pitcher(stat: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Añade fip, xfip, k_pct, bb_pct a un dict de pitcher."""
    out = dict(base)
    ip = _ip_a_float(stat.get("inningsPitched"))
    k = float(stat.get("strikeOuts") or 0)
    bb = float(stat.get("baseOnBalls") or 0)
    hbp = float(stat.get("hitByPitch") or 0)
    hr = float(stat.get("homeRuns") or 0)
    bf = float(stat.get("battersFaced") or 0)
    air = stat.get("airOuts")
    air_f = float(air) if air is not None else None

    fip = calcular_fip(hr, bb, hbp, k, ip)
    xfip = calcular_xfip(hr, bb, hbp, k, ip, air_f)
    k_pct = round(100.0 * k / bf, 1) if bf > 0 else None
    bb_pct = round(100.0 * bb / bf, 1) if bf > 0 else None

    # Fallbacks desde rates si no hay conteos
    if k_pct is None and out.get("k9") is not None and ip > 0:
        k_pct = round(float(out["k9"]) * 100.0 / (float(out.get("whip") or 1.3) * 9.0 + float(out["k9"]) + 3.0), 1)
    if fip is None:
        # Aprox gruesa desde ERA/HR9/BB9/K9
        era = float(out.get("era") or 4.5)
        fip = round(0.55 * era + 0.45 * (
            (13.0 * float(out.get("hr9") or 1.0) + 3.0 * float(out.get("bb9") or 3.0) - 2.0 * float(out.get("k9") or 7.5)) / 9.0
            + FIP_CONSTANT
        ), 2)
    if xfip is None:
        xfip = fip
    if k_pct is None:
        k_pct = round(float(out.get("k9") or 7.5) * 2.4, 1)  # ~K9→K% rough
    if bb_pct is None:
        bb_pct = round(float(out.get("bb9") or 3.0) * 2.5, 1)

    out.update(
        {
            "fip": float(fip),
            "xfip": float(xfip),
            "k_pct": float(k_pct),
            "bb_pct": float(bb_pct),
            "ip": round(ip, 1),
            "metricas_fuente": "statsapi-fip",
        }
    )
    return out


def intentar_overlay_fangraphs(nombre: str, season: int, stats: dict[str, Any]) -> dict[str, Any]:
    """
    Si pybaseball/FanGraphs responde, sobrescribe FIP/xFIP/K%/BB%.
    Si falla (403 típico), deja StatsAPI.
    """
    global _fg_cache, _fg_ts
    out = dict(stats)
    try:
        ahora = time.time()
        if _fg_cache is None or (ahora - _fg_ts) > FG_TTL or _fg_cache.get("season") != season:
            from pybaseball import pitching_stats

            df = pitching_stats(season, qual=1)
            if df is None or len(df) == 0:
                return out
            # Indexar por nombre normalizado
            idx: dict[str, dict[str, Any]] = {}
            for _, row in df.iterrows():
                nm = str(row.get("Name") or "").strip().lower()
                if not nm:
                    continue
                idx[nm] = {
                    "fip": _safe_float(row.get("FIP")),
                    "xfip": _safe_float(row.get("xFIP")),
                    "k_pct": _safe_pct(row.get("K%")),
                    "bb_pct": _safe_pct(row.get("BB%")),
                }
            _fg_cache = {"season": season, "by_name": idx}
            _fg_ts = ahora

        key = (nombre or "").strip().lower()
        hit = (_fg_cache or {}).get("by_name", {}).get(key)
        if not hit:
            # match apellido
            ap = key.split()[-1] if key else ""
            for nm, vals in ((_fg_cache or {}).get("by_name") or {}).items():
                if nm.endswith(ap) and ap:
                    hit = vals
                    break
        if hit and hit.get("fip") is not None:
            out["fip"] = hit["fip"]
            if hit.get("xfip") is not None:
                out["xfip"] = hit["xfip"]
            if hit.get("k_pct") is not None:
                out["k_pct"] = hit["k_pct"]
            if hit.get("bb_pct") is not None:
                out["bb_pct"] = hit["bb_pct"]
            out["metricas_fuente"] = "fangraphs"
    except Exception:
        pass
    return out


def _safe_float(v: Any) -> float | None:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _safe_pct(v: Any) -> float | None:
    """FanGraphs a veces da 0.28 o 28."""
    x = _safe_float(v)
    if x is None:
        return None
    if x <= 1.0:
        return round(x * 100.0, 1)
    return round(x, 1)
