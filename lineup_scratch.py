"""
Scratch de abridor y estrellas fuera del lineup (MLB StatsAPI).

- Scratch SP: el pitcher probable cambió vs el congelado / TBD con lineup listo
- Estrellas: top OPS del equipo ausentes del lineup confirmado
"""

from __future__ import annotations

from typing import Any

import requests

_session = requests.Session()
_top_hit_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}


def _extraer_jugadores_lineup(lineups_api: dict[str, Any] | None, lado: str) -> list[dict[str, Any]]:
    if not lineups_api:
        return []
    raw = (
        lineups_api.get(f"{lado}Players")
        or lineups_api.get(lado)
        or []
    )
    out: list[dict[str, Any]] = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        nombre = p.get("fullName") or p.get("useName") or ""
        if pid:
            out.append({"id": int(pid), "nombre": nombre})
    return out


def parsear_lineups_juego(juego_api: dict[str, Any]) -> dict[str, Any]:
    """Desde el dict crudo del schedule MLB."""
    lu = juego_api.get("lineups") or {}
    away = _extraer_jugadores_lineup(lu, "away")
    home = _extraer_jugadores_lineup(lu, "home")
    return {
        "away": away,
        "home": home,
        "confirmado": bool(away) and bool(home),
    }


def top_bateadores_equipo(team_id: int, season: int, n: int = 5) -> list[dict[str, Any]]:
    """Top N bateadores del equipo por OPS (mín. ~30 PA si hay datos)."""
    key = (team_id, season)
    if key in _top_hit_cache:
        return _top_hit_cache[key][:n]
    try:
        r = _session.get(
            f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
            params={"rosterType": "active", "season": season},
            timeout=12,
        )
        r.raise_for_status()
        personas = []
        for entry in r.json().get("roster") or []:
            person = entry.get("person") or {}
            pid = person.get("id")
            if not pid:
                continue
            pos = ((entry.get("position") or {}).get("abbreviation") or "").upper()
            if pos in ("P", "TWP"):
                continue
            personas.append((int(pid), person.get("fullName") or ""))

        stats_list: list[dict[str, Any]] = []
        # Pedir stats en lote (hasta ~40 ids)
        ids = [p[0] for p in personas]
        if not ids:
            _top_hit_cache[key] = []
            return []
        r2 = _session.get(
            "https://statsapi.mlb.com/api/v1/people",
            params={
                "personIds": ",".join(str(i) for i in ids[:45]),
                "hydrate": f"stats(group=[hitting],type=[season],season={season})",
            },
            timeout=20,
        )
        r2.raise_for_status()
        for person in r2.json().get("people") or []:
            pid = person.get("id")
            nombre = person.get("fullName") or ""
            ops = 0.0
            pa = 0
            for st in person.get("stats") or []:
                splits = st.get("splits") or []
                if not splits:
                    continue
                stat = splits[0].get("stat") or {}
                ops = float(stat.get("ops") or 0) or 0.0
                pa = int(stat.get("plateAppearances") or stat.get("atBats") or 0)
                break
            if pa < 25 and ops <= 0:
                continue
            stats_list.append({"id": int(pid), "nombre": nombre, "ops": ops, "pa": pa})
        stats_list.sort(key=lambda x: (x["ops"], x["pa"]), reverse=True)
        _top_hit_cache[key] = stats_list
        return stats_list[:n]
    except Exception:
        _top_hit_cache[key] = []
        return []


