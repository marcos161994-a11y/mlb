"""
Helpers compartidos del aprendizaje amplio (mente, lecciones, ML, calibración).
"""

from __future__ import annotations

from typing import Any

# Señales trackeadas por la mente (contadores + penalización)
SENALES_MENTE = (
    "scratch",
    "starter_riesgo",
    "humanos",
    "sin_mercado",
    "edge_bajo",
    "favorito_alto",
    "favorito_inflado",
    "mc_over",
    "mc_under",
    "preferir_f5",
    "forma_fria",
    "pitcher_vs_rival_malo",
    "linea_en_contra",
    "linea_tarde",
    "underdog_valor",
    "limpio",
)

PATRONES_LECCION_POSITIVA = (
    "underdog_valor",
    "retry_cuota_ok",
    "mente_veto_acertado",
    "refuerzo_capas",
)

PATRONES_PROMPT_EXCLUIR_MULTIPLE = frozenset(
    {"sin_cuota_real", "mala_practica_sin_mercado"}
)
PATRONES_PROMPT_PRIORIDAD = (
    "favorito_inflado",
    "scratch_lineup",
    "starter_riesgo",
    "linea_en_contra",
    "bullpen",
    "underdog_valor",
    "veto_acertado",
    "oportunidad_perdida",
    "edge_falso",
)

TIPO_ACIERTO = "acierto_refuerzo"
PESO_DINERO = 3.0
PESO_PAPEL = 1.0
PESO_SIN_MERCADO = 0.5
UMBRAL_LINEA_EN_CONTRA_PCT = 5.0


def peso_muestra_aprendizaje(reg: dict[str, Any] | None) -> float:
    """Peso para stats mente / ML / calibración."""
    if not isinstance(reg, dict):
        return 0.0
    if reg.get("invalida_tarde") and not reg.get("aprendizaje_solo"):
        return 0.0
    if reg.get("congelado_en_gracia"):
        return 0.0
    w = PESO_DINERO if reg.get("con_dinero") or reg.get("estado") in ("ganada", "perdida") else PESO_PAPEL
    fuente = str(reg.get("lineas_fuente") or "").lower()
    if fuente in ("modelo", "", "none", "import"):
        w *= PESO_SIN_MERCADO
    return float(w)


