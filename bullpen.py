"""Fatiga real del bullpen.

Antes se estimaba con el porcentaje de derrotas del equipo, así que el valor
salía casi idéntico para todos (0.4) y nunca llegaba a un umbral útil.
Aquí se cuentan las entradas que tiraron los relevistas en los últimos días,
que es lo que de verdad deja al bullpen sin brazos.

El trabajo se reparte: cada ciclo procesa unos pocos boxscores y guarda lo
que ya leyó en disco, para no colgar una petición del panel en Render.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

API = "https://statsapi.mlb.com/api/v1"
_session = requests.Session()

CACHE_PATH = Path(
    os.getenv("BULLPEN_CACHE_PATH", str(Path(__file__).resolve().parent / "bullpen_cache.json"))
)
DIAS_MIRADOS = 3
# Entradas de relevo acumuladas en la ventana: por debajo el bullpen está
# entero, por encima viene exprimido. Con los 28 equipos medidos en vivo, la
# mediana de la liga cae en 0.54 y el umbral 0.7 de la ficha marca el tercio
# más cargado (unas 11.3 entradas de relevo en tres días).
IP_DESCANSADO = 5.0
IP_EXPRIMIDO = 14.0
PRESUPUESTO_SEG = 20.0
TIMEOUT = 8.0

_estado: dict[str, Any] = {"dia": "", "juegos": {}, "pendientes": None}
_cache_leido = False


def _hoy() -> str:
    return date.today().isoformat()


def _cargar() -> None:
    global _cache_leido
    if _cache_leido:
        return
    _cache_leido = True
    try:
        crudo = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(crudo, dict) and crudo.get("dia") == _hoy():
        _estado["dia"] = crudo["dia"]
        _estado["juegos"] = crudo.get("juegos") or {}


def _guardar() -> None:
    tmp = CACHE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps({"dia": _estado["dia"], "juegos": _estado["juegos"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(CACHE_PATH)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def innings_a_float(valor: Any) -> float:
    """MLB escribe las entradas como 5.1 o 5.2, que son 5 y un tercio o 5 y dos tercios."""
    try:
        texto = str(valor).strip()
        if not texto:
            return 0.0
        enteras, _, resto = texto.partition(".")
        out = float(int(enteras or 0))
        if resto.startswith("1"):
            out += 1.0 / 3.0
        elif resto.startswith("2"):
            out += 2.0 / 3.0
        return out
    except (TypeError, ValueError):
        return 0.0


def _juegos_terminados(hasta: date) -> list[int]:
    desde = hasta - timedelta(days=DIAS_MIRADOS)
    r = _session.get(
        f"{API}/schedule",
        params={
            "sportId": 1,
            "startDate": desde.isoformat(),
            "endDate": (hasta - timedelta(days=1)).isoformat(),
            "fields": "dates,games,gamePk,status,codedGameState",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[int] = []
    for dia in (r.json().get("dates") or []):
        for juego in dia.get("games") or []:
            if str((juego.get("status") or {}).get("codedGameState")) != "F":
                continue
            if juego.get("gamePk"):
                out.append(int(juego["gamePk"]))
    return out


def _relevo_del_juego(game_pk: int) -> dict[str, float]:
    """Entradas de relevo por equipo: todo lo que no tiró el abridor."""
    r = _session.get(
        f"{API}/game/{game_pk}/boxscore",
        params={
            "fields": "teams,away,home,team,id,pitchers,players,stats,pitching,inningsPitched"
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    equipos = (r.json().get("teams") or {})
    out: dict[str, float] = {}
    for lado in ("away", "home"):
        bloque = equipos.get(lado) or {}
        team_id = ((bloque.get("team") or {}).get("id"))
        lanzadores = bloque.get("pitchers") or []
        if not team_id or len(lanzadores) < 2:
            continue
        jugadores = bloque.get("players") or {}
        total = 0.0
        for pid in lanzadores[1:]:
            ficha = jugadores.get(f"ID{pid}") or {}
            pitching = ((ficha.get("stats") or {}).get("pitching") or {})
            total += innings_a_float(pitching.get("inningsPitched"))
        out[str(team_id)] = round(total, 2)
    return out


def refrescar(hasta: date | None = None, presupuesto_seg: float = PRESUPUESTO_SEG) -> dict[str, Any]:
    """Lee los boxscores que falten, sin pasarse del tiempo permitido."""
    _cargar()
    hasta = hasta or date.today()
    if _estado["dia"] != _hoy():
        _estado["dia"] = _hoy()
        _estado["juegos"] = {}
        _estado["pendientes"] = None
    if _estado["pendientes"] is None:
        try:
            _estado["pendientes"] = _juegos_terminados(hasta)
        except Exception as e:
            print(f"[BULLPEN] calendario falló: {str(e)[:80]}")
            _estado["pendientes"] = None
            return {"ok": False, "leidos": 0, "faltan": None}
    inicio = time.time()
    leidos = 0
    for game_pk in list(_estado["pendientes"] or []):
        if str(game_pk) in _estado["juegos"]:
            continue
        if time.time() - inicio > presupuesto_seg:
            break
        try:
            _estado["juegos"][str(game_pk)] = _relevo_del_juego(int(game_pk))
            leidos += 1
        except Exception as e:
            print(f"[BULLPEN] boxscore {game_pk} falló: {str(e)[:60]}")
            _estado["juegos"][str(game_pk)] = {}
    if leidos:
        _guardar()
    faltan = sum(
        1 for g in (_estado["pendientes"] or []) if str(g) not in _estado["juegos"]
    )
    return {"ok": True, "leidos": leidos, "faltan": faltan}


def innings_relevo(team_id: int) -> float | None:
    _cargar()
    if _estado["dia"] != _hoy() or not _estado["juegos"]:
        return None
    clave = str(int(team_id))
    total = 0.0
    visto = False
    for juego in _estado["juegos"].values():
        if not isinstance(juego, dict) or clave not in juego:
            continue
        visto = True
        total += float(juego[clave] or 0.0)
    return round(total, 2) if visto else None


def fatiga_bullpen(team_id: int) -> float | None:
    """0 descansado, 1 exprimido. None si todavía no hay datos del día."""
    ip = innings_relevo(team_id)
    if ip is None:
        return None
    escala = (ip - IP_DESCANSADO) / (IP_EXPRIMIDO - IP_DESCANSADO)
    return round(max(0.0, min(1.0, escala)), 2)


def resumen() -> dict[str, Any]:
    _cargar()
    equipos = {}
    for juego in (_estado["juegos"] or {}).values():
        if not isinstance(juego, dict):
            continue
        for team_id in juego:
            equipos[team_id] = fatiga_bullpen(int(team_id))
    return {
        "ok": bool(equipos),
        "dia": _estado["dia"],
        "juegos_leidos": len(_estado["juegos"] or {}),
        "equipos": len(equipos),
        "ventana_dias": DIAS_MIRADOS,
        "actualizado": datetime.now().isoformat(timespec="seconds"),
    }
