"""
Factores humanos / contexto de partido que el modelo clásico no veía.

Fuentes (gratis, sin API key):
- Schedule MLB StatsAPI → descanso, back-to-back, viaje, cambio de zona
- Campos del juego → serie, day/night
- Officials hydrate → home-plate umpire (+ sesgo liviano si es conocido)

No usa "motivación" ni rumores: solo señales observables.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import requests

MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
_session = requests.Session()
_team_sched_cache: dict[str, list[dict[str, Any]]] = {}

# Zona horaria del estadio local por team_id (aprox. IANA)
TZ_EQUIPO: dict[int, str] = {
    108: "America/Los_Angeles",  # LAA
    109: "America/Phoenix",  # AZ
    110: "America/New_York",  # BAL
    111: "America/New_York",  # BOS
    112: "America/Chicago",  # CHC
    113: "America/New_York",  # CIN
    114: "America/New_York",  # CLE
    115: "America/Denver",  # COL
    116: "America/New_York",  # DET
    117: "America/Chicago",  # HOU
    118: "America/Chicago",  # KC
    119: "America/Los_Angeles",  # LAD
    120: "America/New_York",  # WSH
    121: "America/New_York",  # NYM
    133: "America/Los_Angeles",  # ATH (Sacramento)
    134: "America/New_York",  # PIT
    135: "America/Los_Angeles",  # SD
    136: "America/Los_Angeles",  # SEA
    137: "America/Los_Angeles",  # SF
    138: "America/Chicago",  # STL
    139: "America/New_York",  # TB
    140: "America/Chicago",  # TEX
    141: "America/Toronto",  # TOR
    142: "America/Chicago",  # MIN
    143: "America/New_York",  # PHI
    144: "America/New_York",  # ATL
    145: "America/Chicago",  # CWS
    146: "America/New_York",  # MIA
    147: "America/New_York",  # NYY
    158: "America/Chicago",  # MIL
}

# Offset UTC aproximado en verano (para Δ zona entre estadios)
UTC_OFFSET_VERANO: dict[str, int] = {
    "America/Los_Angeles": -7,
    "America/Phoenix": -7,
    "America/Denver": -6,
    "America/Chicago": -5,
    "America/New_York": -4,
    "America/Toronto": -4,
}

# Umpires HP con sesgo histórico público (K-heavy / hitter-friendly).
# Valor = ajuste de entorno de carreras aproximado (-1 pitcher … +1 bateo).
# Lista corta y conservadora; desconocidos = 0.
UMPIRE_SESGO: dict[str, float] = {
    "angel hernandez": 0.35,  # históricamente inconsistente / hitter-lean anecdótico
    "cb bucknor": 0.25,
    "laz diaz": 0.20,
    "ron kulpa": 0.15,
    "joe west": 0.10,
    "doug eddings": -0.15,
    "brian knight": -0.10,
    "pat hoberg": -0.25,  # alta precisión / zona firme
    "trip gibson": -0.15,
    "chris guccione": -0.10,
}


def _parse_fecha(iso_or_date: str | None) -> datetime | None:
    if not iso_or_date:
        return None
    raw = str(iso_or_date).strip()
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _tz_offset(team_id: int | None) -> int:
    if not team_id:
        return -5
    name = TZ_EQUIPO.get(int(team_id), "America/Chicago")
    return UTC_OFFSET_VERANO.get(name, -5)


def _fmt_mdy(d: datetime) -> str:
    return d.strftime("%m/%d/%Y")


def _juegos_recientes_equipo(
    team_id: int,
    fecha_juego: datetime,
    dias_atras: int = 8,
    timeout: float = 8.0,
) -> list[dict[str, Any]]:
    """Schedule del equipo en ventana previa (caché por día)."""
    key = f"{team_id}:{fecha_juego.strftime('%Y-%m-%d')}"
    if key in _team_sched_cache:
        return _team_sched_cache[key]

    inicio = fecha_juego - timedelta(days=dias_atras)
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": _fmt_mdy(inicio),
        "endDate": _fmt_mdy(fecha_juego),
        "hydrate": "team,venue",
    }
    out: list[dict[str, Any]] = []
    try:
        r = _session.get(MLB_SCHEDULE, params=params, timeout=timeout)
        r.raise_for_status()
        for date_entry in (r.json().get("dates") or []):
            for g in date_entry.get("games") or []:
                out.append(g)
    except Exception as e:
        print(f"[HUMANOS] schedule team {team_id}: {e}")
        out = []

    _team_sched_cache[key] = out
    return out


def _lado_en_juego(game: dict, team_id: int) -> str | None:
    try:
        if int(game["teams"]["away"]["team"]["id"]) == int(team_id):
            return "away"
        if int(game["teams"]["home"]["team"]["id"]) == int(team_id):
            return "home"
    except Exception:
        return None
    return None


def _perfil_equipo(
    team_id: int | None,
    fecha_juego: datetime,
    es_visitante: bool,
    home_id_hoy: int | None,
) -> dict[str, Any]:
    """Descanso, B2B, viaje y cambio de zona para un lado."""
    base = {
        "ok": False,
        "dias_descanso": 1.0,
        "back_to_back": False,
        "juegos_seguidos_visita": 0,
        "cambio_zona": 0,
        "fatiga_viaje": 0.0,
        "ultimo_rival": None,
        "ultimo_venue": None,
        "motivo": "",
    }
    if not team_id:
        return base

    games = _juegos_recientes_equipo(int(team_id), fecha_juego)
    # Excluir el juego de hoy (mismo día)
    previos = []
    for g in games:
        gd = _parse_fecha(g.get("gameDate") or g.get("officialDate"))
        if not gd:
            continue
        if gd.date() >= fecha_juego.date():
            continue
        previos.append((gd, g))
    previos.sort(key=lambda x: x[0])

    if not previos:
        base["ok"] = True
        base["motivo"] = "sin historial reciente"
        return base

    last_dt, last_g = previos[-1]
    dias = max(0, (fecha_juego.date() - last_dt.date()).days - 1)
    # days between games: if played yesterday, descanso=0 → B2B
    gap_days = (fecha_juego.date() - last_dt.date()).days
    descanso = max(0, gap_days - 1)
    b2b = gap_days <= 1

    last_side = _lado_en_juego(last_g, int(team_id))
    last_home_id = None
    try:
        last_home_id = int(last_g["teams"]["home"]["team"]["id"])
    except Exception:
        last_home_id = None

    # Cambio de zona: offset del estadio anterior vs estadio de hoy
    tz_prev = _tz_offset(last_home_id)
    tz_hoy = _tz_offset(home_id_hoy)
    cambio = abs(tz_hoy - tz_prev)

    # Road streak: cuántos juegos seguidos de visita terminando en el último
    road = 0
    for _, g in reversed(previos):
        if _lado_en_juego(g, int(team_id)) == "away":
            road += 1
        else:
            break
    if es_visitante:
        road += 1  # cuenta el de hoy

    # Fatiga 0..1
    fatiga = 0.0
    if b2b:
        fatiga += 0.35
    if es_visitante:
        fatiga += 0.15
    fatiga += min(0.35, cambio * 0.12)
    fatiga += min(0.25, max(0, road - 1) * 0.08)
    if descanso >= 2:
        fatiga = max(0.0, fatiga - 0.15)
    fatiga = round(min(1.0, fatiga), 3)

    rival = None
    try:
        if last_side == "away":
            rival = last_g["teams"]["home"]["team"].get("name")
        else:
            rival = last_g["teams"]["away"]["team"].get("name")
    except Exception:
        rival = None
    venue = (last_g.get("venue") or {}).get("name")

    partes = []
    if b2b:
        partes.append("B2B")
    partes.append(f"descanso {descanso}d")
    if cambio:
        partes.append(f"Δzona {cambio}h")
    if es_visitante and road >= 3:
        partes.append(f"road×{road}")
    if fatiga >= 0.45:
        partes.append(f"fatiga {fatiga:.2f}")

    base.update(
        {
            "ok": True,
            "dias_descanso": float(descanso),
            "back_to_back": b2b,
            "juegos_seguidos_visita": road if es_visitante else 0,
            "cambio_zona": int(cambio),
            "fatiga_viaje": fatiga,
            "ultimo_rival": rival,
            "ultimo_venue": venue,
            "motivo": " · ".join(partes) if partes else "fresco",
        }
    )
    return base


def _extraer_umpire(officials: list | None) -> dict[str, Any]:
    out = {
        "ok": False,
        "hp_nombre": None,
        "hp_id": None,
        "sesgo_runs": 0.0,
        "motivo": "sin umpire",
    }
    if not isinstance(officials, list):
        return out
    hp = None
    for row in officials:
        if not isinstance(row, dict):
            continue
        if str(row.get("officialType") or "").lower() in ("home plate", "homeplate"):
            hp = row.get("official") or {}
            break
    if not hp and officials:
        # a veces el primero es HP
        first = officials[0] if isinstance(officials[0], dict) else {}
        if str(first.get("officialType") or "").lower().find("home") >= 0:
            hp = first.get("official") or {}
    if not isinstance(hp, dict) or not hp.get("fullName"):
        return out
    nombre = str(hp.get("fullName") or "").strip()
    key = nombre.lower()
    sesgo = float(UMPIRE_SESGO.get(key, 0.0))
    motivo = f"HP {nombre}"
    if sesgo > 0.05:
        motivo += " (zona ancha / +carreras)"
    elif sesgo < -0.05:
        motivo += " (zona firme / -carreras)"
    return {
        "ok": True,
        "hp_nombre": nombre,
        "hp_id": hp.get("id"),
        "sesgo_runs": sesgo,
        "motivo": motivo,
    }


def analizar_factores_humanos(juego: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    """
    Analiza contexto humano del partido y propone ajustes de fuerza.

    Returns dict con away/home perfiles, serie, umpire, ajustes y features ML.
    """
    inicio = _parse_fecha(juego.get("inicio_juego") or juego.get("fecha"))
    if not inicio:
        inicio = datetime.now(timezone.utc)

    away_id = juego.get("away_id")
    home_id = juego.get("home_id")
    try:
        away_id_i = int(away_id) if away_id is not None else None
    except (TypeError, ValueError):
        away_id_i = None
    try:
        home_id_i = int(home_id) if home_id is not None else None
    except (TypeError, ValueError):
        home_id_i = None

    away = _perfil_equipo(away_id_i, inicio, True, home_id_i)
    home = _perfil_equipo(home_id_i, inicio, False, home_id_i)

    # Serie / day-night (pueden venir del schedule)
    try:
        serie_n = int(juego.get("series_game_number") or 0)
    except (TypeError, ValueError):
        serie_n = 0
    try:
        serie_tot = int(juego.get("games_in_series") or 0)
    except (TypeError, ValueError):
        serie_tot = 0
    day_night = str(juego.get("day_night") or "").lower()
    leverage = 0.0
    if serie_tot >= 3 and serie_n > 0:
        leverage = round(serie_n / float(serie_tot), 3)
    rubber = bool(serie_tot >= 3 and serie_n == serie_tot)

    umpire = _extraer_umpire(juego.get("officials"))

    # Ajustes de fuerza (puntos del modelo logístico, conservadores)
    ajuste_away = 0.0
    ajuste_home = 0.0
    # Fatiga penaliza al lado afectado
    ajuste_away -= away["fatiga_viaje"] * 2.2
    ajuste_home -= home["fatiga_viaje"] * 1.4  # local viaja menos
    if away["back_to_back"] and not home["back_to_back"]:
        ajuste_away -= 0.6
    if home["back_to_back"] and not away["back_to_back"]:
        ajuste_home -= 0.5
    # Descanso extra ayuda un poco
    if away["dias_descanso"] >= 2:
        ajuste_away += 0.35
    if home["dias_descanso"] >= 2:
        ajuste_home += 0.25
    # Umpire: entorno de carreras → favorece al peor pitcher implícitamente vía run env;
    # aplicamos micro-ajuste simétrico hacia el underdog ofensivo no modelado: neutro en ML pick.
    # Solo anotamos sesgo; el run_env humano va a features.
    sesgo_ump = float(umpire.get("sesgo_runs") or 0.0)

    alertas: list[str] = []
    if away["fatiga_viaje"] >= 0.5:
        alertas.append(f"Visita fatigada ({away['motivo']})")
    if home["fatiga_viaje"] >= 0.45:
        alertas.append(f"Local fatigada ({home['motivo']})")
    if rubber:
        alertas.append(f"Rubber match ({serie_n}/{serie_tot})")
    if umpire.get("ok") and abs(sesgo_ump) >= 0.15:
        alertas.append(str(umpire.get("motivo")))

    resumen_parts = []
    if away.get("motivo"):
        resumen_parts.append(f"AWAY: {away['motivo']}")
    if home.get("motivo"):
        resumen_parts.append(f"HOME: {home['motivo']}")
    if serie_n and serie_tot:
        resumen_parts.append(f"Serie {serie_n}/{serie_tot}" + (" rubber" if rubber else ""))
    if day_night:
        resumen_parts.append(day_night)
    if umpire.get("motivo"):
        resumen_parts.append(str(umpire["motivo"]))

    ok = bool(away.get("ok") or home.get("ok") or umpire.get("ok") or serie_n)

    return {
        "ok": ok,
        "away": away,
        "home": home,
        "serie": {
            "game_number": serie_n,
            "games_in_series": serie_tot,
            "leverage": leverage,
            "rubber": rubber,
            "day_night": day_night or None,
        },
        "umpire": umpire,
        "ajuste_away": round(ajuste_away, 2),
        "ajuste_home": round(ajuste_home, 2),
        "sesgo_umpire_runs": sesgo_ump,
        "alertas": alertas,
        "resumen": " | ".join(resumen_parts)[:280],
        "riesgo": bool(alertas),
        # Features listas para mezclar en ML (lado se elige después)
        "features_away": {
            "fatiga_viaje": float(away["fatiga_viaje"]),
            "dias_descanso": float(min(5.0, away["dias_descanso"])),
            "cambio_zona": float(away["cambio_zona"]),
            "leverage_serie": float(leverage),
            "umpire_runs": float(sesgo_ump),
        },
        "features_home": {
            "fatiga_viaje": float(home["fatiga_viaje"]),
            "dias_descanso": float(min(5.0, home["dias_descanso"])),
            "cambio_zona": float(home["cambio_zona"]),
            "leverage_serie": float(leverage),
            "umpire_runs": float(sesgo_ump),
        },
    }


def texto_para_ia(humanos: dict | None, max_len: int = 360) -> str:
    if not isinstance(humanos, dict) or not humanos.get("ok"):
        return "Factores humanos: sin datos."
    partes = ["Factores humanos:"]
    if humanos.get("resumen"):
        partes.append(str(humanos["resumen"]))
    for a in humanos.get("alertas") or []:
        partes.append(f"ALERTA: {a}")
    aw = humanos.get("ajuste_away")
    ah = humanos.get("ajuste_home")
    if aw or ah:
        partes.append(f"Ajuste fuerza away {aw} / home {ah}")
    txt = " ".join(partes)
    return txt[:max_len]


def aplicar_ajustes_fuerza(
    f_away: float,
    f_home: float,
    humanos: dict | None,
) -> tuple[float, float]:
    if not isinstance(humanos, dict) or not humanos.get("ok"):
        return f_away, f_home
    return (
        round(f_away + float(humanos.get("ajuste_away") or 0.0), 2),
        round(f_home + float(humanos.get("ajuste_home") or 0.0), 2),
    )
