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
CACHE_MINUTES = 1  # Actualizar cada minuto para cuotas en vivo

# MLB statsapi -> nombres típicos en The Odds API / BetMGM
ALIASES: dict[str, str] = {
    "athletics": "athletics",
    "oakland athletics": "athletics",
    "oakland": "athletics",
    "as": "athletics",
    "arizona diamondbacks": "arizona diamondbacks",
    "arizona dbacks": "arizona diamondbacks",
    "dbacks": "arizona diamondbacks",
    "chicago cubs": "chicago cubs",
    "chicago white sox": "chicago white sox",
    "los angeles angels": "los angeles angels",
    "la angels": "los angeles angels",
    "anaheim angels": "los angeles angels",
    "los angeles dodgers": "los angeles dodgers",
    "la dodgers": "los angeles dodgers",
    "new york mets": "new york mets",
    "ny mets": "new york mets",
    "new york yankees": "new york yankees",
    "ny yankees": "new york yankees",
    "tampa bay rays": "tampa bay rays",
    "tampa bay": "tampa bay rays",
    "st louis cardinals": "st louis cardinals",
    "saint louis cardinals": "st louis cardinals",
    "stl cardinals": "st louis cardinals",
    "san francisco giants": "san francisco giants",
    "sf giants": "san francisco giants",
    "washington nationals": "washington nationals",
    "boston red sox": "boston red sox",
    "cleveland guardians": "cleveland guardians",
    "cleveland indians": "cleveland guardians",
    "kansas city royals": "kansas city royals",
    "miami marlins": "miami marlins",
    "florida marlins": "miami marlins",
    "san diego padres": "san diego padres",
    "seattle mariners": "seattle mariners",
    "texas rangers": "texas rangers",
    "toronto blue jays": "toronto blue jays",
    "minnesota twins": "minnesota twins",
    "milwaukee brewers": "milwaukee brewers",
    "houston astros": "houston astros",
    "detroit tigers": "detroit tigers",
    "cincinnati reds": "cincinnati reds",
    "pittsburgh pirates": "pittsburgh pirates",
    "philadelphia phillies": "philadelphia phillies",
    "atlanta braves": "atlanta braves",
    "baltimore orioles": "baltimore orioles",
    "colorado rockies": "colorado rockies",
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


def _extraer_h2h_libro(evento: dict, book_key: str) -> dict | None:
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
                            "casa": book_key,
                        }
                    return out
    return None


def _mejor_h2h(evento: dict, book_keys: list[str]) -> dict | None:
    """Toma la mejor cuota decimal por equipo entre varios books."""
    mejor: dict[str, dict] = {}
    usados = []
    for book in book_keys:
        cuotas = _extraer_h2h_libro(evento, book)
        if not cuotas:
            continue
        usados.append(book)
        for equipo, data in cuotas.items():
            prev = mejor.get(equipo)
            if not prev or float(data["decimal"]) > float(prev["decimal"]):
                mejor[equipo] = data
    if len(mejor) < 2:
        return None
    mejor["_libros"] = usados  # type: ignore[assignment]
    return mejor


def obtener_lineas_betmgm(cfg: dict) -> tuple[dict[tuple[str, str], dict], dict]:
    """
    Devuelve (mapa_partidos, meta).
    mapa: (away_norm, home_norm) -> {away: {...}, home: {...}}
    """
    global _cache, _cache_ts
    meta = {"ok": False, "fuente": "odds-api", "mensaje": "", "partidos": 0}

    api_key = cargar_api_key(cfg)
    if not api_key:
        meta["mensaje"] = "Falta ODDS_API_KEY en Render (o odds_api_key.txt)"
        return {}, meta

    ahora = datetime.now()
    if _cache and _cache_ts and ahora - _cache_ts < timedelta(minutes=CACHE_MINUTES):
        return _cache, {**meta, "ok": True, "partidos": len(_cache), "cache": True}

    lineas_cfg = cfg.get("lineas", {})
    casa = lineas_cfg.get("casa", "betmgm")
    # Varios books: más cobertura si BetMGM no lista un juego
    books = lineas_cfg.get("bookmakers") or casa
    if isinstance(books, list):
        book_keys = [str(b) for b in books]
        books_param = ",".join(book_keys)
    else:
        book_keys = [b.strip() for b in str(books).split(",") if b.strip()]
        books_param = ",".join(book_keys)

    params = {
        "apiKey": api_key,
        "regions": lineas_cfg.get("region", "us"),
        "markets": lineas_cfg.get("mercado", "h2h"),
        "bookmakers": books_param,
        "oddsFormat": "american",
    }
    try:
        r = requests.get(ODDS_URL, params=params, timeout=25)
        if r.status_code == 401:
            meta["mensaje"] = "API key inválida (ODDS_API_KEY)"
            return {}, meta
        r.raise_for_status()
        eventos = r.json()
    except requests.RequestException as e:
        meta["mensaje"] = f"Error Odds API: {e}"
        return {}, meta

    mapa: dict[tuple[str, str], dict] = {}
    for ev in eventos:
        away, home = ev.get("away_team", ""), ev.get("home_team", "")
        cuotas = _mejor_h2h(ev, book_keys)
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
    meta["mensaje"] = f"{len(mapa)} partidos con cuotas ({books_param})"
    meta["bookmakers"] = book_keys
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


