"""Mapa de la mente para el panel (red neuronal visual).

No decide picks: solo describe capas, conexiones y números vivos
(lecciones, WR, flags) para que se vea cómo piensa y cómo aprende.
"""

from __future__ import annotations

from typing import Any


def _on(flag: Any, default: bool = True) -> bool:
    if flag is None:
        return default
    return bool(flag)


def _wr_preds(memoria: dict | None, *, min_prob: float | None = None) -> dict[str, Any]:
    ac = fa = 0
    for dia in (memoria or {}).get("dias") or []:
        if not isinstance(dia, dict):
            continue
        for p in dia.get("predicciones") or []:
            if not isinstance(p, dict) or p.get("resultado") not in ("acierto", "fallo"):
                continue
            try:
                pr = float(p.get("probPick") or 0)
            except (TypeError, ValueError):
                pr = 0.0
            if min_prob is not None and pr < min_prob:
                continue
            if p.get("resultado") == "acierto":
                ac += 1
            else:
                fa += 1
    n = ac + fa
    return {
        "aciertos": ac,
        "fallos": fa,
        "n": n,
        "wr": round(100.0 * ac / n, 1) if n else None,
    }


def construir_mente_red(
    cfg: dict | None = None,
    memoria: dict | None = None,
    *,
    lecciones: dict | None = None,
    mente_stats: dict | None = None,
    ml_meta: dict | None = None,
    mente_errores: dict | None = None,
    bitacora: dict | None = None,
) -> dict[str, Any]:
    """Grafo listo para pintar: nodos (x,y,capa) + aristas."""
    cfg = cfg if isinstance(cfg, dict) else {}
    intel = cfg.get("inteligencia") if isinstance(cfg.get("inteligencia"), dict) else {}
    mente_cfg = cfg.get("mente") if isinstance(cfg.get("mente"), dict) else {}
    lec = lecciones if isinstance(lecciones, dict) else {}
    st = mente_stats if isinstance(mente_stats, dict) else {}
    ml = ml_meta if isinstance(ml_meta, dict) else ((memoria or {}).get("ml_meta") or {})
    me = mente_errores if isinstance(mente_errores, dict) else {}
    dec = st.get("decisiones") if isinstance(st.get("decisiones"), dict) else {}
    ap = dec.get("APOSTAR") if isinstance(dec.get("APOSTAR"), dict) else {}
    pa = dec.get("PASAR") if isinstance(dec.get("PASAR"), dict) else {}

    wr_all = _wr_preds(memoria)
    wr_alta = _wr_preds(memoria, min_prob=65.0)
    n_lec = int(lec.get("total") or len((memoria or {}).get("lecciones") or []) or 0)
    bit = bitacora if isinstance(bitacora, dict) else {}
    n_log = int(bit.get("total") or len(bit.get("entradas") or []) or 0)

    def N(
        nid: str,
        label: str,
        x: int,
        y: int,
        capa: str,
        *,
        on: bool = True,
        detalle: str = "",
        kind: str = "hidden",
    ) -> dict[str, Any]:
        return {
            "id": nid,
            "label": label,
            "x": x,
            "y": y,
            "capa": capa,
            "on": bool(on),
            "detalle": detalle,
            "kind": kind,
        }

    nodos = [
        # Sentidos
        N("mlb", "MLB", 70, 70, "sentidos", detalle="schedule + box", kind="in"),
        N("espn", "Cuotas", 70, 140, "sentidos", on=_on((cfg.get("lineas") or {}).get("proveedor")), detalle="ESPN/DK", kind="in"),
        N("clima", "Clima", 70, 210, "sentidos", on=_on(cfg.get("usar_clima", True)), detalle="Open-Meteo", kind="in"),
        N("lesion", "Lesiones", 70, 280, "sentidos", on=_on(cfg.get("usar_lesiones", True)), detalle="ESPN IL", kind="in"),
        N("scratch", "Scratch", 70, 350, "sentidos", on=_on(cfg.get("usar_scratch_lineup", True)), detalle="lineup SP", kind="in"),
        N("humanos", "Humanos", 70, 420, "sentidos", on=_on(cfg.get("usar_factores_humanos", True)), detalle="viaje/umpire", kind="in"),
        N("l10", "L10 / PvR", 70, 490, "sentidos", on=_on(cfg.get("usar_historico_oficial", True)), detalle="oficial MLB", kind="in"),
        # Cortex
        N("stats", "Stats", 250, 140, "cortex", detalle="fuerza + FIP"),
        N("ml", "RF+XGB", 250, 250, "cortex", on=_on(cfg.get("usar_ml", True)), detalle=(ml.get("mensaje") or f"n={ml.get('muestras') or 0}")[:42]),
        N("elo", "Elo", 250, 360, "cortex", on=_on(cfg.get("usar_elo", True)), detalle=f"peso {((cfg.get('elo') or {}).get('peso_elo') or 0.4)}"),
        N("calib", "Calibrar", 250, 470, "cortex", on=_on(cfg.get("usar_calibracion", True)), detalle="por tipo pick"),
        # Intel 5
        N("consenso", "Consenso", 430, 80, "intel", on=_on(intel.get("consenso_mercado", True)), detalle="odds justas"),
        N("bullpen", "Bullpen", 430, 160, "intel", on=_on(intel.get("bullpen_dia", True)), detalle="fatiga relevo"),
        N("park", "Park+Ump", 430, 240, "intel", on=_on(intel.get("park_umpire", True)), detalle="parque / zona"),
        N("tipo", "Tipo pick", 430, 320, "intel", detalle="fav / dog / scratch"),
        N("mc", "Monte Carlo", 430, 400, "intel", on=_on(intel.get("monte_carlo", True)), detalle=f"sims {intel.get('mc_sims') or 800}"),
        N("totales", "Totales/F5", 430, 480, "intel", on=_on(intel.get("monte_carlo_totales", True)), detalle="señal O/U"),
        # Mente
        N("brief", "Briefing", 610, 120, "mente", on=_on(cfg.get("usar_mente", True)), detalle="7 pilares"),
        N("reglas", "Reglas", 610, 220, "mente", on=_on(cfg.get("usar_mente", True)), detalle="veto duro"),
        N("groq", "Groq", 610, 320, "mente", on=_on(cfg.get("usar_ia_veto", True)), detalle="2º voto"),
        N("aprende", "Aprendiz.", 610, 420, "mente", detalle=f"APOSTAR {ap.get('aciertos') or 0}✓/{ap.get('fallos') or 0}✗"),
        N("ops", "Errores", 610, 520, "mente", on=_on((cfg.get("mente_errores") or {}).get("activo", True)), detalle=(me.get("mensaje") or me.get("nivel") or "ops")[:36], kind="ops"),
        # Salida
        N("papel", "Quién gana", 820, 160, "salida", detalle=(f"{wr_all['wr']}%" if wr_all["wr"] is not None else "—") + f" · {wr_all['n']} picks", kind="out"),
        N("alta", "Alta conv.", 820, 280, "salida", detalle=(f"{wr_alta['wr']}%" if wr_alta["wr"] is not None else "—") + " · ≥65%", kind="out"),
        N("dinero", "Dinero", 820, 400, "salida", detalle=f"PASAR evitó {pa.get('evito_fallo') or 0}", kind="out"),
        # Loop
        N("liq", "Liquidar", 500, 580, "loop", detalle="MLB final", kind="loop"),
        N("lecs", "Lecciones", 250, 580, "loop", detalle=f"{n_lec} en memoria", kind="loop"),
        N("log", "Bitácora", 820, 520, "loop", detalle=f"{n_log} notas", kind="loop"),
    ]

    def E(a: str, b: str, loop: bool = False) -> dict[str, Any]:
        return {"from": a, "to": b, "loop": loop}

    aristas = [
        E("mlb", "stats"),
        E("espn", "stats"),
        E("espn", "consenso"),
        E("clima", "stats"),
        E("lesion", "reglas"),
        E("scratch", "reglas"),
        E("scratch", "tipo"),
        E("humanos", "park"),
        E("humanos", "reglas"),
        E("l10", "reglas"),
        E("stats", "ml"),
        E("ml", "elo"),
        E("elo", "consenso"),
        E("elo", "mc"),
        E("consenso", "brief"),
        E("bullpen", "brief"),
        E("park", "brief"),
        E("tipo", "calib"),
        E("tipo", "brief"),
        E("mc", "brief"),
        E("totales", "brief"),
        E("calib", "brief"),
        E("brief", "reglas"),
        E("reglas", "groq"),
        E("groq", "aprende"),
        E("aprende", "papel"),
        E("aprende", "alta"),
        E("aprende", "dinero"),
        E("reglas", "dinero"),
        E("ops", "reglas"),
        E("papel", "liq", True),
        E("alta", "liq", True),
        E("dinero", "liq", True),
        E("liq", "lecs", True),
        E("lecs", "aprende", True),
        E("lecs", "ml", True),
        E("liq", "calib", True),
        E("ops", "log"),
        E("lecs", "log", True),
    ]

    return {
        "ok": True,
        "titulo": "Mente · red de decisión",
        "nodos": nodos,
        "aristas": aristas,
        "wr_todos": wr_all,
        "wr_alta": wr_alta,
        "lecciones": n_lec,
        "modo": (mente_cfg.get("modo") or "normal"),
        "shadow": bool(mente_cfg.get("shadow", False)),
        "bitacora": n_log,
        "mensaje": (
            "Aprende sola al liquidar (lecciones → veto + ML). "
            "El 75% solo vive en Alta convicción (≥65%), no en todo el slate. "
            "Abre la bitácora para ver investigación y cambios."
        ),
    }
