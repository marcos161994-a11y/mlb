"""
Elo estilo FiveThirtyEight (versión ligera) + ajuste de abridor.

1) Elo de equipo (sube/baja con resultados)
2) Ajuste por pitcher (FIP vs liga → puntos Elo)
3) Probabilidad home/away → se fusiona con el % del modelo

Persistencia: DATA_DIR/elo_ratings.json
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))

DEFAULT_ELO = 1500.0
DEFAULT_K = 20.0
DEFAULT_HOME_ADV = 24.0  # ~538 MLB
DEFAULT_FIP_LIGA = 4.20
DEFAULT_PITCHER_SCALE = 28.0  # pts Elo por 1.0 FIP mejor/peor que liga
DEFAULT_PESO_ELO = 0.40  # blend: 40% Elo+pitcher, 60% modelo
MAX_PITCHER_ADJ = 55.0


def _path() -> Path:
    d = Path(os.environ.get("DATA_DIR") or str(DATA_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d / "elo_ratings.json"


def _cfg_elo(cfg: dict | None) -> dict[str, Any]:
    cfg = cfg or {}
    bloque = cfg.get("elo") if isinstance(cfg.get("elo"), dict) else {}
    return {
        "activo": bool(cfg.get("usar_elo", True)),
        "k": float(bloque.get("k") or DEFAULT_K),
        "home_adv": float(bloque.get("home_adv") or DEFAULT_HOME_ADV),
        "fip_liga": float(bloque.get("fip_liga") or DEFAULT_FIP_LIGA),
        "pitcher_scale": float(bloque.get("pitcher_scale") or DEFAULT_PITCHER_SCALE),
        "peso_elo": float(bloque.get("peso_elo") if bloque.get("peso_elo") is not None else DEFAULT_PESO_ELO),
    }


def cargar_ratings() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"equipos": {}, "juegos_aplicados": [], "actualizado_en": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"equipos": {}, "juegos_aplicados": [], "actualizado_en": None}
    if not isinstance(data, dict):
        return {"equipos": {}, "juegos_aplicados": [], "actualizado_en": None}
    data.setdefault("equipos", {})
    data.setdefault("juegos_aplicados", [])
    return data


def guardar_ratings(data: dict[str, Any]) -> None:
    data["actualizado_en"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Limitar historial de ids
    aplicados = data.get("juegos_aplicados") or []
    if isinstance(aplicados, list) and len(aplicados) > 4000:
        data["juegos_aplicados"] = aplicados[-3000:]
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def elo_equipo(team_id: int | str | None, data: dict | None = None) -> float:
    if team_id is None or team_id == "":
        return DEFAULT_ELO
    data = data or cargar_ratings()
    equipos = data.get("equipos") if isinstance(data.get("equipos"), dict) else {}
    key = str(int(team_id)) if str(team_id).isdigit() else str(team_id)
    raw = equipos.get(key)
    if isinstance(raw, dict):
        try:
            return float(raw.get("elo") or DEFAULT_ELO)
        except (TypeError, ValueError):
            return DEFAULT_ELO
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ELO


def asegurar_equipo(
    team_id: int | str | None,
    data: dict,
    *,
    win_pct: float | None = None,
    nombre: str = "",
) -> float:
    """Crea Elo inicial; si hay win_pct, sesga suave desde 1500."""
    if team_id is None or team_id == "":
        return DEFAULT_ELO
    key = str(int(team_id)) if str(team_id).isdigit() else str(team_id)
    equipos = data.setdefault("equipos", {})
    if key in equipos:
        return elo_equipo(key, data)
    elo0 = DEFAULT_ELO
    if win_pct is not None:
        try:
            wp = float(win_pct)
            # 0.40 → -40, 0.60 → +40
            elo0 = DEFAULT_ELO + max(-80.0, min(80.0, (wp - 0.5) * 400.0))
        except (TypeError, ValueError):
            elo0 = DEFAULT_ELO
    equipos[key] = {
        "elo": round(elo0, 2),
        "n": 0,
        "nombre": (nombre or "")[:40],
    }
    return elo0


def prob_desde_elo(
    elo_home: float,
    elo_away: float,
    *,
    home_adv: float = DEFAULT_HOME_ADV,
) -> tuple[float, float]:
    """Prob home/away (0-100) con ventaja de local en puntos Elo."""
    diff = (elo_home + home_adv) - elo_away
    # Fórmula Elo clásica
    p_home = 1.0 / (1.0 + math.pow(10.0, -diff / 400.0))
    p_home = max(0.05, min(0.95, p_home))
    return round(p_home * 100.0, 2), round((1.0 - p_home) * 100.0, 2)


def ajuste_pitcher_fip(
    fip: float | None,
    *,
    fip_liga: float = DEFAULT_FIP_LIGA,
    scale: float = DEFAULT_PITCHER_SCALE,
) -> float:
    """
    FIP mejor (más bajo) que la liga → puntos Elo positivos para ese equipo.
    FIP 3.20 vs 4.20 → +28 pts aprox. con scale=28.
    """
    if fip is None:
        return 0.0
    try:
        f = float(fip)
    except (TypeError, ValueError):
        return 0.0
    if f <= 0 or f > 12:
        return 0.0
    adj = (fip_liga - f) * scale
    return round(max(-MAX_PITCHER_ADJ, min(MAX_PITCHER_ADJ, adj)), 2)


def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, (elo_b - elo_a) / 400.0))


def actualizar_resultado(
    *,
    away_id: int | str | None,
    home_id: int | str | None,
    score_away: int | float | None,
    score_home: int | float | None,
    game_id: str | None = None,
    away_nombre: str = "",
    home_nombre: str = "",
    k: float = DEFAULT_K,
) -> dict[str, Any]:
    """Actualiza Elos tras un juego final. Idempotente por game_id."""
    if away_id is None or home_id is None:
        return {"ok": False, "motivo": "sin team_id"}
    if score_away is None or score_home is None:
        return {"ok": False, "motivo": "sin marcador"}
    try:
        sa, sh = int(score_away), int(score_home)
    except (TypeError, ValueError):
        return {"ok": False, "motivo": "marcador inválido"}
    if sa == sh:
        return {"ok": False, "motivo": "empate (no aplica MLB)"}

    data = cargar_ratings()
    gid = str(game_id or "")
    aplicados = data.setdefault("juegos_aplicados", [])
    if gid and gid in aplicados:
        return {"ok": True, "omitido": True, "motivo": "ya aplicado"}

    ka = str(int(away_id)) if str(away_id).isdigit() else str(away_id)
    kh = str(int(home_id)) if str(home_id).isdigit() else str(home_id)
    asegurar_equipo(ka, data, nombre=away_nombre)
    asegurar_equipo(kh, data, nombre=home_nombre)

    elo_a = elo_equipo(ka, data)
    elo_h = elo_equipo(kh, data)
    # Resultado: 1 = win home, 0 = win away (desde perspectiva home)
    score_h = 1.0 if sh > sa else 0.0
    score_a = 1.0 - score_h
    exp_h = expected_score(elo_h, elo_a)
    exp_a = expected_score(elo_a, elo_h)
    nuevo_h = elo_h + k * (score_h - exp_h)
    nuevo_a = elo_a + k * (score_a - exp_a)

    data["equipos"][kh] = {
        **(data["equipos"].get(kh) or {}),
        "elo": round(nuevo_h, 2),
        "n": int((data["equipos"].get(kh) or {}).get("n") or 0) + 1,
        "nombre": home_nombre or (data["equipos"].get(kh) or {}).get("nombre") or "",
    }
    data["equipos"][ka] = {
        **(data["equipos"].get(ka) or {}),
        "elo": round(nuevo_a, 2),
        "n": int((data["equipos"].get(ka) or {}).get("n") or 0) + 1,
        "nombre": away_nombre or (data["equipos"].get(ka) or {}).get("nombre") or "",
    }
    if gid:
        aplicados.append(gid)
    guardar_ratings(data)
    return {
        "ok": True,
        "home": {"antes": round(elo_h, 2), "despues": round(nuevo_h, 2)},
        "away": {"antes": round(elo_a, 2), "despues": round(nuevo_a, 2)},
        "ganador": "home" if score_h else "away",
    }


def fusionar_probs_elo(
    juego: dict[str, Any],
    prob_away: float,
    prob_home: float,
    stats_pitcher_away: dict[str, Any] | None,
    stats_pitcher_home: dict[str, Any] | None,
    cfg: dict | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """
    Mezcla % del modelo con % Elo+pitcher.
    Devuelve (prob_away, prob_home, meta).
    """
    opts = _cfg_elo(cfg)
    meta: dict[str, Any] = {
        "ok": False,
        "activo": opts["activo"],
        "peso_elo": opts["peso_elo"],
    }
    if not opts["activo"]:
        meta["motivo"] = "usar_elo=false"
        return prob_away, prob_home, meta

    away_id = juego.get("away_id") or juego.get("team_away_id")
    home_id = juego.get("home_id") or juego.get("team_home_id")
    data = cargar_ratings()

    # Semilla suave con win% si existen en el juego
    wp_a = None
    wp_h = None
    try:
        # a veces vienen en records embebidos; opcional
        wp_a = float((juego.get("win_pct_away") or 0) or 0) or None
    except (TypeError, ValueError):
        wp_a = None
    try:
        wp_h = float((juego.get("win_pct_home") or 0) or 0) or None
    except (TypeError, ValueError):
        wp_h = None

    elo_a = asegurar_equipo(
        away_id, data, win_pct=wp_a, nombre=str(juego.get("visitante") or "")
    )
    elo_h = asegurar_equipo(
        home_id, data, win_pct=wp_h, nombre=str(juego.get("home") or "")
    )
    # Persistir semillas nuevas
    guardar_ratings(data)

    pa = stats_pitcher_away if isinstance(stats_pitcher_away, dict) else {}
    ph = stats_pitcher_home if isinstance(stats_pitcher_home, dict) else {}
    adj_a = ajuste_pitcher_fip(
        pa.get("fip"),
        fip_liga=opts["fip_liga"],
        scale=opts["pitcher_scale"],
    )
    adj_h = ajuste_pitcher_fip(
        ph.get("fip"),
        fip_liga=opts["fip_liga"],
        scale=opts["pitcher_scale"],
    )

    elo_a_adj = elo_a + adj_a
    elo_h_adj = elo_h + adj_h
    elo_p_home, elo_p_away = prob_desde_elo(
        elo_h_adj, elo_a_adj, home_adv=opts["home_adv"]
    )

    w = max(0.0, min(0.8, float(opts["peso_elo"])))
    # Renormalizar por si el modelo no suma 100
    m_sum = float(prob_away) + float(prob_home)
    if m_sum <= 0:
        m_away, m_home = 50.0, 50.0
    else:
        m_away = 100.0 * float(prob_away) / m_sum
        m_home = 100.0 * float(prob_home) / m_sum

    fused_away = (1.0 - w) * m_away + w * elo_p_away
    fused_home = (1.0 - w) * m_home + w * elo_p_home
    # Normalizar a 100
    s = fused_away + fused_home
    if s > 0:
        fused_away = round(100.0 * fused_away / s, 2)
        fused_home = round(100.0 * fused_home / s, 2)

    meta.update(
        {
            "ok": True,
            "elo_away": round(elo_a, 1),
            "elo_home": round(elo_h, 1),
            "adj_pitcher_away": adj_a,
            "adj_pitcher_home": adj_h,
            "elo_adj_away": round(elo_a_adj, 1),
            "elo_adj_home": round(elo_h_adj, 1),
            "prob_elo_away": elo_p_away,
            "prob_elo_home": elo_p_home,
            "prob_modelo_away": round(m_away, 2),
            "prob_modelo_home": round(m_home, 2),
            "prob_fusion_away": fused_away,
            "prob_fusion_home": fused_home,
            "fip_away": pa.get("fip"),
            "fip_home": ph.get("fip"),
            "resumen": (
                f"Elo {round(elo_a,0):.0f}/{round(elo_h,0):.0f} "
                f"pitch {adj_a:+.0f}/{adj_h:+.0f} → "
                f"{elo_p_away:.0f}/{elo_p_home:.0f}% · "
                f"fusión {fused_away:.0f}/{fused_home:.0f}%"
            ),
        }
    )
    return fused_away, fused_home, meta


def actualizar_elo_desde_juego(juego: dict[str, Any], cfg: dict | None = None) -> dict[str, Any]:
    """Convenience: usa campos típicos del servidor/modelo."""
    if str(juego.get("estado") or "").upper() not in ("FINALIZADO", "FINAL", "F"):
        # aceptar también si hay ganador y scores
        if juego.get("ganador") is None and (
            juego.get("scoreAway") is None or juego.get("scoreHome") is None
        ):
            return {"ok": False, "motivo": "no final"}
    opts = _cfg_elo(cfg)
    return actualizar_resultado(
        away_id=juego.get("away_id"),
        home_id=juego.get("home_id"),
        score_away=juego.get("scoreAway"),
        score_home=juego.get("scoreHome"),
        game_id=str(juego.get("id") or ""),
        away_nombre=str(juego.get("visitante") or ""),
        home_nombre=str(juego.get("home") or ""),
        k=opts["k"],
    )