def _juegos_con_cuota(juegos: list[dict]) -> int:
    return sum(
        1
        for j in juegos
        if j.get("odds_away_decimal") and j.get("odds_home_decimal")
    )


def aplicar_lineas_a_juegos(juegos: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    """OddsPapi / The Odds API, y si faltan líneas: ESPN (DraftKings, sin key)."""
    lineas_cfg = cfg.get("lineas") or {}
    proveedor = str(lineas_cfg.get("proveedor") or "oddspapi").lower()
    usar_espn = bool(lineas_cfg.get("fallback_internet", True))

    if proveedor in ("espn", "espn-draftkings", "internet"):
        from lineas_espn import aplicar_lineas_espn

        return aplicar_lineas_espn(juegos, cfg, solo_vacios=False)

    if proveedor in ("oddspapi", "odds-papi", "odds_papi"):
        from lineas_oddspapi import aplicar_lineas_oddspapi, circuito_abierto, estado_circuito

        if circuito_abierto():
            st = estado_circuito()
            meta = {
                "ok": False,
                "fuente": "oddspapi",
                "circuito": True,
                "circuito_hasta": st.get("hasta"),
                "circuito_hasta_hora": st.get("hasta_hora"),
                "http_status": st.get("http_status"),
                "mensaje": st.get("mensaje") or "OddsPapi en pausa automática",
                "partidos": 0,
            }
        else:
            juegos, meta = aplicar_lineas_oddspapi(juegos, cfg)
    else:
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
                juego["lineas_fuente"] = away_l.get("casa") or home_l.get("casa") or "odds-api"

    n_ok = _juegos_con_cuota(juegos)
    if usar_espn and n_ok < len(juegos):
        try:
            from lineas_espn import aplicar_lineas_espn

            juegos, meta_e = aplicar_lineas_espn(juegos, cfg, solo_vacios=True)
        except Exception as e:
            meta_e = {"ok": False, "mensaje": f"ESPN fallback: {e}"[:160]}
        n2 = _juegos_con_cuota(juegos)
        aviso_papi = str((meta or {}).get("mensaje") or "OddsPapi no disponible")
        # Nunca reenviar la URL/key de OddsPapi al panel.
        try:
            from lineas_oddspapi import redactar_secretos

            aviso_papi = redactar_secretos(aviso_papi)
        except Exception:
            aviso_papi = "OddsPapi no disponible"
        if len(aviso_papi) > 80:
            aviso_papi = "OddsPapi no disponible"
        if meta_e.get("ok") or n2 > n_ok:
            meta = {
                **(meta or {}),
                "ok": n2 > 0,
                "fallback_espn": True,
                "espn_partidos": meta_e.get("partidos_aplicados") or (n2 - n_ok),
                "partidos": n2,
                "fuente": "espn" if n_ok == 0 else (meta or {}).get("fuente") or "mixto",
                "mensaje": (
                    (
                        f"OddsPapi en pausa automática hasta {(meta or {}).get('circuito_hasta_hora') or 'luego'} · "
                        if (meta or {}).get("circuito")
                        else ""
                    )
                    + f"ESPN/DraftKings · {n2} partidos con cuota real"
                    + (
                        f" ({aviso_papi})"
                        if n_ok == 0 and not (meta or {}).get("circuito")
                        else ""
                    )
                ),
            }
        elif n_ok == 0:
            meta = {
                **(meta or {}),
                "ok": False,
                "fallback_espn": True,
                "mensaje": (
                    f"{aviso_papi} · {meta_e.get('mensaje') or 'ESPN sin cuotas'}"
                )[:200],
            }
    return juegos, meta
