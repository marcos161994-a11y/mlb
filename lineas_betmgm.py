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


def _sanear_api_key(raw: str | None) -> str | None:
    """Quita espacios, comillas y prefijos que suelen pegarse al copiar en Render."""
    if not raw:
        return None
    key = str(raw).strip()
    # BOM / saltos
    key = key.replace("\ufeff", "").replace("\r", "").replace("\n", "").strip()
    if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
        key = key[1:-1].strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    # Placeholder típico del docs
    if not key or key.upper() in ("YOUR_API_KEY", "APIKEY", "{APIKEY}", "XXX"):
        return None
    return key or None


def enmascarar_api_key(key: str | None) -> dict:
    """Diagnóstico seguro: largo + preview, sin exponer la key."""
    if not key:
        return {"key_presente": False, "key_len": 0, "key_preview": None}
    n = len(key)
    if n <= 8:
        preview = "*" * n
    else:
        preview = f"{key[:4]}…{key[-4:]}"
    return {
        "key_presente": True,
        "key_len": n,
        "key_preview": preview,
        "key_tiene_espacios": (" " in key),
    }


def cargar_api_key(cfg: dict) -> str | None:
    """Prioriza ODDS_API_KEY (Render). Config/archivo solo si env vacío."""
    import os

    for candidate in (
        os.environ.get("ODDS_API_KEY"),
        (cfg.get("lineas") or {}).get("api_key"),
        KEY_FILE.read_text(encoding="utf-8") if KEY_FILE.exists() else None,
    ):
        if candidate is None:
            continue
        if isinstance(candidate, str) and candidate.lstrip().startswith("#"):
            continue
        key = _sanear_api_key(candidate if isinstance(candidate, str) else str(candidate))
        if key:
            return key
    return None


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
    meta.update(enmascarar_api_key(api_key))
    if not api_key:
        meta["mensaje"] = "Falta ODDS_API_KEY en Render (the-odds-api.com)"
        meta["ayuda"] = (
            "Crea key gratis en https://the-odds-api.com → Account → API Key. "
            "En Render: Environment → ODDS_API_KEY = (pegar sin comillas) → Save → Manual Deploy."
        )
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
            err_code = None
            err_msg = None
            try:
                body = r.json()
                err_code = body.get("error_code")
                err_msg = body.get("message")
            except Exception:
                pass
            meta["http_status"] = 401
            meta["error_code"] = err_code or "INVALID_KEY"
            if err_code == "DEACTIVATED_KEY":
                meta["mensaje"] = "ODDS_API_KEY desactivada (suscripción cancelada)"
            elif err_code in ("OUT_OF_USAGE_CREDITS",) or "usage" in (err_msg or "").lower():
                meta["mensaje"] = "Sin créditos en The Odds API (cuota mensual agotada)"
            else:
                meta["mensaje"] = "API key inválida (ODDS_API_KEY)"
            meta["ayuda"] = (
                "The Odds API rechazó la key (401). "
                "1) Entra a https://the-odds-api.com y copia la API Key actual. "
                "2) Render → Environment → ODDS_API_KEY: pega SOLO la key, sin comillas ni espacios. "
                "3) Save + Manual Deploy. "
                "Prueba en el navegador: "
                "https://api.the-odds-api.com/v4/sports?apiKey=TU_KEY"
            )
            return {}, meta
        if r.status_code == 429:
            meta["http_status"] = 429
            meta["mensaje"] = "Odds API rate limit / sin créditos"
            meta["ayuda"] = "Espera el reset mensual o reduce llamadas; revisa x-requests-remaining en el dashboard."
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
        out = {"away": m.get("home"), "home": m.get("away")}
        # Total O/U no depende del lado; conservar si existe
        if isinstance(m.get("total"), dict):
            out["total"] = dict(m["total"])
        if isinstance(m.get("libros"), list):
            swapped = []
            for b in m["libros"]:
                if not isinstance(b, dict):
                    continue
                row = {
                    "casa": b.get("casa"),
                    "provider": b.get("provider"),
                    "away": b.get("home"),
                    "home": b.get("away"),
                }
                if b.get("ml_home") is not None:
                    row["ml_away"] = b.get("ml_home")
                if b.get("ml_away") is not None:
                    row["ml_home"] = b.get("ml_away")
                swapped.append(row)
            out["libros"] = swapped
        return out
    return None


