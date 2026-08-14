"""
Cuotas MLB públicas (sin API key) vía ESPN.

Fuente: scoreboard header de ESPN, moneyline de DraftKings.
Se usa como respaldo cuando OddsPapi / The Odds API no traen línea.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import requests

from lineas_betmgm import (
    american_a_decimal,
    buscar_lineas_partido,
    normalizar_nombre_equipo,
)

ESPN_HEADER = "https://site.web.api.espn.com/apis/v2/scoreboard/header"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

_cache: dict[tuple[str, str], dict[str, Any]] | None = None
_cache_ts: datetime | None = None
CACHE_MINUTES = 8

_session = requests.Session()


def invalidar_cache_espn() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = None


def _ml_int(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return n


def _ml_lado(odds: dict[str, Any], lado: str) -> int | None:
    bloque = odds.get(lado) if isinstance(odds.get(lado), dict) else {}
    ml = _ml_int((bloque or {}).get("moneyLine"))
    if ml is not None:
        return ml
    key = "homeTeamOdds" if lado == "home" else "awayTeamOdds"
    team = odds.get(key) if isinstance(odds.get(key), dict) else {}
    return _ml_int((team or {}).get("moneyLine"))


def parsear_eventos_espn(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Convierte el JSON del header ESPN en mapa (away_norm, home_norm) → cuotas."""
    mapa: dict[tuple[str, str], dict[str, Any]] = {}
    sports = payload.get("sports") or []
    leagues = (sports[0].get("leagues") if sports else []) or []
    events = (leagues[0].get("events") if leagues else []) or []
    for ev in events:
        odds = ev.get("odds") if isinstance(ev.get("odds"), dict) else {}
        ml_away = _ml_lado(odds, "away")
        ml_home = _ml_lado(odds, "home")
        if ml_away is None or ml_home is None:
            continue
        away_name = ""
        home_name = ""
        for c in ev.get("competitors") or []:
            if not isinstance(c, dict):
                continue
            nombre = str(c.get("displayName") or "").strip()
            if c.get("homeAway") == "away":
                away_name = nombre
            elif c.get("homeAway") == "home":
                home_name = nombre
        if not away_name:
            away_name = str(((odds.get("awayTeamOdds") or {}).get("team") or {}).get("displayName") or "")
        if not home_name:
            home_name = str(((odds.get("homeTeamOdds") or {}).get("team") or {}).get("displayName") or "")
        if not away_name or not home_name:
            continue
        provider = ((odds.get("provider") or {}).get("name") or "DraftKings").strip()
        casa = provider.lower().replace(" ", "") or "draftkings"
        ka, kh = normalizar_nombre_equipo(away_name), normalizar_nombre_equipo(home_name)
        mapa[(ka, kh)] = {
            "away": {
                "american": ml_away,
                "decimal": american_a_decimal(ml_away),
                "casa": casa,
                "lado": "away",
            },
            "home": {
                "american": ml_home,
                "decimal": american_a_decimal(ml_home),
                "casa": casa,
                "lado": "home",
            },
            "provider": provider,
            "espn_id": ev.get("id"),
        }
    return mapa


def obtener_lineas_espn(timeout: float = 12.0) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    meta: dict[str, Any] = {
        "ok": False,
        "fuente": "espn",
        "mensaje": "",
        "partidos": 0,
        "requiere_key": False,
    }
    ahora = datetime.now()
    global _cache, _cache_ts
    if _cache is not None and _cache_ts and ahora - _cache_ts < timedelta(minutes=CACHE_MINUTES):
        return _cache, {
            **meta,
            "ok": True,
            "partidos": len(_cache),
            "cache": True,
            "mensaje": f"{len(_cache)} partidos ESPN/DraftKings (cache)",
        }
    try:
        r = _session.get(
            ESPN_HEADER,
            params={"sport": "baseball", "league": "mlb"},
            headers=_HEADERS,
            timeout=timeout,
        )
        r.raise_for_status()
        mapa = parsear_eventos_espn(r.json())
    except Exception as e:
        meta["mensaje"] = f"ESPN cuotas: {e}"[:200]
        return {}, meta

    _cache = mapa
    _cache_ts = ahora
    meta["ok"] = bool(mapa)
    meta["partidos"] = len(mapa)
    meta["mensaje"] = (
        f"{len(mapa)} partidos con moneyline ESPN (DraftKings)"
        if mapa
        else "ESPN sin moneyline MLB ahora"
    )
    return mapa, meta


def aplicar_lineas_espn(
    juegos: list[dict],
    cfg: dict | None = None,
    *,
    solo_vacios: bool = True,
) -> tuple[list[dict], dict]:
    """Rellena moneyline en juegos que aún no tienen cuota de casa."""
    del cfg
    mapa, meta = obtener_lineas_espn()
    aplicados = 0
    for juego in juegos:
        if solo_vacios and juego.get("odds_away_decimal") and juego.get("odds_home_decimal"):
            continue
        lineas = buscar_lineas_partido(mapa, juego.get("visitante") or "", juego.get("home") or "")
        if not lineas:
            continue
        away_l = lineas.get("away") or {}
        home_l = lineas.get("home") or {}
        if not away_l.get("decimal") or not home_l.get("decimal"):
            continue
        juego["odds_away_american"] = away_l.get("american")
        juego["odds_away_decimal"] = away_l.get("decimal")
        juego["odds_home_american"] = home_l.get("american")
        juego["odds_home_decimal"] = home_l.get("decimal")
        juego["lineas_fuente"] = away_l.get("casa") or home_l.get("casa") or "espn"
        juego["lineas_betmgm"] = lineas
        aplicados += 1
    meta["ok"] = aplicados > 0
    meta["partidos_aplicados"] = aplicados
    if aplicados:
        meta["mensaje"] = f"ESPN/DraftKings: {aplicados} juegos con cuota real"
    return juegos, meta
