"""
Clima de estadios MLB vía Open-Meteo (gratis, sin API key).

Ajusta el entorno de carreras (calor/viento → más ofensiva; frío → pitchers)
y aporta features al ensemble ML.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_session = requests.Session()
# El día del experimento es el de Puerto Rico. Render está en UTC.
TZ_PARTIDO = ZoneInfo("America/Puerto_Rico")
_clima_cache: dict[str, dict[str, Any]] = {}
_dia_cache: dict[str, str] = {}

CACHE_PATH = Path(
    os.getenv("CLIMA_CACHE_PATH", str(Path(__file__).resolve().parent / "clima_cache.json"))
)
# Open-Meteo corta con 429 si se le pide en ráfaga. Tras un corte esperamos
# antes de volver a salir, y mientras tanto servimos lo último del día.
PAUSA_TRAS_429_SEG = 900.0
_pausa_hasta = 0.0
_cache_leido = False

# Domos / techado fijo: clima exterior no aplica
DOMOS_FIJOS: set[int] = {
    139,  # Tampa Bay Rays — Tropicana Field
    141,  # Toronto Blue Jays — Rogers Centre (cerrado casi siempre en temporada)
    146,  # Miami Marlins — LoanDepot Park
    109,  # Arizona Diamondbacks — Chase Field (techado frecuente; tratamos neutro)
}

# Coordenadas aproximadas del estadio por team_id (home)
# Fuente: ubicaciones públicas de ballparks MLB
COORDS_ESTADIO: dict[int, tuple[float, float, str]] = {
    108: (33.8003, -117.8827, "Angel Stadium"),
    109: (33.4453, -112.0667, "Chase Field"),
    110: (39.2839, -76.6217, "Camden Yards"),
    111: (42.3467, -71.0972, "Fenway Park"),
    112: (41.9484, -87.6553, "Wrigley Field"),
    113: (39.0979, -84.5082, "Great American Ball Park"),
    114: (41.4962, -81.6852, "Progressive Field"),
    115: (39.7559, -104.9942, "Coors Field"),
    116: (42.3390, -83.0485, "Comerica Park"),
    117: (29.7573, -95.3555, "Minute Maid Park"),
    118: (39.0517, -94.4803, "Kauffman Stadium"),
    119: (34.0739, -118.2400, "Dodger Stadium"),
    120: (38.8730, -77.0074, "Nationals Park"),
    121: (40.7571, -73.8458, "Citi Field"),
    133: (38.5802, -121.5133, "Sutter Health Park"),  # Athletics (Sacramento)
    134: (40.4469, -80.0057, "PNC Park"),
    135: (32.7076, -117.1570, "Petco Park"),
    136: (47.5914, -122.3325, "T-Mobile Park"),
    137: (37.7786, -122.3893, "Oracle Park"),
    138: (38.6226, -90.1928, "Busch Stadium"),
    139: (27.7683, -82.6534, "Tropicana Field"),
    140: (32.7473, -97.0817, "Globe Life Field"),
    141: (43.6414, -79.3894, "Rogers Centre"),
    142: (44.9817, -93.2776, "Target Field"),
    143: (39.9057, -75.1665, "Citizens Bank Park"),
    144: (33.8907, -84.4677, "Truist Park"),
    145: (41.8299, -87.6338, "Rate Field"),
    146: (25.7781, -80.2197, "loanDepot park"),
    147: (40.8296, -73.9262, "Yankee Stadium"),
    158: (43.0280, -87.9712, "American Family Field"),
}


def _cache_key(home_id: int, inicio_iso: str | None) -> str:
    hora = (inicio_iso or "")[:13]  # YYYY-MM-DDTHH
    return f"{home_id}:{hora}"


def _hoy() -> date:
    return datetime.now(TZ_PARTIDO).date()


def _dia_de(inicio_iso: str | None) -> str:
    dia = (inicio_iso or "")[:10]
    return dia if len(dia) == 10 else _hoy().isoformat()


def _cargar_cache() -> None:
    """El cache vive en disco porque Render reinicia el proceso varias veces al día."""
    global _cache_leido
    if _cache_leido:
        return
    _cache_leido = True
    try:
        crudo = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(crudo, dict):
        return
    vivos = {(_hoy() - timedelta(days=d)).isoformat() for d in (0, 1)}
    for clave, entrada in (crudo.get("entradas") or {}).items():
        if not isinstance(entrada, dict) or not isinstance(entrada.get("dato"), dict):
            continue
        if entrada.get("dia") not in vivos:
            continue
        _clima_cache[str(clave)] = entrada["dato"]
        _dia_cache[str(clave)] = str(entrada["dia"])


def _guardar_cache() -> None:
    entradas = {
        clave: {"dia": _dia_cache.get(clave, ""), "dato": dato}
        for clave, dato in _clima_cache.items()
    }
    tmp = CACHE_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps({"entradas": entradas}, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(CACHE_PATH)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _recordar(clave: str, dia: str, dato: dict[str, Any]) -> None:
    _clima_cache[clave] = dato
    _dia_cache[clave] = dia
    _guardar_cache()


def _respaldo_del_dia(home_id: int, dia: str) -> dict[str, Any] | None:
    """Último clima bueno de ese estadio hoy, para no quedar ciegos si la API corta."""
    prefijo = f"{home_id}:"
    mejor_clave = None
    for clave, guardado in _dia_cache.items():
        if guardado != dia or not clave.startswith(prefijo):
            continue
        if mejor_clave is None or clave > mejor_clave:
            mejor_clave = clave
    if mejor_clave is None:
        return None
    dato = dict(_clima_cache[mejor_clave])
    dato["fuente"] = "open-meteo-cache"
    return dato


def _en_pausa() -> bool:
    return time.time() < _pausa_hasta


def _activar_pausa(err: Exception) -> bool:
    """True si el error es un 429; entonces dejamos de pedir por un rato."""
    global _pausa_hasta
    codigo = getattr(getattr(err, "response", None), "status_code", None)
    if codigo != 429 and "429" not in str(err):
        return False
    _pausa_hasta = time.time() + PAUSA_TRAS_429_SEG
    return True


def _horas_utc(inicio_iso: str | None) -> str | None:
    if not inicio_iso:
        return None
    try:
        dt = datetime.fromisoformat(inicio_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:00")


def _params_base(hora_utc: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "UTC",
    }
    if hora_utc:
        params["hourly"] = "temperature_2m,relative_humidity_2m,wind_speed_10m"
        params["start_hour"] = hora_utc
        params["end_hour"] = hora_utc
    return params


def _leer_medidas(data: dict[str, Any]) -> tuple[Any, Any, Any]:
    hourly = data.get("hourly") or {}
    if hourly.get("temperature_2m"):
        return (
            hourly["temperature_2m"][0],
            (hourly.get("wind_speed_10m") or [None])[0],
            (hourly.get("relative_humidity_2m") or [None])[0],
        )
    cur = data.get("current") or {}
    return cur.get("temperature_2m"), cur.get("wind_speed_10m"), cur.get("relative_humidity_2m")


def _armar(nombre: str, temp: Any, viento: Any, humedad: Any) -> dict[str, Any] | None:
    if temp is None:
        return None
    run_env = calcular_run_env(float(temp), float(viento or 0.0))
    return {
        "ok": True,
        "fuente": "open-meteo",
        "temp_f": round(float(temp), 1),
        "viento_mph": round(float(viento or 0.0), 1),
        "humedad": int(humedad) if humedad is not None else None,
        "run_env": run_env,
        "motivo": _motivo_run_env(run_env, float(temp), float(viento or 0)),
        "estadio": nombre,
        "es_domo": False,
    }


def precargar_clima_dia(juegos: list[dict[str, Any]], timeout: float = 10.0) -> dict[str, int]:
    """Una petición por hora de inicio en vez de una por juego.

    Open-Meteo acepta varias coordenadas en la misma llamada, así que un slate
    de 15 juegos se resuelve en dos o tres peticiones.
    """
    _cargar_cache()
    resumen = {"pedidos": 0, "guardados": 0, "en_cache": 0, "pausado": 0}
    por_hora: dict[str, list[tuple[str, str, int, float, float, str]]] = {}
    vistos: set[str] = set()
    for juego in juegos or []:
        if not isinstance(juego, dict):
            continue
        home_id = juego.get("home_id")
        if not home_id or int(home_id) in DOMOS_FIJOS:
            continue
        coords = COORDS_ESTADIO.get(int(home_id))
        if not coords:
            continue
        inicio = juego.get("inicio_juego")
        clave = _cache_key(int(home_id), inicio)
        if clave in vistos:
            continue
        vistos.add(clave)
        if clave in _clima_cache:
            resumen["en_cache"] += 1
            continue
        hora = _horas_utc(inicio) or ""
        lat, lon, nombre = coords
        por_hora.setdefault(hora, []).append(
            (clave, _dia_de(inicio), int(home_id), lat, lon, nombre)
        )

    for hora, pendientes in por_hora.items():
        if _en_pausa():
            resumen["pausado"] += len(pendientes)
            continue
        params = _params_base(hora or None)
        params["latitude"] = ",".join(str(p[3]) for p in pendientes)
        params["longitude"] = ",".join(str(p[4]) for p in pendientes)
        resumen["pedidos"] += 1
        try:
            r = _session.get(OPEN_METEO_URL, params=params, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if _activar_pausa(e):
                print(f"[CLIMA] Open-Meteo 429 · pausa {int(PAUSA_TRAS_429_SEG / 60)} min")
            else:
                print(f"[CLIMA] lote falló: {str(e)[:80]}")
            continue
        bloques = data if isinstance(data, list) else [data]
        for pendiente, bloque in zip(pendientes, bloques):
            if not isinstance(bloque, dict):
                continue
            clave, dia, _home_id, _lat, _lon, nombre = pendiente
            dato = _armar(nombre, *_leer_medidas(bloque))
            if dato:
                _clima_cache[clave] = dato
                _dia_cache[clave] = dia
                resumen["guardados"] += 1
    if resumen["guardados"]:
        _guardar_cache()
    return resumen


def obtener_clima_estadio(
    home_id: int | None,
    inicio_iso: str | None = None,
    timeout: float = 6.0,
) -> dict[str, Any]:
    """
    Devuelve clima para el estadio local.
    Si falla la API o es domo → entorno neutro (ok=True, fuente=domo|fallback).
    """
    base: dict[str, Any] = {
        "ok": False,
        "fuente": "none",
        "temp_f": None,
        "viento_mph": None,
        "humedad": None,
        "run_env": 0.0,
        "motivo": "",
        "estadio": "",
        "es_domo": False,
    }
    if not home_id:
        base["motivo"] = "Sin home_id"
        return base

    if home_id in DOMOS_FIJOS:
        base.update(
            {
                "ok": True,
                "fuente": "domo",
                "es_domo": True,
                "run_env": 0.0,
                "motivo": "Estadio techado — clima neutro",
                "estadio": COORDS_ESTADIO.get(home_id, (0, 0, ""))[2],
            }
        )
        return base

    coords = COORDS_ESTADIO.get(int(home_id))
    if not coords:
        base["motivo"] = f"Sin coordenadas para team {home_id}"
        return base

    lat, lon, nombre = coords
    base["estadio"] = nombre
    _cargar_cache()
    ck = _cache_key(int(home_id), inicio_iso)
    dia = _dia_de(inicio_iso)
    if ck in _clima_cache:
        return dict(_clima_cache[ck])

    def _sin_dato(motivo: str) -> dict[str, Any]:
        respaldo = _respaldo_del_dia(int(home_id), dia)
        if respaldo:
            return respaldo
        base["motivo"] = motivo
        base["sin_dato"] = True
        return base

    if _en_pausa():
        return _sin_dato("Open-Meteo en pausa tras 429")

    try:
        params = _params_base(_horas_utc(inicio_iso))
        params["latitude"] = lat
        params["longitude"] = lon
        r = _session.get(OPEN_METEO_URL, params=params, timeout=timeout)
        r.raise_for_status()
        out = _armar(nombre, *_leer_medidas(r.json()))
        if out is None:
            return _sin_dato("Open-Meteo sin temperatura")
        _recordar(ck, dia, out)
        return dict(out)
    except requests.Timeout:
        return _sin_dato("Timeout Open-Meteo")
    except Exception as e:
        if _activar_pausa(e):
            print(f"[CLIMA] Open-Meteo 429 · pausa {int(PAUSA_TRAS_429_SEG / 60)} min")
        return _sin_dato(str(e)[:120])


def calcular_run_env(temp_f: float, viento_mph: float) -> float:
    """
    Entorno de carreras aproximado en [-2.0, +2.0].
    Positivo = más ofensivo / más HR; negativo = favorece pitchers.
    """
    adj = 0.0
    if temp_f < 45:
        adj -= 1.8
    elif temp_f < 55:
        adj -= 1.1
    elif temp_f < 65:
        adj -= 0.4
    elif temp_f > 95:
        adj += 1.4
    elif temp_f > 85:
        adj += 0.8
    elif temp_f > 78:
        adj += 0.35

    if viento_mph >= 18:
        adj += 0.7
    elif viento_mph >= 12:
        adj += 0.35
    elif viento_mph >= 8:
        adj += 0.1

    return round(max(-2.0, min(2.0, adj)), 2)


def _motivo_run_env(run_env: float, temp_f: float, viento_mph: float) -> str:
    partes = [f"{temp_f:.0f}°F", f"viento {viento_mph:.0f} mph"]
    if run_env <= -1.0:
        partes.append("frío → favorece pitchers")
    elif run_env >= 1.0:
        partes.append("calor/viento → favorece bateo")
    else:
        partes.append("clima neutro")
    return " · ".join(partes)


def aplicar_clima_a_fuerzas(
    f_away: float,
    f_home: float,
    of_away: float,
    of_home: float,
    p_def_away: float,
    p_def_home: float,
    park_factor: float,
    clima: dict[str, Any] | None,
) -> tuple[float, float, float]:
    """
    Inclina ligeramente las fuerzas según clima.
    Frío → mejor pitcher; calor → mejor ofensiva; parques hitter + viento → más caos.
    Returns: (f_away, f_home, ajuste_neto_usado)
    """
    if not clima or not clima.get("ok"):
        return f_away, f_home, 0.0

    re = float(clima.get("run_env") or 0.0)
    if abs(re) < 0.15:
        return f_away, f_home, 0.0

    fa, fh = float(f_away), float(f_home)

    if re < 0:
        # Pitcher weather: empuja hacia el mejor starter
        boost = abs(re) * 0.75
        if p_def_away > p_def_home + 0.3:
            fa += boost
        elif p_def_home > p_def_away + 0.3:
            fh += boost
        else:
            # Empate de pitchers: ligera ventaja local (mejor conocimiento)
            fh += boost * 0.25
    else:
        # Hitter weather: empuja hacia mejor ofensiva
        boost = re * 0.65
        if of_away > of_home + 0.5:
            fa += boost
        elif of_home > of_away + 0.5:
            fh += boost
        else:
            fh += boost * 0.2  # leve home bias en slugfests

        # Parques muy ofensivos + viento/calor: más varianza → acerca al underdog
        if park_factor >= 1.12 and re >= 0.8:
            if fa < fh:
                fa += 0.45
            elif fh < fa:
                fh += 0.45

    return round(fa, 2), round(fh, 2), round(re, 2)
