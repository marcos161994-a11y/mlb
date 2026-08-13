"""
Aprendizaje V2 de la mente: contadores por patrón + feedback al liquidar.

La mente baja/sube confianza según historial real:
- PASAR + pick ganó → oportunidad perdida (patrón falló al vetar)
- PASAR + pick falló → veto acertado
- APOSTAR + falló → patrón castigado
- APOSTAR + acertó → patrón reforzado
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Señales que la mente puede trackear (alineadas con briefing.alertas)
SENALES = (
    "scratch",
    "starter_riesgo",
    "humanos",
    "sin_mercado",
    "edge_bajo",
    "favorito_alto",
    "limpio",
)


def asegurar_mente_stats(memoria: dict) -> dict:
    stats = memoria.get("mente_stats")
    if not isinstance(stats, dict):
        stats = {}
        memoria["mente_stats"] = stats
    stats.setdefault(
        "decisiones",
        {
            "APOSTAR": {"n": 0, "aciertos": 0, "fallos": 0},
            "PASAR": {"n": 0, "evito_fallo": 0, "oportunidad_perdida": 0},
            "ESPERAR": {"n": 0, "aciertos": 0, "fallos": 0},
        },
    )
    stats.setdefault("patrones", {})
    for s in SENALES:
        stats["patrones"].setdefault(
            s,
            {
                "n": 0,
                "pasar_ok": 0,
                "pasar_miss": 0,
                "apostar_ok": 0,
                "apostar_fail": 0,
            },
        )
    stats.setdefault("penalizacion", {})  # señal → entero -2..+2
    return stats


def _bucket_decision(pred: dict) -> str:
    """Qué decidió la mente (o veto legacy) para este pick."""
    mente = pred.get("ia_mente") if isinstance(pred.get("ia_mente"), dict) else {}
    if mente.get("decision"):
        d = str(mente.get("decision") or "").upper()
        if d in ("APOSTAR", "PASAR", "ESPERAR"):
            # Si dijo APOSTAR pero no autorizó dinero, cuenta como PASAR/ESPERAR de banca
            if d == "APOSTAR" and mente.get("autoriza_dinero") is False:
                return "PASAR"
            return d
    veto = pred.get("ia_veto") if isinstance(pred.get("ia_veto"), dict) else {}
    if str(veto.get("decision") or "").upper() == "PASAR":
        return "PASAR"
    if str(veto.get("decision") or "").upper() == "APOSTAR":
        return "APOSTAR"
    motivo = str(pred.get("motivo_apuesta") or "").upper()
    if "MENTE PASAR" in motivo or "IA PASAR" in motivo or "MENTE ESPERAR" in motivo:
        if "MENTE ESPERAR" in motivo:
            return "ESPERAR"
        return "PASAR"
    if pred.get("con_dinero"):
        return "APOSTAR"
    return "ESPERAR"


def senales_de_pred(pred: dict, juego: dict | None = None) -> list[str]:
    """Extrae señales activas del pick (alertas mente / contexto)."""
    out: list[str] = []
    mente = pred.get("ia_mente") if isinstance(pred.get("ia_mente"), dict) else {}
    for a in mente.get("alertas") or []:
        a = str(a).lower()
        if a in SENALES and a not in out:
            out.append(a)
    # Inferir de contexto si no hay alertas guardadas
    fuente = str(pred.get("lineas_fuente") or (juego or {}).get("lineas_fuente") or "").lower()
    if fuente in ("modelo", "", "none", "import") and "sin_mercado" not in out:
        out.append("sin_mercado")
    scratch = pred.get("scratch_lineup") or (juego or {}).get("scratch_lineup") or {}
    if isinstance(scratch, dict) and scratch.get("riesgo") and "scratch" not in out:
        out.append("scratch")
    les = pred.get("lesiones") or (juego or {}).get("lesiones") or {}
    if isinstance(les, dict) and les.get("starter_riesgo") and "starter_riesgo" not in out:
        out.append("starter_riesgo")
    humanos = pred.get("factores_humanos") or (juego or {}).get("factores_humanos") or {}
    if isinstance(humanos, dict) and humanos.get("riesgo") and "humanos" not in out:
        out.append("humanos")
    try:
        edge = float(pred.get("edge") or 0)
        prob = float(pred.get("probPick") or 0)
    except (TypeError, ValueError):
        edge, prob = 0.0, 0.0
    if edge < 5 and "edge_bajo" not in out:
        out.append("edge_bajo")
    if prob >= 62 and "favorito_alto" not in out:
        out.append("favorito_alto")
    if not out:
        out.append("limpio")
    return out


def _recalcular_penalizaciones(stats: dict) -> None:
    """
    penalizacion[señal]:
      negativo → la mente debe ser más cauta al APOSTAR / menos agresiva al PASAR soft
      positivo → el veto en esa señal ha funcionado
    """
    pen: dict[str, int] = {}
    for senal, p in (stats.get("patrones") or {}).items():
        if not isinstance(p, dict):
            continue
        n_pasar = int(p.get("pasar_ok") or 0) + int(p.get("pasar_miss") or 0)
        n_ap = int(p.get("apostar_ok") or 0) + int(p.get("apostar_fail") or 0)
        score = 0
        if n_pasar >= 4:
            miss = int(p.get("pasar_miss") or 0) / n_pasar
            ok = int(p.get("pasar_ok") or 0) / n_pasar
            if miss >= 0.55:
                score -= 1
            if miss >= 0.7:
                score -= 1
            if ok >= 0.6:
                score += 1
        if n_ap >= 4:
            fail = int(p.get("apostar_fail") or 0) / n_ap
            if fail >= 0.55:
                score -= 1
            if fail >= 0.7:
                score -= 1
            if int(p.get("apostar_ok") or 0) / n_ap >= 0.6:
                score += 1
        pen[senal] = max(-2, min(2, score))
    stats["penalizacion"] = pen
    stats["actualizado_en"] = datetime.utcnow().isoformat() + "Z"


def actualizar_stats_tras_liquidar(
    memoria: dict,
    pred: dict,
    juego: dict | None = None,
) -> dict[str, Any] | None:
    """Actualiza contadores mente tras un pick liquidado. Idempotente por game_id."""
    if pred.get("estado") != "liquidado":
        return None
    if pred.get("resultado") not in ("acierto", "fallo"):
        return None
    if pred.get("invalida_tarde") and not pred.get("aprendizaje_solo"):
        return None

    stats = asegurar_mente_stats(memoria)
    vistos = stats.setdefault("vistos", [])
    if not isinstance(vistos, list):
        vistos = []
        stats["vistos"] = vistos
    gid = str(pred.get("game_id") or "")
    key = f"{gid}:{pred.get('resultado')}"
    if gid and key in vistos:
        return None

    decision = _bucket_decision(pred)
    resultado = pred.get("resultado")
    dec = stats["decisiones"].setdefault(
        decision, {"n": 0, "aciertos": 0, "fallos": 0, "evito_fallo": 0, "oportunidad_perdida": 0}
    )
    dec["n"] = int(dec.get("n") or 0) + 1

    if decision == "PASAR":
        if resultado == "fallo":
            dec["evito_fallo"] = int(dec.get("evito_fallo") or 0) + 1
            outcome = "pasar_ok"
        else:
            dec["oportunidad_perdida"] = int(dec.get("oportunidad_perdida") or 0) + 1
            outcome = "pasar_miss"
    elif decision == "APOSTAR":
        if resultado == "acierto":
            dec["aciertos"] = int(dec.get("aciertos") or 0) + 1
            outcome = "apostar_ok"
        else:
            dec["fallos"] = int(dec.get("fallos") or 0) + 1
            outcome = "apostar_fail"
    else:  # ESPERAR
        if resultado == "acierto":
            dec["aciertos"] = int(dec.get("aciertos") or 0) + 1
            outcome = "pasar_miss"  # perdimos valor al esperar
        else:
            dec["fallos"] = int(dec.get("fallos") or 0) + 1
            outcome = "pasar_ok"

    senales = senales_de_pred(pred, juego)
    for s in senales:
        p = stats["patrones"].setdefault(
            s,
            {"n": 0, "pasar_ok": 0, "pasar_miss": 0, "apostar_ok": 0, "apostar_fail": 0},
        )
        p["n"] = int(p.get("n") or 0) + 1
        p[outcome] = int(p.get(outcome) or 0) + 1

    if gid:
        vistos.append(key)
        if len(vistos) > 400:
            stats["vistos"] = vistos[-400:]

    _recalcular_penalizaciones(stats)
    print(
        f"[MENTE-APRENDIZAJE] {decision} → {resultado} señales={senales} "
        f"pen={ {k: stats['penalizacion'].get(k) for k in senales} }"
    )
    return {"decision": decision, "resultado": resultado, "senales": senales}


def penalizacion_senales(memoria: dict | None, senales: list[str]) -> int:
    """Suma de penalizaciones de señales activas (-4..+4 aprox)."""
    if not memoria:
        return 0
    stats = asegurar_mente_stats(memoria)
    pen = stats.get("penalizacion") or {}
    total = 0
    for s in senales:
        try:
            total += int(pen.get(s) or 0)
        except (TypeError, ValueError):
            pass
    return max(-4, min(4, total))


def aplicar_aprendizaje_a_conclusion(
    conclusion: dict,
    memoria: dict | None,
    senales: list[str],
) -> dict:
    """
    Ajusta confianza / suaviza PASAR soft según historial de patrones.
    No toca reglas duras de seguridad (scratch SP / starter) — solo conf y ESPERAR.
    """
    if not conclusion.get("ok"):
        return conclusion
    pen = penalizacion_senales(memoria, senales)
    conclusion = dict(conclusion)
    conclusion["senales"] = list(senales)
    conclusion["penalizacion_aprendizaje"] = pen

    conf = int(conclusion.get("confianza") or 3)
    decision = str(conclusion.get("decision") or "").upper()
    fuente = str(conclusion.get("fuente") or "")

    # APOSTAR con patrones castigados → bajar confianza (puede bloquear dinero)
    if decision == "APOSTAR" and pen < 0:
        conf = max(1, conf + pen)  # pen negativo
        conclusion["razones"] = list(conclusion.get("razones") or []) + [
            f"Aprendizaje: señales {','.join(senales[:3])} castigadas ({pen})"
        ]

    # PASAR soft (heurística/groq) con muchos misses → ESPERAR en vez de vetar de más
    if decision == "PASAR" and pen <= -1 and fuente in ("heuristica", "groq"):
        # Si el patrón falla al PASAR (oportunidades perdidas), no vetar tan duro
        pasar_miss_heavy = False
        if memoria:
            stats = asegurar_mente_stats(memoria)
            for s in senales:
                p = (stats.get("patrones") or {}).get(s) or {}
                n = int(p.get("pasar_ok") or 0) + int(p.get("pasar_miss") or 0)
                if n >= 4 and int(p.get("pasar_miss") or 0) / n >= 0.55:
                    pasar_miss_heavy = True
                    break
        if pasar_miss_heavy and "scratch" not in senales and "starter_riesgo" not in senales:
            conclusion["decision"] = "ESPERAR"
            conclusion["stake_pct"] = 0
            conf = max(1, conf - 1)
            conclusion["razones"] = list(conclusion.get("razones") or []) + [
                "Aprendizaje: este patrón pierde valor al PASAR → ESPERAR"
            ]

    # PASAR que históricamente acierta → subir conf
    if decision == "PASAR" and pen > 0:
        conf = min(5, conf + min(1, pen))

    conclusion["confianza"] = conf
    return conclusion


def texto_aprendizaje_para_prompt(memoria: dict | None, max_n: int = 6) -> str:
    if not memoria:
        return "Aprendizaje mente: sin historial."
    stats = asegurar_mente_stats(memoria)
    pen = stats.get("penalizacion") or {}
    lineas = []
    for s, v in sorted(pen.items(), key=lambda kv: abs(int(kv[1] or 0)), reverse=True):
        if not v:
            continue
        p = (stats.get("patrones") or {}).get(s) or {}
        lineas.append(
            f"- {s}: penalización {v} "
            f"(pasar_ok={p.get('pasar_ok', 0)} miss={p.get('pasar_miss', 0)} "
            f"ap_ok={p.get('apostar_ok', 0)} ap_fail={p.get('apostar_fail', 0)})"
        )
        if len(lineas) >= max_n:
            break
    dec = stats.get("decisiones") or {}
    ap = dec.get("APOSTAR") or {}
    pa = dec.get("PASAR") or {}
    resumen = (
        f"Mente APOSTAR {ap.get('aciertos', 0)}✓/{ap.get('fallos', 0)}✗ · "
        f"PASAR evitó {pa.get('evito_fallo', 0)} / perdió {pa.get('oportunidad_perdida', 0)}"
    )
    if not lineas:
        return f"Aprendizaje mente: {resumen}. Sin penalizaciones aún."
    return f"Aprendizaje mente: {resumen}\n" + "\n".join(lineas)


def resumen_mente_stats(memoria: dict) -> dict[str, Any]:
    stats = asegurar_mente_stats(memoria)
    return {
        "decisiones": stats.get("decisiones") or {},
        "penalizacion": stats.get("penalizacion") or {},
        "patrones": stats.get("patrones") or {},
        "actualizado_en": stats.get("actualizado_en"),
    }


def recomputar_stats_desde_historial(memoria: dict, max_dias: int = 60) -> int:
    """Backfill: recorre predicciones liquidadas y arma contadores (idempotente)."""
    stats = asegurar_mente_stats(memoria)
    # Reset suave para recompute limpio
    stats["decisiones"] = {
        "APOSTAR": {"n": 0, "aciertos": 0, "fallos": 0},
        "PASAR": {"n": 0, "evito_fallo": 0, "oportunidad_perdida": 0},
        "ESPERAR": {"n": 0, "aciertos": 0, "fallos": 0},
    }
    stats["patrones"] = {
        s: {"n": 0, "pasar_ok": 0, "pasar_miss": 0, "apostar_ok": 0, "apostar_fail": 0}
        for s in SENALES
    }
    stats["vistos"] = []
    n = 0
    dias = list(memoria.get("dias") or [])[-max_dias:]
    for dia in dias:
        for pred in dia.get("predicciones") or []:
            if not isinstance(pred, dict):
                continue
            if actualizar_stats_tras_liquidar(memoria, pred):
                n += 1
    return n