def _juegos_con_cuota(juegos: list[dict]) -> int:
    return sum(
        1
        for j in juegos
        if j.get("odds_away_decimal") and j.get("odds_home_decimal")
    )


_cache_por_casa: dict[str, dict] | None = None
_cache_por_casa_ts: datetime | None = None


def obtener_mapas_por_casa(cfg: dict) -> tuple[dict[str, dict], dict]:
    """The Odds API partido a partido, una casa por mapa. No mezcla el mejor precio.

    Sin ``ODDS_API_KEY`` (ni ``lineas.api_key`` ni ``odds_api_key.txt``) no llama
    a la red: devuelve vacío y el motivo, para que la cadena siga.
    """
    global _cache_por_casa, _cache_por_casa_ts
    meta: dict[str, Any] = {
        "ok": False,
        "fuente": "odds-api",
        "mensaje": "",
        "partidos": 0,
        "requiere_key": True,
    }
    api_key = cargar_api_key(cfg)
    meta.update(enmascarar_api_key(api_key))
    if not api_key:
        meta["mensaje"] = (
            "Falta ODDS_API_KEY (variable de entorno, lineas.api_key o odds_api_key.txt)"
        )
        return {}, meta

    ahora = datetime.now()
    if (
        _cache_por_casa is not None
        and _cache_por_casa_ts
        and ahora - _cache_por_casa_ts < timedelta(minutes=CACHE_MINUTES)
    ):
        n = sum(len(m) for m in _cache_por_casa.values())
        return _cache_por_casa, {**meta, "ok": n > 0, "partidos": n, "cache": True}

    lineas_cfg = cfg.get("lineas") or {}
    casa = lineas_cfg.get("casa", "betmgm")
    books = lineas_cfg.get("bookmakers") or casa
    if isinstance(books, list):
        book_keys = [str(b).strip().lower() for b in books if str(b).strip()]
    else:
        book_keys = [b.strip().lower() for b in str(books).split(",") if b.strip()]
    if not book_keys:
        book_keys = ["draftkings"]
    params = {
        "apiKey": api_key,
        "regions": lineas_cfg.get("region", "us"),
        "markets": lineas_cfg.get("mercado", "h2h"),
        "bookmakers": ",".join(book_keys),
        "oddsFormat": "american",
    }
    try:
        r = requests.get(ODDS_URL, params=params, timeout=25)
        if r.status_code in (401, 429):
            meta["http_status"] = r.status_code
            meta["mensaje"] = (
                "ODDS_API_KEY rechazada" if r.status_code == 401 else "The Odds API sin cupo"
            )
            return {}, meta
        r.raise_for_status()
        eventos = r.json()
    except requests.RequestException as e:
        meta["mensaje"] = f"Error Odds API: {e}"[:180]
        return {}, meta

    mapas: dict[str, dict] = {book: {} for book in book_keys}
    for ev in eventos if isinstance(eventos, list) else []:
        away, home = ev.get("away_team", ""), ev.get("home_team", "")
        if not away or not home:
            continue
        ka, kh = _match_key(away, home)
        for book in book_keys:
            cuotas = _extraer_h2h_libro(ev, book)
            if not cuotas:
                continue
            fila = {}
            if ka in cuotas:
                fila["away"] = {**cuotas[ka], "lado": "away"}
            if kh in cuotas:
                fila["home"] = {**cuotas[kh], "lado": "home"}
            if len(fila) == 2:
                mapas[book][(ka, kh)] = fila
    mapas = {book: mapa for book, mapa in mapas.items() if mapa}
    _cache_por_casa = mapas
    _cache_por_casa_ts = ahora
    n = sum(len(m) for m in mapas.values())
    meta["ok"] = n > 0
    meta["partidos"] = n
    meta["bookmakers"] = list(mapas)
    meta["mensaje"] = (
        f"The Odds API: {n} momios en {', '.join(mapas) or 'ninguna casa'}"
    )
    remaining = r.headers.get("x-requests-remaining")
    if remaining:
        meta["requests_restantes"] = remaining
    return mapas, meta


def aplicar_lineas_a_juegos(juegos: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    """Cadena de momios: ESPN scoreboard, ESPN header y The Odds API."""
    from cadena_momios import aplicar_cadena_momios

    return aplicar_cadena_momios(juegos, cfg)
