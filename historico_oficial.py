"""
Historial oficial MLB (StatsAPI, sin API key).

Señales que la mente necesita para no decidir a ciegas:
- L10 de cada equipo (W-L últimos 10, desde standings)
- Pitcher vs rival (gameLog filtrado + OPS en contra vía vsTeamTotal)

No inventa motivación: solo resultados oficiales.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

MLB_STANDINGS = "https://statsapi.mlb.com/api/v1/standings"
MLB_PEOPLE = "https://statsapi.mlb.com/api/v1/people"

_session = requests.Session()
_l10_cache: dict[int, dict[int, dict[str, Any]]] = {}
_pvr_cache: dict[str, dict[str, Any]] = {}


def _ip_a_float(ip_raw: Any) -> float:
    if ip_raw is None:
        return 0.0
    try:
        s = str(ip_raw)
        if "." in s:
            enteros, dec = s.split(".", 1)
            outs = int(dec[0]) if dec else 0
            return float(enteros) + outs / 3.0
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _safe_float(v: Any, default: float | None = None) -> float | None:
    if v is None or v == "" or v in (".---", "-.--", "-.--"):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def cargar_l10(season: int, timeout: float = 10.0) -> dict[int, dict[str, Any]]:
    """L10 oficial por team_id desde standings.splitRecords type=lastTen."""
    if season in _l10_cache:
        return _l10_cache[season]

    out: dict[int, dict[str, Any]] = {}
    try:
        r = _session.get(
            MLB_STANDINGS,
            params={
                "leagueId": "103,104",
                "season": season,
                "standingsTypes": "regularSeason",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        for block in r.json().get("records") or []:
            for entry in block.get("teamRecords") or []:
                tid = int((entry.get("team") or {}).get("id") or 0)
                if not tid:
                    continue
                l10 = None
                for sr in ((entry.get("records") or {}).get("splitRecords") or []):
                    if sr.get("type") == "lastTen":
                        l10 = sr
                        break
                if not l10:
                    continue
                wins = int(l10.get("wins") or 0)
                losses = int(l10.get("losses") or 0)
                pct = _safe_float(l10.get("pct"), wins / max(1, wins + losses)) or 0.5
                streak = entry.get("streak") or {}
                out[tid] = {
                    "ok": True,
                    "wins": wins,
                    "losses": losses,
                    "pct": round(float(pct), 3),
                    "marca": f"{wins}-{losses}",
                    "streak_code": streak.get("streakCode"),
                    "streak_type": streak.get("streakType"),
                    "streak_n": int(streak.get("streakNumber") or 0),
                }
    except Exception as e:
        print(f"[HISTORICO] L10 standings: {e}")

    _l10_cache[season] = out
    return out


def _agregar_outing(acc: dict[str, Any], split: dict[str, Any]) -> None:
    st = split.get("stat") or {}
    ip = _ip_a_float(st.get("inningsPitched"))
    acc["gs"] += int(st.get("gamesStarted") or 0) or (1 if ip > 0 else 0)
    acc["ip"] += ip
    acc["er"] += float(st.get("earnedRuns") or 0)
    acc["r"] += float(st.get("runs") or 0)
    acc["h"] += float(st.get("hits") or 0)
    acc["hr"] += float(st.get("homeRuns") or 0)
    acc["bb"] += float(st.get("baseOnBalls") or 0)
    acc["k"] += float(st.get("strikeOuts") or 0)
    acc["outs"] += []
    if split.get("date"):
        acc["fechas"].append(str(split["date"])[:10])
    if st.get("summary"):
        acc["resumenes"].append(str(st["summary"])[:48])


def pitcher_vs_rival(
    pitcher_id: int | None,
    rival_id: int | None,
    season: int,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """
    Historial del pitcher contra un equipo rival (gameLog + OPS vsTeamTotal).
    """
    base: dict[str, Any] = {
        "ok": False,
        "gs": 0,
        "ip": 0.0,
        "era": None,
        "whip": None,
        "k": 0,
        "bb": 0,
        "hr": 0,
        "ops_contra": None,
        "avg_contra": None,
        "calidad": "desconocido",  # bueno | neutro | malo | desconocido
        "motivo": "",
        "fechas": [],
        "resumenes": [],
    }
    if not pitcher_id or not rival_id:
        base["motivo"] = "sin pitcher o rival"
        return base

    cache_key = f"{pitcher_id}:{rival_id}:{season}"
    if cache_key in _pvr_cache:
        return dict(_pvr_cache[cache_key])

    acc = {
        "gs": 0,
        "ip": 0.0,
        "er": 0.0,
        "r": 0.0,
        "h": 0.0,
        "hr": 0.0,
        "bb": 0.0,
        "k": 0.0,
        "outs": [],
        "fechas": [],
        "resumenes": [],
    }
    ops_contra = None
    avg_contra = None
    nombre = None

    try:
        # gameLog: ERA/IP reales vs rival
        r = _session.get(
            f"{MLB_PEOPLE}/{int(pitcher_id)}",
            params={"hydrate": f"stats(group=[pitching],type=[gameLog],season={season})"},
            timeout=timeout,
        )
        r.raise_for_status()
        people = (r.json().get("people") or [{}])[0]
        nombre = people.get("fullName")
        for block in people.get("stats") or []:
            if (block.get("type") or {}).get("displayName") != "gameLog":
                continue
            for sp in block.get("splits") or []:
                oid = (sp.get("opponent") or {}).get("id")
                if oid is not None and int(oid) == int(rival_id):
                    _agregar_outing(acc, sp)

        # vsTeamTotal: OPS/AVG en contra (bateo del rival vs este pitcher)
        r2 = _session.get(
            f"{MLB_PEOPLE}/{int(pitcher_id)}",
            params={
                "hydrate": (
                    f"stats(group=[pitching],type=[vsTeamTotal],"
                    f"opposingTeamId={int(rival_id)},season={season})"
                )
            },
            timeout=timeout,
        )
        if r2.ok:
            people2 = (r2.json().get("people") or [{}])[0]
            for block in people2.get("stats") or []:
                if (block.get("type") or {}).get("displayName") != "vsTeamTotal":
                    continue
                for sp in block.get("splits") or []:
                    oid = (sp.get("opponent") or {}).get("id")
                    if oid is not None and int(oid) != int(rival_id):
                        continue
                    st = sp.get("stat") or {}
                    ops_contra = _safe_float(st.get("ops"))
                    avg_contra = _safe_float(st.get("avg"))
                    if not acc["gs"] and int(st.get("gamesPlayed") or 0) > 0:
                        acc["gs"] = int(st.get("gamesPlayed") or 0)
                    break
    except Exception as e:
        base["motivo"] = str(e)[:100]
        _pvr_cache[cache_key] = dict(base)
        return base

    ip = round(acc["ip"], 1)
    era = round(acc["er"] * 9.0 / ip, 2) if ip > 0 else None
    whip = round((acc["h"] + acc["bb"]) / ip, 2) if ip > 0 else None

    calidad = "desconocido"
    motivo = "sin historial vs rival"
    if ip >= 3.0 or acc["gs"] >= 1:
        if era is not None and era >= 6.0:
            calidad = "malo"
            motivo = f"ERA {era} en {ip} IP vs rival"
        elif ops_contra is not None and ops_contra >= 0.900 and (ip >= 3 or acc["gs"] >= 2):
            calidad = "malo"
            motivo = f"OPS contra {ops_contra:.3f} vs rival"
        elif era is not None and era <= 3.0 and ip >= 4.0:
            calidad = "bueno"
            motivo = f"ERA {era} en {ip} IP vs rival"
        elif ops_contra is not None and ops_contra <= 0.650 and ip >= 4.0:
            calidad = "bueno"
            motivo = f"OPS contra {ops_contra:.3f} vs rival"
        else:
            calidad = "neutro"
            bits = []
            if era is not None:
                bits.append(f"ERA {era}")
            if ip:
                bits.append(f"{ip} IP")
            if ops_contra is not None:
                bits.append(f"OPS {ops_contra:.3f}")
            motivo = " · ".join(bits) if bits else f"{acc['gs']} GS vs rival"

    out = {
        "ok": True,
        "pitcher_id": int(pitcher_id),
        "pitcher": nombre,
        "rival_id": int(rival_id),
        "gs": int(acc["gs"]),
        "ip": ip,
        "era": era,
        "whip": whip,
        "k": int(acc["k"]),
        "bb": int(acc["bb"]),
        "hr": int(acc["hr"]),
        "ops_contra": ops_contra,
        "avg_contra": avg_contra,
        "calidad": calidad,
        "motivo": motivo,
        "fechas": acc["fechas"][-4:],
        "resumenes": acc["resumenes"][-3:],
    }
    _pvr_cache[cache_key] = dict(out)
    return out


def _forma_l10(l10: dict[str, Any] | None) -> str:
    if not l10 or not l10.get("ok"):
        return "desconocida"
    wins = int(l10.get("wins") or 0)
    if wins >= 8:
        return "caliente"
    if wins <= 2:
        return "fria"
    if wins >= 6:
        return "buena"
    if wins <= 3:
        return "mala"
    return "neutra"


def analizar_historico_oficial(
    juego: dict[str, Any],
    season: int | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """
    Contexto histórico para un partido: L10 ambos lados + pitchers vs rival.
    """
    if season is None:
        raw = str(juego.get("fecha") or juego.get("inicio_juego") or "")[:10]
        try:
            season = int(raw.split("-")[0]) if raw else datetime.now(timezone.utc).year
        except Exception:
            season = datetime.now(timezone.utc).year

    away_id = juego.get("away_id")
    home_id = juego.get("home_id")
    try:
        away_id = int(away_id) if away_id is not None else None
    except (TypeError, ValueError):
        away_id = None
    try:
        home_id = int(home_id) if home_id is not None else None
    except (TypeError, ValueError):
        home_id = None

    l10_map = cargar_l10(int(season), timeout=timeout)
    l10_away = dict(l10_map.get(away_id) or {"ok": False, "motivo": "sin L10"})
    l10_home = dict(l10_map.get(home_id) or {"ok": False, "motivo": "sin L10"})
    if l10_away.get("ok"):
        l10_away["forma"] = _forma_l10(l10_away)
    if l10_home.get("ok"):
        l10_home["forma"] = _forma_l10(l10_home)

    pa_id = juego.get("pitcher_away_id")
    ph_id = juego.get("pitcher_home_id")
    try:
        pa_id = int(pa_id) if pa_id is not None else None
    except (TypeError, ValueError):
        pa_id = None
    try:
        ph_id = int(ph_id) if ph_id is not None else None
    except (TypeError, ValueError):
        ph_id = None

    # Away pitcher faces home lineup; home pitcher faces away lineup
    pvr_away = pitcher_vs_rival(pa_id, home_id, int(season), timeout=timeout)
    pvr_home = pitcher_vs_rival(ph_id, away_id, int(season), timeout=timeout)

    alertas: list[str] = []
    if l10_away.get("forma") == "fria":
        alertas.append("l10_fria_away")
    if l10_home.get("forma") == "fria":
        alertas.append("l10_fria_home")
    if l10_away.get("forma") == "caliente":
        alertas.append("l10_caliente_away")
    if l10_home.get("forma") == "caliente":
        alertas.append("l10_caliente_home")
    if pvr_away.get("calidad") == "malo":
        alertas.append("pvr_malo_away")
    if pvr_home.get("calidad") == "malo":
        alertas.append("pvr_malo_home")
    if pvr_away.get("calidad") == "bueno":
        alertas.append("pvr_bueno_away")
    if pvr_home.get("calidad") == "bueno":
        alertas.append("pvr_bueno_home")

    # Ajuste suave de fuerza (no domina al modelo)
    ajuste_away = 0.0
    ajuste_home = 0.0
    if l10_away.get("ok"):
        ajuste_away += (float(l10_away.get("pct") or 0.5) - 0.5) * 2.0
    if l10_home.get("ok"):
        ajuste_home += (float(l10_home.get("pct") or 0.5) - 0.5) * 2.0
    if pvr_away.get("calidad") == "bueno":
        ajuste_away += 0.6
    elif pvr_away.get("calidad") == "malo":
        ajuste_away -= 0.8
    if pvr_home.get("calidad") == "bueno":
        ajuste_home += 0.6
    elif pvr_home.get("calidad") == "malo":
        ajuste_home -= 0.8

    bits = []
    if l10_away.get("ok"):
        bits.append(f"L10 visita {l10_away.get('marca')}")
    if l10_home.get("ok"):
        bits.append(f"L10 local {l10_home.get('marca')}")
    if pvr_away.get("ok") and pvr_away.get("calidad") != "desconocido":
        bits.append(f"SP visita vs rival: {pvr_away.get('motivo')}")
    if pvr_home.get("ok") and pvr_home.get("calidad") != "desconocido":
        bits.append(f"SP local vs rival: {pvr_home.get('motivo')}")

    riesgo = any(
        a.startswith("l10_fria_") or a.startswith("pvr_malo_") for a in alertas
    )

    return {
        "ok": bool(l10_away.get("ok") or l10_home.get("ok") or pvr_away.get("ok") or pvr_home.get("ok")),
        "season": int(season),
        "l10_away": l10_away,
        "l10_home": l10_home,
        "pitcher_vs_rival_away": pvr_away,
        "pitcher_vs_rival_home": pvr_home,
        "alertas": alertas,
        "riesgo": riesgo,
        "ajuste_away": round(ajuste_away, 2),
        "ajuste_home": round(ajuste_home, 2),
        "resumen": " · ".join(bits)[:220] if bits else "sin historial oficial",
    }


def aplicar_ajustes_fuerza(
    f_away: float,
    f_home: float,
    historico: dict[str, Any] | None,
) -> tuple[float, float]:
    if not historico or not historico.get("ok"):
        return f_away, f_home
    return (
        round(f_away + float(historico.get("ajuste_away") or 0), 2),
        round(f_home + float(historico.get("ajuste_home") or 0), 2),
    )


def texto_para_ia(historico: dict[str, Any] | None) -> str:
    if not historico or not historico.get("ok"):
        return "Historial oficial: no disponible."
    return f"Historial oficial: {historico.get('resumen') or 'n/d'}"


def limpiar_caches() -> None:
    _l10_cache.clear()
    _pvr_cache.clear()
