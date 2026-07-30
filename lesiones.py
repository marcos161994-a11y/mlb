"""
Lesiones MLB vía ESPN (gratis, sin API key).

Detecta IL / day-to-day por equipo, cruza con pitchers probables
y aporta ajuste + contexto para Groq.
"""

from __future__ import annotations

import re
import time
from typing import Any

import requests

ESPN_INJURIES_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/injuries"
_session = requests.Session()

# Cache global del reporte (TTL ~20 min)
_cache_reporte: dict[str, Any] | None = None
_cache_ts: float = 0.0
CACHE_TTL_SEC = 20 * 60

# Alias nombre ESPN ↔ StatsAPI
ALIASES_EQUIPO: dict[str, str] = {
    "oakland athletics": "athletics",
    "athletics": "athletics",
    "a's": "athletics",
    "arizona diamondbacks": "arizona diamondbacks",
    "chicago white sox": "chicago white sox",
    "chicago cubs": "chicago cubs",
    "los angeles angels": "los angeles angels",
    "los angeles dodgers": "los angeles dodgers",
    "new york yankees": "new york yankees",
    "new york mets": "new york mets",
    "tampa bay rays": "tampa bay rays",
    "san francisco giants": "san francisco giants",
    "st. louis cardinals": "st. louis cardinals",
    "saint louis cardinals": "st. louis cardinals",
}


def _norm_equipo(nombre: str) -> str:
    n = (nombre or "").strip().lower()
    n = n.replace(".", "")
    return ALIASES_EQUIPO.get(n, n)


def _norm_jugador(nombre: str) -> str:
    n = (nombre or "").strip().lower()
    n = re.sub(r"[^a-z\s]", "", n)
    partes = n.split()
    if not partes:
        return ""
    # Último token (apellido) + inicial si hay
    return partes[-1]


def _es_relevante(status: str, pos: str) -> bool:
    """Prioriza pitchers y bajas claras (IL / Out / Day-To-Day)."""
    s = (status or "").lower()
    p = (pos or "").upper()
    if any(x in s for x in ("il", "out", "day-to-day", "day to day", "suspended")):
        return True
    if p in ("SP", "RP", "P", "C", "SS", "2B", "3B", "1B", "CF", "RF", "LF", "OF", "DH"):
        return "questionable" in s or "doubtful" in s or True
    return False


def _severidad(status: str, pos: str) -> int:
    """1=leve … 5=crítica (SP en IL)."""
    s = (status or "").lower()
    p = (pos or "").upper()
    score = 1
    if "60" in s or "60-day" in s:
        score = 4
    elif "15" in s or "10" in s or "il" in s:
        score = 3
    elif "out" in s:
        score = 3
    elif "day" in s:
        score = 2
    elif "questionable" in s or "doubtful" in s:
        score = 2
    if p == "SP":
        score += 2
    elif p in ("RP", "P"):
        score += 1
    elif p in ("CF", "SS", "C", "RF", "LF", "3B"):
        score += 1
    return min(5, score)