def _prob_edge(reg: dict) -> tuple[float, float, float]:
    try:
        prob = float(reg.get("probPick") or 0)
        edge = float(reg.get("edge") or 0)
        odds = float(reg.get("odds") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0
    return prob, edge, odds


def extraer_senales_aprendizaje(
    reg: dict[str, Any],
    juego: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """Señales activas para contadores mente (predicción liquidada o juego en vivo)."""
    out: list[str] = []
    src = reg if isinstance(reg, dict) else {}
    juego = juego if isinstance(juego, dict) else {}

    mente = src.get("ia_mente") if isinstance(src.get("ia_mente"), dict) else {}
    alertas_raw = list(mente.get("alertas") or []) + list(mente.get("senales") or [])
    for a in alertas_raw:
        key = str(a).lower()
        if key in SENALES_MENTE and key not in out:
            out.append(key)

    fuente = str(
        src.get("lineas_fuente") or juego.get("lineas_fuente") or ""
    ).lower()
    if fuente in ("modelo", "", "none", "import") and "sin_mercado" not in out:
        out.append("sin_mercado")

    scratch = src.get("scratch_lineup") or juego.get("scratch_lineup") or {}
    if isinstance(scratch, dict) and scratch.get("riesgo") and "scratch" not in out:
        out.append("scratch")

    les = src.get("lesiones") or juego.get("lesiones") or {}
    if isinstance(les, dict) and les.get("starter_riesgo") and "starter_riesgo" not in out:
        out.append("starter_riesgo")

    humanos = src.get("factores_humanos") or juego.get("factores_humanos") or {}
    if isinstance(humanos, dict) and humanos.get("riesgo") and "humanos" not in out:
        out.append("humanos")

    prob, edge, odds = _prob_edge(src)
    if edge < 5 and "edge_bajo" not in out:
        out.append("edge_bajo")
    if prob >= 62 and "favorito_alto" not in out:
        out.append("favorito_alto")

    try:
        from modelo_mlb import bloqueado_favorito_inflado

        merged = {**juego, **src, "probPick": prob, "edge": edge}
        if bloqueado_favorito_inflado(merged, cfg or {})[0] and "favorito_inflado" not in out:
            out.append("favorito_inflado")
    except Exception:
        fi = ((cfg or {}).get("estrategia") or {}).get("favorito_inflado") or {}
        umbral = float(fi.get("umbral_prob", 62))
        min_e = float(fi.get("min_edge_pct", 15))
        if prob >= umbral and edge < min_e and fuente not in ("modelo", "", "none") and "favorito_inflado" not in out:
            out.append("favorito_inflado")

    intel = src.get("inteligencia") or juego.get("inteligencia") or {}
    if isinstance(intel, dict):
        tot = intel.get("totales") if isinstance(intel.get("totales"), dict) else {}
        senal = str(tot.get("señal") or tot.get("senal") or "").lower()
        if senal == "over" and "mc_over" not in out:
            out.append("mc_over")
        elif senal == "under" and "mc_under" not in out:
            out.append("mc_under")
        if intel.get("preferir_f5") and "preferir_f5" not in out:
            out.append("preferir_f5")

    if src.get("preferir_f5") or juego.get("preferir_f5"):
        if "preferir_f5" not in out:
            out.append("preferir_f5")

    mc = src.get("mc_totales") or juego.get("mc_totales") or {}
    if isinstance(mc, dict) and mc.get("ok"):
        s = str(mc.get("señal") or mc.get("senal") or "").lower()
        if s == "over" and "mc_over" not in out:
            out.append("mc_over")
        elif s == "under" and "mc_under" not in out:
            out.append("mc_under")

    hist = src.get("historico_oficial") or juego.get("historico_oficial") or {}
    if isinstance(hist, dict) and hist.get("ok"):
        pick = str(src.get("pick") or juego.get("pick") or "")
        visitante = str(src.get("visitante") or juego.get("visitante") or "")
        home = str(src.get("home") or juego.get("home") or "")
        lado = "away" if visitante and visitante in pick else ("home" if home and home in pick else None)
        if lado == "away":
            forma = str((hist.get("l10_away") or {}).get("forma") or "")
            pvr = (hist.get("pitcher_vs_rival_away") or {}).get("calidad")
        elif lado == "home":
            forma = str((hist.get("l10_home") or {}).get("forma") or "")
            pvr = (hist.get("pitcher_vs_rival_home") or {}).get("calidad")
        else:
            forma, pvr = "", None
        if forma == "fria" and "forma_fria" not in out:
            out.append("forma_fria")
        if pvr == "malo" and "pitcher_vs_rival_malo" not in out:
            out.append("pitcher_vs_rival_malo")

    mov = src.get("linea_movimiento_pct")
    if mov is not None:
        try:
            if float(mov) <= -UMBRAL_LINEA_EN_CONTRA_PCT and "linea_en_contra" not in out:
                out.append("linea_en_contra")
        except (TypeError, ValueError):
            pass

    if src.get("cuota_retry") and "linea_tarde" not in out:
        out.append("linea_tarde")

    if odds >= 2.0 and 58 <= prob <= 62 and edge >= 6 and "underdog_valor" not in out:
        out.append("underdog_valor")

    if not out:
        out.append("limpio")
    return out


def analisis_capas_inteligencia(reg: dict[str, Any]) -> dict[str, Any]:
    """Resumen de capas activas al momento del pick (post-mortem / refuerzo)."""
    intel = reg.get("inteligencia") if isinstance(reg.get("inteligencia"), dict) else {}
    elo = reg.get("elo") if isinstance(reg.get("elo"), dict) else {}
    mc = reg.get("mc_totales") if isinstance(reg.get("mc_totales"), dict) else {}
    capas = list(intel.get("capas") or [])
    tot = intel.get("totales") if isinstance(intel.get("totales"), dict) else {}
    return {
        "capas": capas,
        "mc_senal": tot.get("señal") or tot.get("senal") or mc.get("señal"),
        "elo_ok": bool(elo.get("ok")),
        "elo_resumen": (elo.get("resumen") or "")[:80],
        "intel_resumen": (intel.get("resumen") or "")[:100],
        "preferir_f5": bool(intel.get("preferir_f5") or reg.get("preferir_f5")),
    }


def calcular_movimiento_linea(reg: dict[str, Any], odds_nueva: float) -> float | None:
    """% cambio de cuota vs congelada (negativo = mercado en contra)."""
    try:
        base = float(reg.get("odds_congelada") or reg.get("odds") or 0)
        nueva = float(odds_nueva)
    except (TypeError, ValueError):
        return None
    if base <= 1.0 or nueva <= 1.0:
        return None
    return round((nueva - base) / base * 100.0, 2)


def lecciones_seleccionadas_para_prompt(
    lecciones: list[dict[str, Any]],
    max_n: int = 8,
) -> list[dict[str, Any]]:
    """Dedup por patrón, prioriza dinero/fallos, limita ruido sin_cuota."""
    items = [x for x in lecciones if isinstance(x, dict)]
    if not items:
        return []

    def score(item: dict) -> tuple[int, int, int]:
        patron = str(item.get("patron") or "otro")
        prio = len(PATRONES_PROMPT_PRIORIDAD) - PATRONES_PROMPT_PRIORIDAD.index(patron) if patron in PATRONES_PROMPT_PRIORIDAD else 0
        dinero = 1 if item.get("con_dinero") or item.get("tipo") == TIPO_ACIERTO else 0
        conf = int(item.get("confianza") or 3)
        return (dinero, prio, conf)

    vistos_patron: dict[str, int] = {}
    seleccion: list[dict] = []
    for item in reversed(items):
        patron = str(item.get("patron") or "otro")
        cnt = vistos_patron.get(patron, 0)
        max_patron = 1 if patron in PATRONES_PROMPT_EXCLUIR_MULTIPLE else 2
        if cnt >= max_patron:
            continue
        seleccion.append(item)
        vistos_patron[patron] = cnt + 1
        if len(seleccion) >= max_n * 2:
            break

    seleccion.sort(key=score, reverse=True)
    return seleccion[:max_n]


def segmento_calibracion(reg: dict[str, Any]) -> str:
    """Segmento extra para calibración (además de tipo_pick)."""
    prob, _, odds = _prob_edge(reg)
    if prob >= 62:
        return "prob_alta"
    if odds >= 2.0:
        return "underdog_cuota"
    capas = analisis_capas_inteligencia(reg)
    if capas.get("mc_senal") == "over":
        return "mc_over"
    if capas.get("preferir_f5"):
        return "entorno_f5"
    return "general"
