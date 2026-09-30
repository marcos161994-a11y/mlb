"""
Cuotas MLB públicas (sin API key) vía ESPN.

Fuente principal: scoreboard por fecha (partidos correctos del día).
Respaldo: header ESPN (puede mezclar slate distinto — solo si scoreboard falla).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from lineas_betmgm import (
    american_a_decimal,
    buscar_lineas_partido,
    normalizar_nombre_equipo,
)

ESPN_HEADER = "https://site.web.api.espn.com/apis/v2/scoreboard/header"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
# El experimento y la temporada se cuentan en Puerto Rico, no en UTC.
# Render corre en UTC: a las 8pm de la isla ya es el día siguiente allá.
TZ_PARTIDO = ZoneInfo("America/Puerto_Rico")
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
DISK_CACHE_HOURS = 6

_session = requests.Session()


def invalidar_cache_espn() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = None


def _espn_disk_path() -> Path:
    d = Path(os.environ.get("DATA_DIR") or str(Path(__file__).resolve().parent))
    d.mkdir(parents=True, exist_ok=True)
    return d / "espn_scoreboard_cache.json"


def _serializar_mapa(mapa: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for (away, home), fila in mapa.items():
        away_l = fila.get("away") or {}
        home_l = fila.get("home") or {}
        tot = fila.get("total") if isinstance(fila.get("total"), dict) else {}
        rows.append(
            {
                "away": away,
                "home": home,
                "away_american": away_l.get("american"),
                "away_decimal": away_l.get("decimal"),
                "away_casa": away_l.get("casa"),
                "home_american": home_l.get("american"),
                "home_decimal": home_l.get("decimal"),
                "home_casa": home_l.get("casa"),
                "total_linea": tot.get("linea"),
                "total_over_american": tot.get("over_american"),
                "total_over_decimal": tot.get("over_decimal"),
                "total_under_american": tot.get("under_american"),
                "total_under_decimal": tot.get("under_decimal"),
                "total_casa": tot.get("casa"),
            }
        )
    return rows


def _deserializar_mapa(rows: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    mapa: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        away = str(row.get("away") or "").strip()
        home = str(row.get("home") or "").strip()
        if not away or not home:
            continue
        if not row.get("away_decimal") or not row.get("home_decimal"):
            continue
        fila: dict[str, Any] = {
            "away": {
                "american": row.get("away_american"),
                "decimal": row.get("away_decimal"),
                "casa": row.get("away_casa") or "draftkings",
                "lado": "away",
            },
            "home": {
                "american": row.get("home_american"),
                "decimal": row.get("home_decimal"),
                "casa": row.get("home_casa") or "draftkings",
                "lado": "home",
            },
        }
        if row.get("total_linea") is not None:
            fila["total"] = {
                "linea": row.get("total_linea"),
                "over_american": row.get("total_over_american"),
                "over_decimal": row.get("total_over_decimal"),
                "under_american": row.get("total_under_american"),
                "under_decimal": row.get("total_under_decimal"),
                "casa": row.get("total_casa") or "draftkings",
            }
        mapa[(away, home)] = fila
    return mapa


def _parse_total_espn(odds: dict[str, Any], casa: str) -> dict[str, Any] | None:
    """Extrae over/under del bloque odds ESPN (DraftKings)."""
    try:
        linea = odds.get("overUnder")
        if linea is None and isinstance(odds.get("total"), dict):
            linea = odds["total"].get("overUnder") or odds["total"].get("line")
        if linea is None:
            return None
        linea_f = float(linea)
    except (TypeError, ValueError):
        return None
    over_am = _ml_int(odds.get("overOdds"))
    under_am = _ml_int(odds.get("underOdds"))
    out: dict[str, Any] = {
        "linea": linea_f,
        "casa": casa,
        "fuente": "espn",
    }
    if over_am is not None:
        out["over_american"] = over_am
        out["over_decimal"] = american_a_decimal(over_am)
    if under_am is not None:
        out["under_american"] = under_am
        out["under_decimal"] = american_a_decimal(under_am)
    return out

def _guardar_disco(mapa: dict[tuple[str, str], dict[str, Any]]) -> None:
    if not mapa:
        return
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "partidos": len(mapa),
        "rows": _serializar_mapa(mapa),
    }
    try:
        _espn_disk_path().write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _cargar_disco() -> dict[tuple[str, str], dict[str, Any]] | None:
    path = _espn_disk_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        ts = datetime.fromisoformat(str(data.get("ts") or ""))
    except ValueError:
        return None
    if datetime.now() - ts > timedelta(hours=DISK_CACHE_HOURS):
        return None
    mapa = _deserializar_mapa(data.get("rows") or [])
    return mapa or None


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


def _parse_american_str(raw: Any) -> int | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return int(s.replace("+", ""))
    except ValueError:
        return None


def _ml_from_scoreboard_odds(odds: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extrae moneyline away/home del bloque odds[] del scoreboard ESPN."""
    ml = odds.get("moneyline") if isinstance(odds.get("moneyline"), dict) else {}
    away_raw = ((ml.get("away") or {}).get("close") or {}).get("odds")
    home_raw = ((ml.get("home") or {}).get("close") or {}).get("odds")
    away = _parse_american_str(away_raw) or _ml_int((odds.get("awayTeamOdds") or {}).get("moneyLine"))
    home = _parse_american_str(home_raw) or _ml_int((odds.get("homeTeamOdds") or {}).get("moneyLine"))
    return away, home


