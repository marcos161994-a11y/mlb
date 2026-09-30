"""Moneyline MLB público de Action Network, sin API key.

Verificado contra ``GET /web/v1/scoreboard/mlb`` (septiembre 2026):

- URL: ``https://api.actionnetwork.com/web/v1/scoreboard/mlb``
- Query: ``period=game``, ``date=YYYYMMDD`` y ``bookIds`` (la respuesta
  puede traer otras casas; el filtro del query no es estricto).
- Cada partido trae ``away_team_id``, ``home_team_id``, ``start_time``,
  ``status`` / ``real_status`` y ``teams[].full_name``.
- El moneyline del periodo ``game`` está en ``odds[].ml_away`` /
  ``odds[].ml_home``, con ``book_id`` e ``inserted``.
- Casas que salen en ese payload: 68 DraftKings, 69 FanDuel, 75 BetMGM,
  123 Caesars, 71 BetRivers, 79 bet365. 15 es el consenso y 30 la línea
  de apertura: no son una casa y no se usan. Pinnacle es el id 3; si no
  viene, se sigue con la siguiente.

Hace falta un User-Agent de navegador. 403, 429 y un cuerpo bloqueado
no tiran la cadena. El resultado se cachea unos minutos en memoria
(no en disco: un cache de disco no puede autorizar una apuesta).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

from lineas_betmgm import (
    anexar_linea,
    fecha_iso,
    fecha_slate_desde_instante,
    normalizar_nombre_equipo,
)

SCOREBOARD_URL = "https://api.actionnetwork.com/web/v1/scoreboard/mlb"
# Pinnacle, DraftKings, FanDuel, BetMGM y el resto que el tablero suele traer.
BOOK_IDS = "3,68,69,75,248,123,71,79"
TIMEOUT_SEG = 8.0
CACHE_MINUTES = 3
TZ_PARTIDO = ZoneInfo("America/Puerto_Rico")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.actionnetwork.com/",
    "Origin": "https://www.actionnetwork.com",
}

# id de Action Network → (clave de lineas.bookmakers, nombre real).
_CASAS: dict[int, tuple[str, str]] = {
    3: ("pinnacle", "Pinnacle"),
    68: ("draftkings", "DraftKings"),
    69: ("fanduel", "FanDuel"),
    75: ("betmgm", "BetMGM"),
    248: ("betmgm", "BetMGM"),
    123: ("caesars", "Caesars"),
    71: ("betrivers", "BetRivers"),
    79: ("bet365", "bet365"),
}
# Consenso y opening line. No son una casa y no se inventa un precio con ellas.
_NO_CASA = frozenset({15, 30})
_PRE = frozenset({"scheduled", "pregame", "created", "warmup", "delayed", "timetbd", "preview"})
_EN_VIVO = frozenset({"inprogress", "live", "halftime", "in"})
_CERRADO = frozenset(
    {"complete", "closed", "final", "postponed", "cancelled", "canceled", "suspended", "post"}
)

_cache: dict[str, tuple[datetime, dict, dict]] = {}


def reset_cache_action_network() -> None:
    """Limpia el cache de este proceso. Los tests lo usan entre casos."""
    _cache.clear()


def _compactar_estado(valor: Any) -> str:
    return str(valor or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def _estado_partido(game: dict) -> str:
    status = _compactar_estado(game.get("status"))
    real = _compactar_estado(game.get("real_status"))
    if status in _EN_VIVO or real in _EN_VIVO:
        return "in"
    if status in _CERRADO or real in _CERRADO:
        return "post"
    if status in _PRE or real in _PRE or not status:
        return "pre"
    return "post"


def _ya_empezo(inicio: Any, estado: str) -> bool:
    if estado != "pre":
        return True
    if not inicio:
        return False
    texto = str(inicio).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc) - timedelta(seconds=60)


def _nombre_equipo(team: dict) -> str:
    full = str(team.get("full_name") or "").strip()
    if full:
        return full
    location = str(team.get("location") or "").strip()
    display = str(team.get("display_name") or team.get("short_name") or "").strip()
    return " ".join(p for p in (location, display) if p).strip()


def _casa_de(book_id: int) -> tuple[str, str] | None:
    if book_id in _NO_CASA:
        return None
    if book_id in _CASAS:
        return _CASAS[book_id]
    return (f"book{book_id}", f"Book {book_id}")


def parsear_scoreboard_action(payload: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Tablero → mapa de la cadena. Solo moneyline de una casa, con su hora."""
    mapa: dict[tuple[str, str], dict[str, Any]] = {}
    games = payload.get("games") if isinstance(payload, dict) else None
    if not isinstance(games, list):
        return mapa
    for game in games:
        if not isinstance(game, dict):
            continue
        teams = game.get("teams") if isinstance(game.get("teams"), list) else []
        por_id = {
            str(t.get("id")): t
            for t in teams
            if isinstance(t, dict) and t.get("id") not in (None, "")
        }
        away_team = por_id.get(str(game.get("away_team_id")))
        home_team = por_id.get(str(game.get("home_team_id")))
        if not isinstance(away_team, dict) or not isinstance(home_team, dict):
            continue
        away = _nombre_equipo(away_team)
        home = _nombre_equipo(home_team)
        if not away or not home:
            continue
        inicio = game.get("start_time")
        fecha = fecha_slate_desde_instante(inicio)
        if not fecha or not inicio:
            continue
        estado = _estado_partido(game)
        en_vivo = _ya_empezo(inicio, estado)
        estado_cuota = "in" if en_vivo else estado
        libros: list[dict[str, Any]] = []
        vistos: set[str] = set()
        for odd in game.get("odds") or []:
            if not isinstance(odd, dict):
                continue
            tipo = str(odd.get("type") or "game").strip().lower()
            if tipo not in ("game", "moneyline"):
                continue
            try:
                book_id = int(odd.get("book_id"))
            except (TypeError, ValueError):
                continue
            casa = _casa_de(book_id)
            if casa is None or casa[0] in vistos:
                continue
            if odd.get("ml_away") is None or odd.get("ml_home") is None:
                continue
            # Sin hora de la línea no se puede saber si tiene ≤15 min.
            # No se sella con "ahora": eso inventaría frescura.
            if not str(odd.get("inserted") or "").strip():
                continue
            vistos.add(casa[0])
            libros.append(
                {
                    "casa": casa[0],
                    "provider": casa[1],
                    "ml_away": odd.get("ml_away"),
                    "ml_home": odd.get("ml_home"),
                    "fetched_at": odd.get("inserted"),
                    "stale": False,
                    "en_vivo": en_vivo,
                    "estado_cuota": estado_cuota,
                    "fecha": fecha,
                    "inicio": inicio,
                }
            )
        if not libros:
            continue
        fila = {
            "fecha": fecha,
            "inicio": inicio,
            "an_id": game.get("id"),
            "estado_cuota": estado_cuota,
            "en_vivo": en_vivo,
            "stale": False,
            "libros": libros,
        }
        anexar_linea(mapa, (normalizar_nombre_equipo(away), normalizar_nombre_equipo(home)), fila)
    return mapa


