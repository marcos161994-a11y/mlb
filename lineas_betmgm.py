"""
Líneas moneyline de BetMGM vía The Odds API (https://the-odds-api.com).
Coloca tu API key en odds_api_key.txt (una línea).
"""

from __future__ import annotations

import re
from typing import Any
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
KEY_FILE = BASE_DIR / "odds_api_key.txt"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"

_cache: dict[tuple[str, str], dict[str, Any]] | None = None
_cache_ts: datetime | None = None
CACHE_MINUTES = 1  # Actualizar líneas con frecuencia durante el día

# MLB statsapi -> nombres típicos en The Odds API / BetMGM
# Usamos un solo nombre canónico (Oakland Athletics) para evitar rebotes en el mapeo
ALIASES: dict[str, str] = {
    "athletics": "oakland athletics",
    "oakland": "oakland athletics",
}


def _norm(nombre: str) -> str:
    s = nombre.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


def normalizar_nombre_equipo(nombre: str) -> str:
    n = _norm(nombre)
    return ALIASES.get(n, n)


def _match_key(away: str, home: str) -> tuple[str, str]:
    return normalizar_nombre_equipo(away), normalizar_nombre_equipo(home)


def cargar_api_key(cfg: dict) -> str | None:
    key = (cfg.get("lineas") or {}).get("api_key", "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key and not key.startswith("#"):
            return key
    import os

    return os.environ.get("ODDS_API_KEY", "").strip() or None


def american_a_decimal(price: float | int) -> float:
    p = float(price)
    if p > 0:
        return round(1 + p / 100, 3)
    if p < 0:
        return round(1 + 100 / abs(p), 3)
    return 1.0


def decimal_a_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int(round((decimal - 1) * 100))
    return int(round(-100 / (decimal - 1)))


def _extraer_betmgm(evento: dict, book_key: str) -> dict | None:
    for bm in evento.get("bookmakers", []):
        if bm.get("key") == book_key:
            for market in bm.get("markets", []):
                if market.get("key") == "h2h":
                    out = {}
                    for o in market.get("outcomes", []):
                        out[normalizar_nombre_equipo(o["name"])] = {
                            "nombre": o["name"],
                            "american": int(o["price"]),
                            "decimal": american_a_decimal(o["price"]),
                        }
                    return out
    return None


def obtener_lineas_betmgm(cfg: dict) -> tuple[dict[tuple[str, str], dict], dict]:
    """
    Devuelve (mapa_partidos, meta).
    mapa: (away_norm, home_norm) -> {away: {...}, home: {...}}
    """
    global _cache, _cache_ts
    meta = {"ok": False, "fuente": "betmgm", "mensaje": "", "partidos": 0}

    api_key = cargar_api_key(cfg)
    if not api_key:
        meta["mensaje"] = "Falta API key en odds_api_key.txt (gratis en the-odds-api.com)"
        return {}, meta

    ahora = datetime.now()
    if _cache and _cache_ts and ahora - _cache_ts < timedelta(minutes=CACHE_MINUTES):
        return _cache, {**meta, "ok": True, "partidos": len(_cache), "cache": True}

    lineas_cfg = cfg.get("lineas", {})
    params = {
        "apiKey": api_key,
        "regions": lineas_cfg.get("region", "us"),
        "markets": lineas_cfg.get("mercado", "h2h"),
        "bookmakers": lineas_cfg.get("casa", "betmgm"),
        "oddsFormat": "american",
    }
    try:
        r = requests.get(ODDS_URL, params=params, timeout=25)
        if r.status_code == 401:
            meta["mensaje"] = "API key inválida. Revisa odds_api_key.txt"
            return {}, meta
        r.raise_for_status()
        eventos = r.json()
    except requests.RequestException as e:
        meta["mensaje"] = f"Error al pedir líneas BetMGM: {e}"
        return {}, meta

    mapa: dict[tuple[str, str], dict] = {}
    book = lineas_cfg.get("casa", "betmgm")
    for ev in eventos:
        away, home = ev.get("away_team", ""), ev.get("home_team", "")
        cuotas = _extraer_betmgm(ev, book)
        if not cuotas:
            continue
        ka, kh = _match_key(away, home)
        fila = {}
        if ka in cuotas:
            fila["away"] = {**cuotas[ka], "lado": "away"}
        if kh in cuotas:
            fila["home"] = {**cuotas[kh], "lado": "home"}
        if len(fila) == 2:
            mapa[(ka, kh)] = fila

    _cache = mapa
    _cache_ts = ahora
    meta["ok"] = True
    meta["partidos"] = len(mapa)
    meta["mensaje"] = f"{len(mapa)} partidos con líneas BetMGM"
    remaining = r.headers.get("x-requests-remaining")
    if remaining:
        meta["requests_restantes"] = remaining
    return mapa, meta


def buscar_lineas_partido(
    mapa: dict[tuple[str, str], dict], visitante: str, home: str
) -> dict | None:
    ka, kh = _match_key(visitante, home)
    if (ka, kh) in mapa:
        return mapa[(ka, kh)]
    if (kh, ka) in mapa:
        m = mapa[(kh, ka)]
        return {"away": m.get("home"), "home": m.get("away")}
    return None


def aplicar_lineas_a_juegos(juegos: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    mapa, meta = obtener_lineas_betmgm(cfg)
    for juego in juegos:
        lineas = buscar_lineas_partido(mapa, juego["visitante"], juego["home"])
        juego["lineas_betmgm"] = lineas
        juego["odds_away_american"] = None
        juego["odds_home_american"] = None
        juego["odds_away_decimal"] = None
        juego["odds_home_decimal"] = None
        juego["lineas_fuente"] = "modelo"

        if lineas:
            away_l = lineas.get("away")
            home_l = lineas.get("home")
            if away_l:
                juego["odds_away_american"] = away_l["american"]
                juego["odds_away_decimal"] = away_l["decimal"]
            if home_l:
                juego["odds_home_american"] = home_l["american"]
                juego["odds_home_decimal"] = home_l["decimal"]
            juego["lineas_fuente"] = "betmgm"

    return juegos, meta