def _ml_lado(odds: dict[str, Any], lado: str) -> int | None:
    bloque = odds.get(lado) if isinstance(odds.get(lado), dict) else {}
    ml = _ml_int((bloque or {}).get("moneyLine"))
    if ml is not None:
        return ml
    key = "homeTeamOdds" if lado == "home" else "awayTeamOdds"
    team = odds.get(key) if isinstance(odds.get(key), dict) else {}
    return _ml_int((team or {}).get("moneyLine"))


def _libros_scoreboard(odds_list: Any) -> list[dict[str, Any]]:
    """Todas las casas que ESPN devuelve para el partido, no solo la primera.

    Con una sola casa no hay consenso de mercado ni cierre con el que medir CLV.
    """
    libros: list[dict[str, Any]] = []
    vistas: set[str] = set()
    for odds in odds_list or []:
        if not isinstance(odds, dict):
            continue
        ml_away, ml_home = _ml_from_scoreboard_odds(odds)
        if ml_away is None or ml_home is None:
            continue
        dec_away = american_a_decimal(ml_away)
        dec_home = american_a_decimal(ml_home)
        if not dec_away or not dec_home:
            continue
        provider = ((odds.get("provider") or {}).get("name") or "DraftKings").strip()
        casa = provider.lower().replace(" ", "") or "draftkings"
        if casa in vistas:
            continue
        vistas.add(casa)
        libros.append(
            {
                "casa": casa,
                "provider": provider,
                "away": float(dec_away),
                "home": float(dec_home),
                "ml_away": ml_away,
                "ml_home": ml_home,
                "odds": odds,
            }
        )
    return libros


