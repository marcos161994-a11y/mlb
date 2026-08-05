"""
Cuotas MLB vía OddsPapi (https://oddspapi.io).

Free tier ~250 req/mes: 1 request de fixtures + 1 de odds-by-tournaments
cubre todo el slate (mucho más eficiente que The Odds API).

Env: ODDSPAPI_API_KEY (o ODDS_PAPI_KEY / lineas.api_key)
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from lineas_betmgm import (
    american_a_decimal,
    decimal_a_american,
    normalizar_nombre_equipo,
)

BASE_URL = "https://api.oddspapi.io/v4"
BASE_DIR = Path(__file__).resolve().parent
KEY_FILE = BASE_DIR / "oddspapi_api_key.txt"

SPORT_ID_BASEBALL = 13
TOURNAMENT_MLB = 109
MARKET_MONEYLINE = "131"

_cache: dict[tuple[str, str], dict[str, Any]] | None = None
_cache_ts: datetime | None = None
CACHE_MINUTES = 10  # free tier ~250 req/mes: ~2 req por refresco


def _norm(nombre: str) -> str:
    s = nombre.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


def cargar_api_key(cfg: dict) -> str | None:
    lineas = cfg.get("lineas") or {}
    for candidate in (
        os.environ.get("ODDSPAPI_API_KEY"),
        os.environ.get("ODDS_PAPI_KEY"),
        lineas.get("api_key"),
        KEY_FILE.read_text(encoding="utf-8") if KEY_FILE.exists() else None,
    ):
        if candidate is None:
            continue
        key = str(candidate).strip().replace("\ufeff", "").replace("\r", "").replace("\n", "")
        if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
            key = key[1:-1].strip()
        if key.lower().startswith("bearer "):
            key = key[7:].strip()
        if key and key.upper() not in ("YOUR_API_KEY", "APIKEY", "XXX"):
            return key
    return None


def _extraer_ml_libro(bookie: dict) -> tuple[float | None, float | None, str | None]:
    """Devuelve (decimal_home, decimal_away, slug_ok)."""
    markets = bookie.get("markets") or {}
    market = markets.get(MARKET_MONEYLINE) or markets.get(131)
    if not market:
        return None, None, None
    home_px = away_px = None
    for outcome in (market.get("outcomes") or {}).values():
        for player in (outcome.get("players") or {}).values():
            if not player.get("active", True):
                continue
            boid = str(player.get("bookmakerOutcomeId") or "").lower()
            price = player.get("price")
            if price is None:
                continue
            try:
                dec = float(price)
            except (TypeError, ValueError):
                continue
            if dec <= 1.0:
                continue
            if "home" in boid or boid in ("1", "h"):
                home_px = dec
            elif "away" in boid or boid in ("2", "a"):
                away_px = dec
    if home_px and away_px:
        return home_px, away_px, None
    return None, None, None


def _mejor_ml_fixture(bookmaker_odds: dict, book_keys: list[str]) -> dict | None:
    """Mejor cuota decimal home/away entre books preferidos (o todos si no hay match)."""
    preferidos = [b.strip().lower() for b in book_keys if b.strip()]
    candidatos = []
    for slug, bookie in (bookmaker_odds or {}).items():
        if not isinstance(bookie, dict):
            continue
        if preferidos and slug.lower() not in preferidos:
            continue
        h, a, _ = _extraer_ml_libro(bookie)
        if h and a:
            candidatos.append((slug, h, a))
    if not candidatos and preferidos:
        # Fallback: cualquier book si los preferidos no listan MLB
        for slug, bookie in (bookmaker_odds or {}).items():
            if not isinstance(bookie, dict):
                continue
            h, a, _ = _extraer_ml_libro(bookie)
            if h and a:
                candidatos.append((slug, h, a))
    if not candidatos:
        return None

    best_home = max(candidatos, key=lambda x: x[1])
    best_away = max(candidatos, key=lambda x: x[2])
    return {
        "home": {
            "decimal": round(best_home[1], 3),
            "american": decimal_a_american(best_home[1]),
            "casa": best_home[0],
        },
        "away": {
            "decimal": round(best_away[2], 3),
            "american": decimal_a_american(best_away[2]),
            "casa": best_away[0],
        },
    }


def obtener_lineas_oddspapi(cfg: dict) -> tuple[dict[tuple[str, str], dict], dict]:
    """
    mapa: (away_norm, home_norm) -> {away: {...}, home: {...}}
    """
    global _cache, _cache_ts
    meta: dict[str, Any] = {
        "ok": False,
        "fuente": "oddspapi",
        "mensaje": "",
        "partidos": 0,
        "proveedor": "oddspapi",
    }
    api_key = cargar_api_key(cfg)
    if not api_key:
        meta["mensaje"] = "Falta ODDSPAPI_API_KEY en Render (oddspapi.io)"
        return {}, meta

    ahora = datetime.now()
    if _cache is not None and _cache_ts and ahora - _cache_ts < timedelta(minutes=CACHE_MINUTES):
        return _cache, {
            **meta,
            "ok": True,
            "partidos": len(_cache),
            "cache": True,
            "mensaje": f"{len(_cache)} partidos (cache)",
        }

    lineas_cfg = cfg.get("lineas") or {}
    sport_id = int(lineas_cfg.get("sport_id") or SPORT_ID_BASEBALL)
    tournament_id = int(lineas_cfg.get("tournament_id") or TOURNAMENT_MLB)
    books = lineas_cfg.get("bookmakers") or lineas_cfg.get("casa") or "draftkings,fanduel,betmgm,pinnacle"
    if isinstance(books, list):
        book_keys = [str(b) for b in books]
    else:
        book_keys = [b.strip() for b in str(books).split(",") if b.strip()]

    hoy = ahora.strftime("%Y-%m-%d")
    manana = (ahora + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        r_fix = requests.get(
            f"{BASE_URL}/fixtures",
            params={
                "apiKey": api_key,
                "sportId": sport_id,
                "from": hoy,
                "to": manana,
            },
            timeout=25,
        )
        if r_fix.status_code in (401, 403):
            meta["mensaje"] = "API key OddsPapi inválida o sin permiso"
            meta["http_status"] = r_fix.status_code
            return {}, meta
        r_fix.raise_for_status()
        fixtures = r_fix.json()
        if not isinstance(fixtures, list):
            fixtures = fixtures.get("fixtures") or fixtures.get("data") or []
    except requests.RequestException as e:
        meta["mensaje"] = f"Error OddsPapi fixtures: {e}"
        return {}, meta

    names_by_id: dict[str, tuple[str, str]] = {}
    for fx in fixtures or []:
        if not isinstance(fx, dict):
            continue
        tid = fx.get("tournamentId")
        tname = str(fx.get("tournamentName") or "")
        if tid not in (tournament_id, str(tournament_id)) and "MLB" not in tname.upper():
            continue
        fid = str(fx.get("fixtureId") or "")
        p1 = fx.get("participant1Name") or ""
        p2 = fx.get("participant2Name") or ""
        if fid and p1 and p2:
            names_by_id[fid] = (p1, p2)

    # OddsPapi exige EXACTAMENTE un bookmaker por request (no 'bookmakers' plural).
    # Para no quemar el free tier: 1 book principal (+ opcional 2º si el 1º viene vacío).
    casa = str(lineas_cfg.get("casa") or (book_keys[0] if book_keys else "pinnacle")).lower()
    extras = [b for b in book_keys if b.lower() != casa]
    books_a_probar = [casa] + extras[:1]

    odds_by_fid: dict[str, dict] = {}
    books_ok: list[str] = []
    ultimo_error = ""
    import time

    for bi, book in enumerate(books_a_probar):
        if bi > 0:
            time.sleep(1.1)  # rate limit del endpoint
        try:
            r_odds = requests.get(
                f"{BASE_URL}/odds-by-tournaments",
                params={
                    "apiKey": api_key,
                    "tournamentIds": str(tournament_id),
                    "oddsFormat": "decimal",
                    "bookmaker": book,
                },
                timeout=35,
            )
            if r_odds.status_code in (401, 403):
                meta["mensaje"] = "API key OddsPapi inválida (odds)"
                meta["http_status"] = r_odds.status_code
                return {}, meta
            if r_odds.status_code == 429:
                ultimo_error = "rate limit OddsPapi"
                time.sleep(1.5)
                continue
            if r_odds.status_code >= 400:
                try:
                    ultimo_error = str((r_odds.json().get("error") or {}).get("message") or r_odds.text)[:120]
                except Exception:
                    ultimo_error = r_odds.text[:120]
                continue
            odds_list = r_odds.json()
            if not isinstance(odds_list, list):
                odds_list = odds_list.get("fixtures") or odds_list.get("data") or []
            n_add = 0
            for row in odds_list or []:
                if not isinstance(row, dict):
                    continue
                fid = str(row.get("fixtureId") or "")
                if not fid:
                    continue
                prev = odds_by_fid.get(fid) or {"bookmakerOdds": {}}
                merged_books = dict(prev.get("bookmakerOdds") or {})
                merged_books.update(row.get("bookmakerOdds") or {})
                odds_by_fid[fid] = {**row, "bookmakerOdds": merged_books}
                n_add += 1
            if n_add:
                books_ok.append(book)
            # Con 1 book con datos ya basta para operar
            if books_ok and bi == 0:
                break
        except requests.RequestException as e:
            ultimo_error = str(e)[:120]
            continue

    if not odds_by_fid:
        meta["mensaje"] = ultimo_error or "OddsPapi sin cuotas MLB"
        return {}, meta

    mapa: dict[tuple[str, str], dict] = {}
    for fid, row in odds_by_fid.items():
        p1, p2 = names_by_id.get(fid, ("", ""))
        p1 = row.get("participant1Name") or p1
        p2 = row.get("participant2Name") or p2
        if not p1 or not p2:
            continue
        ml = _mejor_ml_fixture(row.get("bookmakerOdds") or {}, books_ok or book_keys)
        if not ml:
            continue
        # Fixtures OddsPapi: participant1 vs participant2; moneyline home/away = venue.
        # Empíricamente participant1 ≈ home en este feed.
        n_home = normalizar_nombre_equipo(p1)
        n_away = normalizar_nombre_equipo(p2)
        mapa[(n_away, n_home)] = {
            "away": {**ml["away"], "lado": "away", "nombre": p2},
            "home": {**ml["home"], "lado": "home", "nombre": p1},
        }

    _cache = mapa
    _cache_ts = ahora
    meta["ok"] = True
    meta["partidos"] = len(mapa)
    meta["mensaje"] = (
        f"{len(mapa)} partidos OddsPapi MLB · books {','.join(books_ok) or casa} "
        f"· fixtures {len(names_by_id)}"
    )
    meta["bookmakers"] = books_ok or [casa]
    meta["fixtures_mlb"] = len(names_by_id)
    return mapa, meta


def buscar_lineas_partido(
    mapa: dict[tuple[str, str], dict], visitante: str, home: str
) -> dict | None:
    ka, kh = normalizar_nombre_equipo(visitante), normalizar_nombre_equipo(home)
    if (ka, kh) in mapa:
        return mapa[(ka, kh)]
    if (kh, ka) in mapa:
        m = mapa[(kh, ka)]
        return {"away": m.get("home"), "home": m.get("away")}
    # Fuzzy: match si ambos nombres aparecen en alguna clave
    for (a, h), fila in mapa.items():
        if {a, h} == {ka, kh}:
            if a == ka:
                return fila
            return {"away": fila.get("home"), "home": fila.get("away")}
    return None


def aplicar_lineas_oddspapi(juegos: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    mapa, meta = obtener_lineas_oddspapi(cfg)
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
                juego["odds_away_american"] = away_l.get("american")
                juego["odds_away_decimal"] = away_l.get("decimal")
            if home_l:
                juego["odds_home_american"] = home_l.get("american")
                juego["odds_home_decimal"] = home_l.get("decimal")
            juego["lineas_fuente"] = (
                (away_l or {}).get("casa")
                or (home_l or {}).get("casa")
                or "oddspapi"
            )
    return juegos, meta