def analizar_scratch_lineup(
    *,
    away_id: int | None,
    home_id: int | None,
    pitcher_away_id: int | None,
    pitcher_home_id: int | None,
    pitcher_away_nombre: str | None,
    pitcher_home_nombre: str | None,
    lineups: dict[str, Any] | None,
    season: int,
    pred_congelada: dict[str, Any] | None = None,
    min_estrellas_fuera: int = 2,
) -> dict[str, Any]:
    """
    Returns flags: scratch_away/home, estrellas_fuera_*, riesgo, ajuste_*, resumen.
    """
    lu = lineups or {}
    away_lu = lu.get("away") or []
    home_lu = lu.get("home") or []
    confirmado = bool(lu.get("confirmado")) or (bool(away_lu) and bool(home_lu))

    out: dict[str, Any] = {
        "ok": True,
        "lineup_confirmado": confirmado,
        "scratch_away": False,
        "scratch_home": False,
        "estrellas_fuera_away": [],
        "estrellas_fuera_home": [],
        "ajuste_away": 0.0,
        "ajuste_home": 0.0,
        "min_estrellas_fuera": int(min_estrellas_fuera),
        "riesgo": False,
        "alerta": "",
        "resumen": "",
    }

    # Scratch: pitcher congelado ≠ pitcher actual (mismo lado)
    if pred_congelada:
        pa_prev = pred_congelada.get("pitcher_away_id") or pred_congelada.get("pitcherAwayId")
        ph_prev = pred_congelada.get("pitcher_home_id") or pred_congelada.get("pitcherHomeId")
        # También comparar por nombre si no hay id en pred
        pa_nom_prev = (pred_congelada.get("pitcherAway") or "").strip().lower()
        ph_nom_prev = (pred_congelada.get("pitcherHome") or "").strip().lower()
        pa_nom = (pitcher_away_nombre or "").strip().lower()
        ph_nom = (pitcher_home_nombre or "").strip().lower()

        if pa_prev and pitcher_away_id and int(pa_prev) != int(pitcher_away_id):
            out["scratch_away"] = True
        elif pa_nom_prev and pa_nom and pa_nom_prev != pa_nom and "tbd" not in pa_nom_prev:
            out["scratch_away"] = True

        if ph_prev and pitcher_home_id and int(ph_prev) != int(pitcher_home_id):
            out["scratch_home"] = True
        elif ph_nom_prev and ph_nom and ph_nom_prev != ph_nom and "tbd" not in ph_nom_prev:
            out["scratch_home"] = True

    # Bullpen game / TBD con lineup ya confirmado → tratar como scratch del lado TBD
    if confirmado:
        if not pitcher_away_id or (pitcher_away_nombre or "").upper() == "TBD":
            out["scratch_away"] = True
        if not pitcher_home_id or (pitcher_home_nombre or "").upper() == "TBD":
            out["scratch_home"] = True

    # Estrellas fuera del lineup
    if confirmado and away_id:
        top = top_bateadores_equipo(int(away_id), season, n=5)
        ids_lu = {p["id"] for p in away_lu}
        fuera = [t for t in top if t["id"] not in ids_lu]
        out["estrellas_fuera_away"] = fuera
        if len(fuera) >= min_estrellas_fuera:
            out["ajuste_away"] = -min(3.5, 1.2 * len(fuera))

    if confirmado and home_id:
        top = top_bateadores_equipo(int(home_id), season, n=5)
        ids_lu = {p["id"] for p in home_lu}
        fuera = [t for t in top if t["id"] not in ids_lu]
        out["estrellas_fuera_home"] = fuera
        if len(fuera) >= min_estrellas_fuera:
            out["ajuste_home"] = -min(3.5, 1.2 * len(fuera))

    alertas = []
    if out["scratch_away"]:
        alertas.append(f"Scratch/TBD SP visitante ({pitcher_away_nombre or 'TBD'})")
    if out["scratch_home"]:
        alertas.append(f"Scratch/TBD SP local ({pitcher_home_nombre or 'TBD'})")
    if len(out["estrellas_fuera_away"]) >= min_estrellas_fuera:
        noms = ", ".join(x["nombre"] for x in out["estrellas_fuera_away"][:3])
        alertas.append(f"Estrellas out visitante: {noms}")
    if len(out["estrellas_fuera_home"]) >= min_estrellas_fuera:
        noms = ", ".join(x["nombre"] for x in out["estrellas_fuera_home"][:3])
        alertas.append(f"Estrellas out local: {noms}")

    out["alerta"] = " · ".join(alertas)
    out["riesgo"] = bool(
        out["scratch_away"]
        or out["scratch_home"]
        or len(out["estrellas_fuera_away"]) >= min_estrellas_fuera
        or len(out["estrellas_fuera_home"]) >= min_estrellas_fuera
    )
    out["resumen"] = out["alerta"] or ("Lineup OK" if confirmado else "Lineup pendiente")
    return out


def pick_afectado_por_scratch(
    pick: str,
    visitante: str,
    home: str,
    info: dict[str, Any],
    min_estrellas: int | None = None,
) -> bool:
    """True si el ML apostado es el lado con scratch SP o muchas estrellas out."""
    p = pick or ""
    umbral = int(
        min_estrellas
        if min_estrellas is not None
        else (info.get("min_estrellas_fuera") or 2)
    )
    if info.get("scratch_away") and visitante and visitante in p:
        return True
    if info.get("scratch_home") and home and home in p:
        return True
    if len(info.get("estrellas_fuera_away") or []) >= umbral and visitante and visitante in p:
        return True
    if len(info.get("estrellas_fuera_home") or []) >= umbral and home and home in p:
        return True
    return False


def texto_para_ia(info: dict[str, Any] | None, max_len: int = 280) -> str:
    if not info or not info.get("ok"):
        return "Lineup/scratch: sin datos"
    if info.get("riesgo"):
        return f"ALERTA roster: {(info.get('alerta') or '')[:max_len]}"
    return f"Lineup: {(info.get('resumen') or 'ok')[:max_len]}"