def _fechas_de(juegos: list[dict] | None) -> list[str]:
    fechas: list[str] = []
    for juego in juegos or []:
        if not isinstance(juego, dict):
            continue
        fecha = fecha_iso(juego.get("fecha"))
        if not fecha:
            fecha = fecha_slate_desde_instante(juego.get("inicio_juego") or juego.get("commence_time"))
        if fecha and fecha not in fechas:
            fechas.append(fecha)
    if not fechas:
        fechas.append(datetime.now(TZ_PARTIDO).date().isoformat())
    return fechas


def _mensaje_http(status: int) -> str:
    if status == 403:
        return "Action Network 403"
    if status == 429:
        return "Action Network 429"
    if status in (401, 451):
        return f"Action Network bloqueó la respuesta ({status})"
    return f"Action Network HTTP {status}"


def _guardar_cache(clave: str, mapa: dict, meta: dict) -> None:
    _cache[clave] = (datetime.now(timezone.utc), mapa, dict(meta))


def _leer_cache(clave: str) -> tuple[dict, dict] | None:
    guardado = _cache.get(clave)
    if not guardado:
        return None
    cuando, mapa, meta = guardado
    if datetime.now(timezone.utc) - cuando >= timedelta(minutes=CACHE_MINUTES):
        return None
    return mapa, {**meta, "cache": True}