def cargar_reporte_lesiones(forzar: bool = False, timeout: float = 12.0) -> dict[str, Any]:
    """
    Descarga el board de lesiones ESPN y lo indexa por equipo.
    Returns: {ok, equipos: {nombre_norm: [lesiones]}, fuente, motivo}
    """
    global _cache_reporte, _cache_ts
    ahora = time.time()
    if (
        not forzar
        and _cache_reporte is not None
        and (ahora - _cache_ts) < CACHE_TTL_SEC
    ):
        return _cache_reporte

    out: dict[str, Any] = {
        "ok": False,
        "fuente": "espn",
        "equipos": {},
        "motivo": "",
        "total": 0,
    }
    try:
        r = _session.get(ESPN_INJURIES_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        equipos: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for bloque in data.get("injuries") or []:
            team_name = bloque.get("displayName") or ""
            key = _norm_equipo(team_name)
            lista: list[dict[str, Any]] = []
            for inj in bloque.get("injuries") or []:
                ath = inj.get("athlete") or {}
                pos_raw = ath.get("position")
                if isinstance(pos_raw, dict):
                    pos = pos_raw.get("abbreviation") or pos_raw.get("displayName") or ""
                else:
                    pos = str(pos_raw or "")
                status = inj.get("status") or ""
                if not _es_relevante(status, pos):
                    continue
                nombre = ath.get("displayName") or f"{ath.get('firstName','')} {ath.get('lastName','')}".strip()
                item = {
                    "jugador": nombre,
                    "apellido": _norm_jugador(nombre),
                    "pos": pos or "?",
                    "status": status,
                    "comentario": (inj.get("shortComment") or "")[:140],
                    "severidad": _severidad(status, pos),
                    "equipo": team_name,
                }
                lista.append(item)
                total += 1
            lista.sort(key=lambda x: -x["severidad"])
            equipos[key] = lista
        out.update({"ok": True, "equipos": equipos, "total": total, "motivo": f"{total} bajas indexadas"})
        _cache_reporte = out
        _cache_ts = ahora
        return out
    except requests.Timeout:
        out["motivo"] = "Timeout ESPN injuries"
        return out
    except Exception as e:
        out["motivo"] = str(e)[:120]
        return out


def lesiones_equipo(nombre_equipo: str, reporte: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    reporte = reporte or cargar_reporte_lesiones()
    if not reporte.get("ok"):
        return []
    return list((reporte.get("equipos") or {}).get(_norm_equipo(nombre_equipo), []))


def _pitcher_en_lista(nombre_pitcher: str, bajas: list[dict[str, Any]]) -> dict[str, Any] | None:
    ap = _norm_jugador(nombre_pitcher)
    if not ap or ap in ("tbd", "undecided", "unknown"):
        return None
    for b in bajas:
        if b.get("apellido") == ap:
            return b
        # match parcial apellido en comentario/nombre
        jug = (b.get("jugador") or "").lower()
        if ap in jug.split():
            return b
    return None


def analizar_lesiones_juego(
    visitante: str,
    home: str,
    pitcher_away: str | None = None,
    pitcher_home: str | None = None,
    reporte: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Resumen de lesiones para un partido + flags críticos.
    """
    reporte = reporte or cargar_reporte_lesiones()
    base: dict[str, Any] = {
        "ok": bool(reporte.get("ok")),
        "fuente": reporte.get("fuente") or "espn",
        "motivo": reporte.get("motivo") or "",
        "away": [],
        "home": [],
        "resumen": "",
        "ajuste_away": 0.0,
        "ajuste_home": 0.0,
        "starter_riesgo": False,
        "starter_away_lesionado": False,
        "starter_home_lesionado": False,
        "alerta": "",
    }
    if not base["ok"]:
        return base

    away = lesiones_equipo(visitante, reporte)[:8]
    home_l = lesiones_equipo(home, reporte)[:8]
    base["away"] = away
    base["home"] = home_l

    hit_a = _pitcher_en_lista(pitcher_away or "", away)
    hit_h = _pitcher_en_lista(pitcher_home or "", home_l)
    if hit_a:
        base["starter_away_lesionado"] = True
        base["starter_riesgo"] = True
        base["alerta"] = f"SP visitante en baja: {hit_a.get('jugador')} ({hit_a.get('status')})"
    if hit_h:
        base["starter_home_lesionado"] = True
        base["starter_riesgo"] = True
        extra = f"SP local en baja: {hit_h.get('jugador')} ({hit_h.get('status')})"
        base["alerta"] = f"{base['alerta']} · {extra}".strip(" ·")

    base["ajuste_away"] = _ajuste_fuerza(away, starter_out=bool(hit_a))
    base["ajuste_home"] = _ajuste_fuerza(home_l, starter_out=bool(hit_h))
    base["resumen"] = _texto_resumen(visitante, away, home, home_l, base)
    return base


def _ajuste_fuerza(bajas: list[dict[str, Any]], starter_out: bool) -> float:
    """Penalización negativa a la fuerza del equipo con bajas."""
    if starter_out:
        return -4.0
    pen = 0.0
    sps = sum(1 for b in bajas if (b.get("pos") or "").upper() == "SP")
    bats = sum(1 for b in bajas if (b.get("pos") or "").upper() not in ("SP", "RP", "P", "?"))
    rps = sum(1 for b in bajas if (b.get("pos") or "").upper() in ("RP", "P"))
    pen -= min(2.5, sps * 0.45)  # profundidad de rotación
    pen -= min(2.0, bats * 0.35)  # bats clave
    pen -= min(1.2, rps * 0.25)  # bullpen
    # top severidad
    if bajas:
        pen -= min(1.5, (bajas[0].get("severidad") or 0) * 0.2)
    return round(pen, 2)


def _texto_resumen(
    visitante: str,
    away: list[dict[str, Any]],
    home: str,
    home_l: list[dict[str, Any]],
    meta: dict[str, Any],
) -> str:
    def fmt(lista: list[dict[str, Any]], n: int = 4) -> str:
        if not lista:
            return "sin bajas relevantes"
        bits = [f"{x['jugador']}({x['pos']}/{x['status']})" for x in lista[:n]]
        return ", ".join(bits)

    partes = [
        f"{visitante}: {fmt(away)}",
        f"{home}: {fmt(home_l)}",
    ]
    if meta.get("alerta"):
        partes.insert(0, f"ALERTA: {meta['alerta']}")
    return " | ".join(partes)


def texto_para_ia(lesiones: dict[str, Any] | None, max_len: int = 420) -> str:
    """Bloque corto para el prompt de Groq."""
    if not lesiones or not lesiones.get("ok"):
        return "Lesiones: sin datos"
    txt = lesiones.get("resumen") or ""
    if lesiones.get("starter_riesgo"):
        txt = f"CRITICO starter lesionado. {txt}"
    return txt[:max_len]