def parsear_scoreboard_espn(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Scoreboard por fecha: partidos alineados con el schedule oficial de MLB."""
    mapa: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in payload.get("events") or []:
        if not isinstance(ev, dict):
            continue
        comps = ev.get("competitions") or []
        if not comps or not isinstance(comps[0], dict):
            continue
        comp = comps[0]
        away_name = home_name = ""
        for t in comp.get("competitors") or []:
            if not isinstance(t, dict):
                continue
            team = t.get("team") if isinstance(t.get("team"), dict) else {}
            nombre = str(team.get("displayName") or "").strip()
            if t.get("homeAway") == "away":
                away_name = nombre
            elif t.get("homeAway") == "home":
                home_name = nombre
        if not away_name or not home_name:
            continue
        odds_list = comp.get("odds") or []
        libros = _libros_scoreboard(odds_list)
        if not libros:
            continue
        principal = libros[0]
        odds = principal["odds"]
        casa = principal["casa"]
        ka, kh = normalizar_nombre_equipo(away_name), normalizar_nombre_equipo(home_name)
        fila: dict[str, Any] = {
            "away": {
                "american": principal["ml_away"],
                "decimal": principal["away"],
                "casa": casa,
                "lado": "away",
            },
            "home": {
                "american": principal["ml_home"],
                "decimal": principal["home"],
                "casa": casa,
                "lado": "home",
            },
            "provider": principal["provider"],
            "espn_id": ev.get("id"),
            "libros": [
                {
                    "casa": b["casa"],
                    "provider": b.get("provider"),
                    "away": b["away"],
                    "home": b["home"],
                    "ml_away": b.get("ml_away"),
                    "ml_home": b.get("ml_home"),
                }
                for b in libros
            ],
        }
        tot = _parse_total_espn(odds, casa)
        if tot:
            fila["total"] = tot
        mapa[(ka, kh)] = fila
    return mapa


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
        fila: dict[str, Any] = {
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
            "libros": [
                {
                    "casa": casa,
                    "provider": provider,
                    "away": float(american_a_decimal(ml_away)),
                    "home": float(american_a_decimal(ml_home)),
                    "ml_away": ml_away,
                    "ml_home": ml_home,
                }
            ],
        }
        tot = _parse_total_espn(odds, casa)
        if tot:
            fila["total"] = tot
        mapa[(ka, kh)] = fila
    return mapa


def _aplicar_total_a_juego(juego: dict[str, Any], lineas: dict[str, Any]) -> bool:
    """Escribe total_linea / lineas_total si el mapa trae O/U."""
    tot = lineas.get("total") if isinstance(lineas.get("total"), dict) else None
    if not tot or tot.get("linea") is None:
        return False
    try:
        linea = float(tot["linea"])
    except (TypeError, ValueError):
        return False
    juego["total_linea"] = linea
    juego["lineas_total"] = {
        "linea": linea,
        "over_american": tot.get("over_american"),
        "over_decimal": tot.get("over_decimal"),
        "under_american": tot.get("under_american"),
        "under_decimal": tot.get("under_decimal"),
        "casa": tot.get("casa") or juego.get("lineas_fuente") or "espn",
        "fuente": tot.get("fuente") or "espn",
    }
    return True


def _fechas_scoreboard_espn(ahora: datetime | None = None) -> list[str]:
    """Hoy, mañana y ayer en hora de Puerto Rico (YYYYMMDD).

    Hoy va primero. Mañana cubre el borde de medianoche. Ayer queda al final
    para los juegos de la costa oeste que siguen vivos pasada la medianoche.
    """
    ahora = ahora or datetime.now(TZ_PARTIDO)
    if ahora.tzinfo is None:
        ahora = ahora.replace(tzinfo=TZ_PARTIDO)
    hoy = ahora.astimezone(TZ_PARTIDO).date()
    dias = [hoy, hoy + timedelta(days=1), hoy - timedelta(days=1)]
    return [d.strftime("%Y%m%d") for d in dias]


def _fetch_mapa_espn(timeout: float) -> tuple[dict[tuple[str, str], dict[str, Any]], str, str | None]:
    """Scoreboard por fecha primero; header solo si no hay líneas."""
    mapa: dict[tuple[str, str], dict[str, Any]] = {}
    errores: list[str] = []
    for fecha in _fechas_scoreboard_espn():
        try:
            # Scoreboard rechaza nuestro User-Agent custom (403); sin headers funciona.
            r = requests.get(
                ESPN_SCOREBOARD,
                params={"dates": fecha},
                timeout=timeout,
            )
            r.raise_for_status()
            parcial = parsear_scoreboard_espn(r.json())
            # La serie se repite al día siguiente. Si mañana pisa a hoy, el
            # partido de esta noche se queda con la cuota de mañana.
            for clave, fila in parcial.items():
                if clave not in mapa:
                    mapa[clave] = fila
        except Exception as e:
            errores.append(f"scoreboard {fecha}: {e}"[:80])
    if mapa:
        return mapa, "scoreboard", None
    try:
        r = _session.get(
            ESPN_HEADER,
            params={"sport": "baseball", "league": "mlb"},
            headers=_HEADERS,
            timeout=timeout,
        )
        r.raise_for_status()
        header = parsear_eventos_espn(r.json())
        if header:
            return header, "header", None
    except Exception as e:
        errores.append(f"header: {e}"[:80])
    return {}, "none", " · ".join(errores)[:160] or "ESPN sin cuotas"


_cache_header: dict[tuple[str, str], dict[str, Any]] | None = None
_cache_header_ts: datetime | None = None


def obtener_mapa_header_espn(
    timeout: float = 12.0,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Header ESPN (público, sin key). Segundo paso si el scoreboard no trae el partido."""
    meta: dict[str, Any] = {
        "ok": False,
        "fuente": "espn-header",
        "mensaje": "",
        "partidos": 0,
        "requiere_key": False,
    }
    ahora = datetime.now()
    global _cache_header, _cache_header_ts
    if (
        _cache_header is not None
        and _cache_header_ts
        and ahora - _cache_header_ts < timedelta(minutes=CACHE_MINUTES)
    ):
        return _cache_header, {
            **meta,
            "ok": bool(_cache_header),
            "partidos": len(_cache_header),
            "cache": True,
            "mensaje": f"{len(_cache_header)} partidos ESPN header (cache)",
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
        meta["mensaje"] = f"ESPN header: {e}"[:200]
        return {}, meta
    _cache_header = mapa
    _cache_header_ts = ahora
    meta["ok"] = bool(mapa)
    meta["partidos"] = len(mapa)
    meta["mensaje"] = (
        f"{len(mapa)} partidos ESPN header" if mapa else "ESPN header sin moneyline"
    )
    return mapa, meta


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
        mapa, api_usada, err = _fetch_mapa_espn(timeout)
        if not mapa:
            raise RuntimeError(err or "ESPN sin moneyline")
    except Exception as e:
        disco = _cargar_disco()
        if disco:
            _cache = disco
            _cache_ts = ahora
            meta["ok"] = True
            meta["partidos"] = len(disco)
            meta["cache_disco"] = True
            meta["mensaje"] = (
                f"{len(disco)} partidos ESPN/DraftKings (cache disco · red falló)"
            )
            return disco, meta
        meta["mensaje"] = f"ESPN cuotas: {e}"[:200]
        return {}, meta

    _cache = mapa
    _cache_ts = ahora
    _guardar_disco(mapa)
    meta["ok"] = bool(mapa)
    meta["partidos"] = len(mapa)
    meta["api"] = api_usada
    meta["mensaje"] = (
        f"{len(mapa)} partidos ESPN/DraftKings ({api_usada})"
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
    """Rellena moneyline (y total O/U) en juegos."""
    del cfg
    mapa, meta = obtener_lineas_espn()
    aplicados = 0
    totales = 0
    for juego in juegos:
        lineas = buscar_lineas_partido(mapa, juego.get("visitante") or "", juego.get("home") or "")
        if not lineas:
            continue
        aplicar_ml = not (
            solo_vacios and juego.get("odds_away_decimal") and juego.get("odds_home_decimal")
        )
        if aplicar_ml:
            away_l = lineas.get("away") or {}
            home_l = lineas.get("home") or {}
            if away_l.get("decimal") and home_l.get("decimal"):
                juego["odds_away_american"] = away_l.get("american")
                juego["odds_away_decimal"] = away_l.get("decimal")
                juego["odds_home_american"] = home_l.get("american")
                juego["odds_home_decimal"] = home_l.get("decimal")
                juego["lineas_fuente"] = away_l.get("casa") or home_l.get("casa") or "espn"
                juego["lineas_betmgm"] = lineas
                libros = lineas.get("libros") if isinstance(lineas.get("libros"), list) else []
                if libros:
                    juego["lineas_libros"] = [dict(b) for b in libros]
                elif not juego.get("lineas_libros"):
                    juego["lineas_libros"] = [
                        {
                            "casa": juego["lineas_fuente"],
                            "provider": away_l.get("casa") or home_l.get("casa"),
                            "away": float(away_l["decimal"]),
                            "home": float(home_l["decimal"]),
                            "ml_away": away_l.get("american"),
                            "ml_home": home_l.get("american"),
                        }
                    ]
                aplicados += 1
        if _aplicar_total_a_juego(juego, lineas):
            totales += 1
    meta["ok"] = aplicados > 0 or totales > 0
    meta["partidos_aplicados"] = aplicados
    meta["totales_aplicados"] = totales
    if aplicados or totales:
        meta["mensaje"] = f"ESPN/DraftKings: {aplicados} ML · {totales} totales"
    return juegos, meta