def _fetch_fecha(fecha: str) -> tuple[dict, dict]:
    clave = f"{fecha}|{BOOK_IDS}"
    cacheado = _leer_cache(clave)
    if cacheado is not None:
        return cacheado
    meta: dict[str, Any] = {
        "ok": False,
        "fuente": "action_network",
        "mensaje": "",
        "partidos": 0,
        "fecha": fecha,
        "requiere_key": False,
    }
    params = {"period": "game", "bookIds": BOOK_IDS, "date": fecha.replace("-", "")}
    try:
        r = requests.get(SCOREBOARD_URL, params=params, headers=_HEADERS, timeout=TIMEOUT_SEG)
    except requests.Timeout:
        meta["mensaje"] = "Action Network: timeout"
        _guardar_cache(clave, {}, meta)
        print(f"[MOMIO] action_network: {meta['mensaje']}")
        return {}, meta
    except requests.RequestException as e:
        meta["mensaje"] = f"Action Network: {e}"[:180]
        _guardar_cache(clave, {}, meta)
        print(f"[MOMIO] action_network: {meta['mensaje']}")
        return {}, meta
    if r.status_code in (401, 403, 429, 451) or r.status_code >= 400:
        meta["http_status"] = r.status_code
        meta["mensaje"] = _mensaje_http(r.status_code)
        _guardar_cache(clave, {}, meta)
        print(f"[MOMIO] action_network: {meta['mensaje']}")
        return {}, meta
    try:
        payload = r.json()
    except ValueError:
        meta["mensaje"] = "Action Network bloqueó la respuesta"
        _guardar_cache(clave, {}, meta)
        print(f"[MOMIO] action_network: {meta['mensaje']}")
        return {}, meta
    if not isinstance(payload, dict) or "games" not in payload:
        meta["mensaje"] = "Action Network sin scoreboard"
        _guardar_cache(clave, {}, meta)
        print(f"[MOMIO] action_network: {meta['mensaje']}")
        return {}, meta
    mapa = parsear_scoreboard_action(payload)
    if mapa:
        from cadena_momios import anotar_fetch_ok

        anotar_fetch_ok()
    meta["ok"] = bool(mapa)
    meta["partidos"] = len(mapa)
    meta["mensaje"] = (
        f"Action Network: {len(mapa)} partidos"
        if mapa
        else "Action Network sin moneyline de casa"
    )
    _guardar_cache(clave, mapa, meta)
    return mapa, meta


def obtener_mapa_action_network(
    juegos: list[dict] | None = None,
    cfg: dict | None = None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Un mapa por fecha de los partidos que todavía no tienen casa.

    ``cfg`` no lleva key: el endpoint es público. Se acepta para la misma
    firma que el resto de la cadena.
    """
    del cfg
    total: dict[tuple[str, str], dict[str, Any]] = {}
    mensajes: list[str] = []
    estado: int | None = None
    alguna_ok = False
    for fecha in _fechas_de(juegos):
        mapa, meta = _fetch_fecha(fecha)
        alguna_ok = alguna_ok or bool(meta.get("ok"))
        if meta.get("mensaje"):
            mensajes.append(str(meta["mensaje"]))
        if meta.get("http_status"):
            estado = int(meta["http_status"])
        for clave, fila in mapa.items():
            anexar_linea(total, clave, fila)
    out = {
        "ok": alguna_ok and bool(total),
        "fuente": "action_network",
        "mensaje": "; ".join(mensajes)[:180] or "Action Network sin partidos",
        "partidos": len(total),
        "requiere_key": False,
    }
    if estado is not None:
        out["http_status"] = estado
    return total, out
