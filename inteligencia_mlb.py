"""
5 capas extra de inteligencia (fusionadas al modelo):

1) Consenso de mercado — odds justas (sin vig) + multi-casa si hay
2) Bullpen del día — pitches de relevistas en el último juego
3) Park + umpire — refuerzo sobre las %
4) Tipo de pick — favorito_alto / underdog / scratch / limpio (para calibrar)
5) Monte Carlo — simulación ligera Elo+pitcher

No inventa cuotas ni mueve dinero solo: ajusta probabilidades y metadatos.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

_session = requests.Session()
_bullpen_cache: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# 1) Consenso de mercado
# ---------------------------------------------------------------------------

def odds_justas_sin_vig(
    dec_away: float | None,
    dec_home: float | None,
) -> dict[str, Any]:
    """Quita el overround de la casa → probs 'justas' del mercado."""
    try:
        da = float(dec_away) if dec_away else 0.0
        dh = float(dec_home) if dec_home else 0.0
    except (TypeError, ValueError):
        return {"ok": False, "motivo": "sin cuotas"}
    if da < 1.01 or dh < 1.01:
        return {"ok": False, "motivo": "cuotas inválidas"}
    ia, ih = 1.0 / da, 1.0 / dh
    s = ia + ih
    if s <= 0:
        return {"ok": False, "motivo": "vig inválido"}
    p_away = 100.0 * ia / s
    p_home = 100.0 * ih / s
    vig = max(0.0, (s - 1.0) * 100.0)
    return {
        "ok": True,
        "prob_away": round(p_away, 2),
        "prob_home": round(p_home, 2),
        "vig_pct": round(vig, 2),
        "dec_away_fair": round(100.0 / p_away, 3),
        "dec_home_fair": round(100.0 / p_home, 3),
    }


def consenso_mercado(juego: dict[str, Any]) -> dict[str, Any]:
    """
    Consenso = promedio de fuentes disponibles (ESPN/DK, OddsPapi, etc.).
    Si solo hay una casa, usa odds justas sin vig como ancla.
    """
    fuentes: list[dict[str, Any]] = []
    # Principal (ya aplicada al juego)
    if juego.get("odds_away_decimal") and juego.get("odds_home_decimal"):
        fuentes.append(
            {
                "casa": str(juego.get("lineas_fuente") or "mercado"),
                "away": float(juego["odds_away_decimal"]),
                "home": float(juego["odds_home_decimal"]),
            }
        )
    # Extra si el enricher dejó libros
    extra = juego.get("lineas_libros") if isinstance(juego.get("lineas_libros"), list) else []
    for b in extra:
        if not isinstance(b, dict):
            continue
        try:
            a, h = float(b.get("away") or 0), float(b.get("home") or 0)
        except (TypeError, ValueError):
            continue
        if a >= 1.01 and h >= 1.01:
            fuentes.append({"casa": str(b.get("casa") or "extra"), "away": a, "home": h})

    # Dedup por casa
    vistos: set[str] = set()
    unicas: list[dict[str, Any]] = []
    for f in fuentes:
        k = f["casa"].lower()
        if k in vistos:
            continue
        vistos.add(k)
        unicas.append(f)

    if not unicas:
        return {"ok": False, "motivo": "sin mercado", "n_fuentes": 0}

    avg_a = sum(f["away"] for f in unicas) / len(unicas)
    avg_h = sum(f["home"] for f in unicas) / len(unicas)
    fair = odds_justas_sin_vig(avg_a, avg_h)
    # Discrepancia entre casas (máx |implied diff|)
    disc = 0.0
    if len(unicas) >= 2:
        imps = []
        for f in unicas:
            fj = odds_justas_sin_vig(f["away"], f["home"])
            if fj.get("ok"):
                imps.append(float(fj["prob_home"]))
        if len(imps) >= 2:
            disc = max(imps) - min(imps)

    return {
        "ok": True,
        "n_fuentes": len(unicas),
        "fuentes": [f["casa"] for f in unicas][:6],
        "dec_away": round(avg_a, 3),
        "dec_home": round(avg_h, 3),
        "prob_away": fair.get("prob_away"),
        "prob_home": fair.get("prob_home"),
        "vig_pct": fair.get("vig_pct"),
        "discrepancia_casas_pct": round(disc, 2),
        "resumen": (
            f"Consenso {len(unicas)} casa(s) · fair "
            f"{fair.get('prob_away')}/{fair.get('prob_home')}% · vig {fair.get('vig_pct')}%"
            + (f" · Δcasas {disc:.1f}%" if disc >= 3 else "")
        ),
    }


# ---------------------------------------------------------------------------
# 2) Bullpen del día
# ---------------------------------------------------------------------------

def _fecha_iso(d: datetime | None = None) -> str:
    d = d or datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%d")


def analizar_bullpen_dia(
    team_id: int | None,
    *,
    season: int,
    nombre: str = "",
) -> dict[str, Any]:
    """Pitches de relevistas en el último juego final (últimos 3 días)."""
    if not team_id:
        return {"ok": False, "fatiga": 0.3, "motivo": "sin team_id"}
    key = f"{team_id}:{_fecha_iso()}"
    if key in _bullpen_cache:
        return _bullpen_cache[key]

    out: dict[str, Any] = {
        "ok": False,
        "fatiga": 0.3,
        "pitches_relevo": 0,
        "relevistas": 0,
        "horas_desde": None,
        "resumen": "",
        "nombre": nombre,
    }
    try:
        hoy = datetime.now(timezone.utc).date()
        start = (hoy - timedelta(days=3)).isoformat()
        end = hoy.isoformat()
        r = _session.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={
                "sportId": 1,
                "teamId": int(team_id),
                "startDate": start,
                "endDate": end,
                "season": season,
            },
            timeout=12,
        )
        r.raise_for_status()
        games = []
        for block in r.json().get("dates") or []:
            for g in block.get("games") or []:
                st = (g.get("status") or {}).get("abstractGameState") or ""
                if st == "Final":
                    games.append(g)
        if not games:
            out["motivo"] = "sin juego final reciente"
            _bullpen_cache[key] = out
            return out
        games.sort(key=lambda g: str(g.get("gameDate") or ""), reverse=True)
        last = games[0]
        gpk = last.get("gamePk")
        game_date = str(last.get("gameDate") or "")
        # Boxscore
        rb = _session.get(
            f"https://statsapi.mlb.com/api/v1/game/{gpk}/boxscore",
            timeout=12,
        )
        rb.raise_for_status()
        box = rb.json()
        lado = None
        for side in ("away", "home"):
            tid = ((box.get("teams") or {}).get(side) or {}).get("team", {}).get("id")
            if tid == int(team_id):
                lado = side
                break
        if not lado:
            out["motivo"] = "equipo no en boxscore"
            _bullpen_cache[key] = out
            return out
        players = ((box.get("teams") or {}).get(lado) or {}).get("players") or {}
        pitches = 0
        n_rel = 0
        for _pid, pl in players.items():
            stats = (pl.get("stats") or {}).get("pitching") or {}
            if not stats:
                continue
            # Starter: gamesStarted >= 1
            gs = int(float(stats.get("gamesStarted") or 0))
            if gs >= 1:
                continue
            npitch = stats.get("numberOfPitches") or stats.get("pitchesThrown")
            try:
                n = int(float(npitch or 0))
            except (TypeError, ValueError):
                # fallback innings * 15
                ip = str(stats.get("inningsPitched") or "0")
                try:
                    if "." in ip:
                        e, d = ip.split(".", 1)
                        outs = int(e) * 3 + int(d[:1] or 0)
                    else:
                        outs = int(float(ip)) * 3
                    n = outs * 5
                except (TypeError, ValueError):
                    n = 0
            if n <= 0:
                continue
            pitches += n
            n_rel += 1

        horas = None
        try:
            gd = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
            horas = max(0.0, (datetime.now(timezone.utc) - gd).total_seconds() / 3600.0)
        except ValueError:
            horas = 24.0

        # Fatiga 0-1: >80 pitches bullpen ayer = alto; decae con horas
        bruto = min(1.0, pitches / 90.0)
        if horas is not None and horas >= 36:
            bruto *= 0.55
        elif horas is not None and horas >= 20:
            bruto *= 0.75
        fatiga = round(max(0.05, min(0.95, 0.15 + bruto * 0.75)), 3)
        out.update(
            {
                "ok": True,
                "fatiga": fatiga,
                "pitches_relevo": pitches,
                "relevistas": n_rel,
                "horas_desde": round(horas, 1) if horas is not None else None,
                "game_pk": gpk,
                "resumen": (
                    f"Bullpen {nombre or team_id}: {pitches} pitches relevo "
                    f"({n_rel} brazos) · fatiga {fatiga:.2f}"
                ),
            }
        )
    except Exception as e:
        out["motivo"] = str(e)[:100]
    _bullpen_cache[key] = out
    return out


# ---------------------------------------------------------------------------
# 3) Park + umpire
# ---------------------------------------------------------------------------

def ajuste_park_umpire(
    *,
    park_factor: float = 1.0,
    sesgo_umpire_runs: float = 0.0,
    es_home: bool = True,
) -> float:
    """
    Ajuste en puntos de 'fuerza' / semi-prob.
    Park alto (>1) favorece bateo → ligero boost ofensiva local.
    Umpire hitter-friendly → más corridas (leve ruido, no edge claro).
    """
    park_adj = (float(park_factor) - 1.0) * 8.0  # Coors ~+8–12
    ump_adj = float(sesgo_umpire_runs) * 2.5
    # Local siente más el park
    if es_home:
        return round(park_adj * 0.55 + ump_adj * 0.3, 2)
    return round(park_adj * 0.35 - ump_adj * 0.15, 2)


def aplicar_park_umpire_a_probs(
    prob_away: float,
    prob_home: float,
    *,
    park_factor: float,
    sesgo_umpire: float,
) -> tuple[float, float, dict[str, Any]]:
    adj_h = ajuste_park_umpire(
        park_factor=park_factor, sesgo_umpire_runs=sesgo_umpire, es_home=True
    )
    adj_a = ajuste_park_umpire(
        park_factor=park_factor, sesgo_umpire_runs=sesgo_umpire, es_home=False
    )
    # Convertir ajuste fuerza → shift logístico suave
    # +1 fuerza ≈ +1.2% 
    h = float(prob_home) + adj_h * 1.15
    a = float(prob_away) + adj_a * 1.15
    s = h + a
    if s <= 0:
        return prob_away, prob_home, {"ok": False}
    h, a = 100.0 * h / s, 100.0 * a / s
    return (
        round(a, 2),
        round(h, 2),
        {
            "ok": True,
            "adj_away": adj_a,
            "adj_home": adj_h,
            "park_factor": park_factor,
            "sesgo_umpire": sesgo_umpire,
            "resumen": f"Park×{park_factor:.2f} ump {sesgo_umpire:+.2f} → {a:.0f}/{h:.0f}%",
        },
    )


# ---------------------------------------------------------------------------
# 4) Tipo de pick (para calibración)
# ---------------------------------------------------------------------------

TIPOS_PICK = ("favorito_alto", "underdog", "scratch", "limpio")


def clasificar_tipo_pick(
    juego: dict[str, Any],
    *,
    pick: str | None = None,
    prob: float | None = None,
    odds: float | None = None,
) -> str:
    pick = pick or str(juego.get("pick") or "")
    try:
        prob_f = float(prob if prob is not None else juego.get("probPick") or 50)
    except (TypeError, ValueError):
        prob_f = 50.0
    try:
        odds_f = float(odds if odds is not None else juego.get("odds") or 0)
    except (TypeError, ValueError):
        odds_f = 0.0

    scratch = juego.get("scratch_lineup") if isinstance(juego.get("scratch_lineup"), dict) else {}
    if scratch.get("riesgo"):
        return "scratch"
    if odds_f >= 2.0 or (odds_f >= 1.70 and prob_f < 55):
        return "underdog"
    if prob_f >= 62 or (odds_f and 1.01 <= odds_f <= 1.55):
        return "favorito_alto"
    return "limpio"


# ---------------------------------------------------------------------------
# 5) Monte Carlo ligero
# ---------------------------------------------------------------------------

def monte_carlo_probs(
    elo_home_adj: float,
    elo_away_adj: float,
    *,
    home_adv: float = 24.0,
    n: int = 800,
    noise: float = 45.0,
    seed: int | None = None,
) -> dict[str, Any]:
    """Simula n enfrentamientos con ruido gaussiano sobre Elo."""
    rng = random.Random(seed)
    wins_home = 0
    for _ in range(max(50, int(n))):
        h = elo_home_adj + home_adv + rng.gauss(0, noise)
        a = elo_away_adj + rng.gauss(0, noise)
        # Prob puntual Elo
        p_h = 1.0 / (1.0 + math.pow(10.0, -(h - a) / 400.0))
        if rng.random() < p_h:
            wins_home += 1
    n_eff = max(50, int(n))
    p_home = 100.0 * wins_home / n_eff
    p_away = 100.0 - p_home
    return {
        "ok": True,
        "n": n_eff,
        "prob_home": round(p_home, 2),
        "prob_away": round(p_away, 2),
        "resumen": f"MC×{n_eff} → {p_away:.0f}/{p_home:.0f}%",
    }


def fusionar_con_montecarlo(
    prob_away: float,
    prob_home: float,
    mc: dict[str, Any],
    *,
    peso_mc: float = 0.25,
) -> tuple[float, float]:
    if not mc.get("ok"):
        return prob_away, prob_home
    w = max(0.0, min(0.5, float(peso_mc)))
    a = (1 - w) * float(prob_away) + w * float(mc["prob_away"])
    h = (1 - w) * float(prob_home) + w * float(mc["prob_home"])
    s = a + h
    if s <= 0:
        return prob_away, prob_home
    return round(100.0 * a / s, 2), round(100.0 * h / s, 2)


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------

def enriquecer_probs(
    juego: dict[str, Any],
    prob_away: float,
    prob_home: float,
    cfg: dict | None = None,
    *,
    park_factor: float = 1.0,
    season: int = 2026,
) -> tuple[float, float, dict[str, Any]]:
    """Aplica capas 1–5 sobre probs ya fusionadas (modelo+Elo)."""
    cfg = cfg or {}
    intel_cfg = cfg.get("inteligencia") if isinstance(cfg.get("inteligencia"), dict) else {}
    meta: dict[str, Any] = {"ok": True, "capas": []}

    # 2) Bullpen
    bp_away = bp_home = {"ok": False, "fatiga": 0.3}
    if intel_cfg.get("bullpen_dia", True) and cfg.get("estrategia", {}).get("analizar_bullpen", True):
        bp_away = analizar_bullpen_dia(
            juego.get("away_id"), season=season, nombre=str(juego.get("visitante") or "")
        )
        bp_home = analizar_bullpen_dia(
            juego.get("home_id"), season=season, nombre=str(juego.get("home") or "")
        )
        # Fatiga alta → baja un poco la % de ese equipo
        fa, fh = float(bp_away.get("fatiga") or 0.3), float(bp_home.get("fatiga") or 0.3)
        # Differencial
        shift = (fh - fa) * 4.0  # home más fatigado → away sube
        prob_away = float(prob_away) + shift
        prob_home = float(prob_home) - shift
        s = prob_away + prob_home
        if s > 0:
            prob_away, prob_home = 100.0 * prob_away / s, 100.0 * prob_home / s
        meta["bullpen"] = {
            "away": bp_away,
            "home": bp_home,
            "shift_hacia_away": round(shift, 2),
        }
        meta["capas"].append("bullpen")

    # 3) Park + umpire
    if intel_cfg.get("park_umpire", True):
        humanos = juego.get("factores_humanos") if isinstance(juego.get("factores_humanos"), dict) else {}
        sesgo = float(humanos.get("sesgo_umpire_runs") or 0.0)
        prob_away, prob_home, park_meta = aplicar_park_umpire_a_probs(
            prob_away,
            prob_home,
            park_factor=park_factor,
            sesgo_umpire=sesgo,
        )
        meta["park_umpire"] = park_meta
        if park_meta.get("ok"):
            meta["capas"].append("park_umpire")

    # 5) Monte Carlo (usa Elo adj si existe)
    if intel_cfg.get("monte_carlo", True):
        elo = juego.get("elo") if isinstance(juego.get("elo"), dict) else {}
        try:
            eh = float(elo.get("elo_adj_home") or elo.get("elo_home") or 1500)
            ea = float(elo.get("elo_adj_away") or elo.get("elo_away") or 1500)
        except (TypeError, ValueError):
            eh, ea = 1500.0, 1500.0
        home_adv = float((cfg.get("elo") or {}).get("home_adv") or 24)
        n_mc = int(intel_cfg.get("mc_sims") or 800)
        seed = None
        try:
            seed = int(juego.get("id") or 0) % 100000
        except (TypeError, ValueError):
            seed = 42
        mc = monte_carlo_probs(eh, ea, home_adv=home_adv, n=n_mc, seed=seed)
        peso = float(intel_cfg.get("peso_mc") or 0.22)
        prob_away, prob_home = fusionar_con_montecarlo(
            prob_away, prob_home, mc, peso_mc=peso
        )
        meta["monte_carlo"] = mc
        meta["capas"].append("monte_carlo")

    # 1) Consenso (metadato + opcional ancla suave hacia fair)
    cons = {"ok": False}
    if intel_cfg.get("consenso_mercado", True):
        cons = consenso_mercado(juego)
        meta["consenso"] = cons
        if cons.get("ok"):
            meta["capas"].append("consenso")
            # Ancla suave 10% hacia mercado justo (evita overconfidence)
            w = float(intel_cfg.get("peso_consenso") or 0.10)
            if w > 0 and cons.get("prob_away") is not None:
                prob_away = (1 - w) * float(prob_away) + w * float(cons["prob_away"])
                prob_home = (1 - w) * float(prob_home) + w * float(cons["prob_home"])
                s = prob_away + prob_home
                if s > 0:
                    prob_away, prob_home = 100.0 * prob_away / s, 100.0 * prob_home / s

    # Normalizar final
    s = float(prob_away) + float(prob_home)
    if s > 0:
        prob_away = round(100.0 * float(prob_away) / s, 2)
        prob_home = round(100.0 * float(prob_home) / s, 2)

    # 4) Tipo (se asigna tras elegir pick; aquí pre-tipo por probs)
    pre_tipo = "limpio"
    if max(prob_away, prob_home) >= 62:
        pre_tipo = "favorito_alto"
    meta["tipo_pre"] = pre_tipo
    meta["resumen"] = (
        f"Intel[{','.join(meta['capas']) or 'none'}] → {prob_away:.0f}/{prob_home:.0f}%"
    )
    juego["inteligencia"] = meta
    juego["bullpen_dia"] = {"away": bp_away, "home": bp_home}
    return prob_away, prob_home, meta
