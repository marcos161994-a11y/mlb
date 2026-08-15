"""
Mente Quantum MLB — director inteligente sobre el modelo.

Flujo V1:
  1) Junta pilares (clima, lesiones, scratch, humanos, cuotas, ML, lecciones)
  2) Aplica reglas duras locales
  3) Pide conclusión a Groq (si hay key)
  4) Devuelve UNA decisión estructurada:
     APOSTAR | PASAR | ESPERAR + stake_pct + razones + confianza + lecciones_usadas

El dinero solo se mueve si decision=APOSTAR y confianza >= umbral del modo.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"
DEFAULT_TIMEOUT = 10.0

# Cache por game_id
_mente_cache: dict[str, dict[str, Any]] = {}

MODOS = {
    "estricto": {"min_confianza": 4, "requiere_mercado": True, "shadow": False},
    "normal": {"min_confianza": 3, "requiere_mercado": True, "shadow": False},
    "agresivo": {"min_confianza": 2, "requiere_mercado": False, "shadow": False},
    "shadow": {"min_confianza": 3, "requiere_mercado": True, "shadow": True},
}


def _api_key(cfg: dict | None = None) -> str:
    env = (os.environ.get("GROQ_API_KEY") or "").strip()
    if env:
        return env
    if cfg:
        return str((cfg.get("groq") or {}).get("api_key") or "").strip()
    return ""


def mente_disponible(cfg: dict | None = None) -> bool:
    cfg = cfg or {}
    if not cfg.get("usar_mente", True):
        return False
    # Mente local siempre puede concluir con reglas; Groq es opcional
    return True


def _modo_cfg(cfg: dict) -> dict[str, Any]:
    mente = cfg.get("mente") if isinstance(cfg.get("mente"), dict) else {}
    nombre = str(mente.get("modo") or cfg.get("mente_modo") or "normal").lower()
    base = dict(MODOS.get(nombre) or MODOS["normal"])
    if mente.get("min_confianza") is not None:
        try:
            base["min_confianza"] = int(mente["min_confianza"])
        except (TypeError, ValueError):
            pass
    if "requiere_mercado" in mente:
        base["requiere_mercado"] = bool(mente["requiere_mercado"])
    if "shadow" in mente:
        base["shadow"] = bool(mente["shadow"])
    base["nombre"] = nombre if nombre in MODOS else "normal"
    return base


def _texto_lecciones(memoria: dict | None, max_n: int = 6) -> tuple[str, list[str]]:
    ids: list[str] = []
    if not memoria:
        return "Lecciones: ninguna aún.", ids
    try:
        from ia_lecciones import texto_lecciones_para_prompt, asegurar_lista_lecciones

        txt = texto_lecciones_para_prompt(memoria, max_n=max_n)
        for item in asegurar_lista_lecciones(memoria)[-max_n:]:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, dict) and item.get("patron"):
                ids.append(str(item["patron"]))
        return txt, ids
    except ImportError:
        lec = memoria.get("lecciones") if isinstance(memoria.get("lecciones"), list) else []
        if not lec:
            return "Lecciones: ninguna aún.", ids
        lineas = []
        for i, item in enumerate(reversed([x for x in lec if isinstance(x, dict)][-max_n:]), 1):
            lineas.append(
                f"{i}. [{item.get('patron') or 'otro'}] {item.get('leccion') or item.get('motivo') or ''}"
            )
            if item.get("id"):
                ids.append(str(item["id"]))
            elif item.get("patron"):
                ids.append(str(item["patron"]))
        return "Lecciones de fallos recientes:\n" + "\n".join(lineas), ids


def construir_briefing(juego: dict[str, Any], memoria: dict | None = None) -> dict[str, Any]:
    """Junta todos los pilares en un informe corto para la mente (uso interno)."""
    clima = juego.get("clima") if isinstance(juego.get("clima"), dict) else {}
    lesiones = juego.get("lesiones") if isinstance(juego.get("lesiones"), dict) else {}
    scratch = juego.get("scratch_lineup") if isinstance(juego.get("scratch_lineup"), dict) else {}
    humanos = juego.get("factores_humanos") if isinstance(juego.get("factores_humanos"), dict) else {}
    historico = juego.get("historico_oficial") if isinstance(juego.get("historico_oficial"), dict) else {}
    feats = juego.get("ml_features") if isinstance(juego.get("ml_features"), dict) else {}

    pilares = {
        "modelo": {
            "pick": juego.get("pick"),
            "prob": juego.get("probPick"),
            "edge": juego.get("edge"),
            "odds": juego.get("odds"),
            "fuente_cuotas": juego.get("lineas_fuente") or "modelo",
            "motivo": (juego.get("motivo_apuesta") or "")[:160],
        },
        "clima": {
            "ok": bool(clima.get("ok")),
            "run_env": clima.get("run_env"),
            "motivo": (clima.get("motivo") or "")[:100],
        },
        "lesiones": {
            "ok": bool(lesiones.get("ok")),
            "starter_riesgo": bool(lesiones.get("starter_riesgo")),
            "resumen": (lesiones.get("resumen") or lesiones.get("alerta") or "")[:140],
        },
        "scratch": {
            "ok": bool(scratch.get("ok")),
            "riesgo": bool(scratch.get("riesgo")),
            "resumen": (scratch.get("resumen") or scratch.get("alerta") or "")[:140],
        },
        "humanos": {
            "ok": bool(humanos.get("ok")),
            "riesgo": bool(humanos.get("riesgo")),
            "resumen": (humanos.get("resumen") or "")[:180],
            "fatiga_away": (humanos.get("away") or {}).get("fatiga_viaje"),
            "fatiga_home": (humanos.get("home") or {}).get("fatiga_viaje"),
        },
        "historico": {
            "ok": bool(historico.get("ok")),
            "riesgo": bool(historico.get("riesgo")),
            "resumen": (historico.get("resumen") or "")[:200],
            "l10_away": (historico.get("l10_away") or {}).get("marca"),
            "l10_home": (historico.get("l10_home") or {}).get("marca"),
            "pvr_away": (historico.get("pitcher_vs_rival_away") or {}).get("calidad"),
            "pvr_home": (historico.get("pitcher_vs_rival_home") or {}).get("calidad"),
        },
        "elo": {
            "ok": bool((juego.get("elo") or {}).get("ok")),
            "resumen": str((juego.get("elo") or {}).get("resumen") or "")[:160],
            "prob_elo_away": (juego.get("elo") or {}).get("prob_elo_away"),
            "prob_elo_home": (juego.get("elo") or {}).get("prob_elo_home"),
            "adj_pitcher_away": (juego.get("elo") or {}).get("adj_pitcher_away"),
            "adj_pitcher_home": (juego.get("elo") or {}).get("adj_pitcher_home"),
            "peso_elo": (juego.get("elo") or {}).get("peso_elo"),
        },
        "inteligencia": {
            "ok": bool((juego.get("inteligencia") or {}).get("ok")),
            "capas": list((juego.get("inteligencia") or {}).get("capas") or [])[:8],
            "resumen": str((juego.get("inteligencia") or {}).get("resumen") or "")[:160],
            "tipo_pick": juego.get("tipo_pick")
            or (juego.get("inteligencia") or {}).get("tipo_pick")
            or (juego.get("inteligencia") or {}).get("tipo_pre"),
            "consenso_n": ((juego.get("inteligencia") or {}).get("consenso") or {}).get("n_fuentes"),
            "mc": str(((juego.get("inteligencia") or {}).get("monte_carlo") or {}).get("resumen") or "")[:80],
        },
        "pitchers": {
            "away": juego.get("pitcherAway"),
            "home": juego.get("pitcherHome"),
            "fip": feats.get("fip_pitcher"),
            "fatiga_bullpen": feats.get("fatiga_bullpen"),
        },
    }
    lec_txt, lec_ids = _texto_lecciones(memoria)
    alertas: list[str] = []
    if pilares["lesiones"]["starter_riesgo"]:
        alertas.append("starter_riesgo")
    if pilares["scratch"]["riesgo"]:
        alertas.append("scratch")
    if pilares["humanos"]["riesgo"]:
        alertas.append("humanos")
    if pilares["historico"]["riesgo"]:
        alertas.append("historico")
    for a in historico.get("alertas") or []:
        if a not in alertas:
            alertas.append(str(a))
    fuente = str(pilares["modelo"]["fuente_cuotas"] or "").lower()
    if fuente in ("modelo", "", "none"):
        alertas.append("sin_mercado")
    elo_p = pilares.get("elo") or {}
    if elo_p.get("ok"):
        try:
            pick = str(juego.get("pick") or "")
            home = str(juego.get("home") or "")
            elo_raw = juego.get("elo") if isinstance(juego.get("elo"), dict) else {}
            if home and home in pick:
                modelo_lado = float(elo_raw.get("prob_modelo_home") or juego.get("probPick") or 0)
                elo_lado = float(elo_p.get("prob_elo_home") or 0)
            else:
                modelo_lado = float(elo_raw.get("prob_modelo_away") or juego.get("probPick") or 0)
                elo_lado = float(elo_p.get("prob_elo_away") or 0)
            if abs(modelo_lado - elo_lado) >= 12:
                alertas.append("elo_discrepa")
        except (TypeError, ValueError):
            pass
    intel_p = pilares.get("inteligencia") or {}
    if intel_p.get("ok") and intel_p.get("capas"):
        cons_n = intel_p.get("consenso_n") or 0
        try:
            disc = float(
                ((juego.get("inteligencia") or {}).get("consenso") or {}).get(
                    "discrepancia_casas_pct"
                )
                or 0
            )
        except (TypeError, ValueError):
            disc = 0.0
        if cons_n >= 2 and disc >= 6:
            alertas.append("mercado_dividido")
        if intel_p.get("tipo_pick") == "scratch":
            alertas.append("tipo_scratch")

    resumen_bits = [
        f"Pick {juego.get('pick') or '?'} @ {juego.get('probPick')}% edge={juego.get('edge')}",
        f"cuotas={fuente or 'n/a'}",
    ]
    if elo_p.get("ok") and elo_p.get("resumen"):
        resumen_bits.append(str(elo_p["resumen"])[:90])
    if intel_p.get("ok") and intel_p.get("resumen"):
        resumen_bits.append(str(intel_p["resumen"])[:90])
    if alertas:
        resumen_bits.append("alertas=" + ",".join(alertas[:8]))
    if humanos.get("resumen"):
        resumen_bits.append(str(humanos["resumen"])[:80])
    if historico.get("resumen"):
        resumen_bits.append(str(historico["resumen"])[:100])

    return {
        "ok": True,
        "pilares": pilares,
        "alertas": alertas,
        "lecciones_txt": lec_txt,
        "lecciones_ids": lec_ids,
        "resumen": " · ".join(resumen_bits)[:320],
    }


def compactar_briefing_para_memoria(
    briefing: dict[str, Any],
    *,
    fase: str = "t60",
) -> dict[str, Any]:
    """
    Versión persistible del briefing (T-60 / bloqueo).
    Uso interno de la mente — no se expone en el panel.
    """
    from datetime import datetime, timezone

    return {
        "ok": bool(briefing.get("ok")),
        "fase": fase,
        "creado_en": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "alertas": list(briefing.get("alertas") or [])[:12],
        "resumen": str(briefing.get("resumen") or "")[:320],
        "pilares": briefing.get("pilares") if isinstance(briefing.get("pilares"), dict) else {},
        "lecciones_ids": list(briefing.get("lecciones_ids") or [])[:8],
        # lecciones_txt se regenera al decidir (memoria puede haber crecido)
    }


def generar_briefing_juego(
    juego: dict[str, Any],
    memoria: dict | None = None,
    *,
    fase: str = "t60",
) -> dict[str, Any]:
    """Construye y compacta briefing; lo deja en juego['ia_briefing']."""
    full = construir_briefing(juego, memoria)
    compacto = compactar_briefing_para_memoria(full, fase=fase)
    juego["ia_briefing"] = compacto
    return compacto


def _briefing_para_decision(
    juego: dict[str, Any],
    memoria: dict | None,
) -> dict[str, Any]:
    """
    Prefiere briefing congelado en T-60/bloqueo si existe;
    refresca solo el texto de lecciones actuales.
    """
    frozen = juego.get("ia_briefing") if isinstance(juego.get("ia_briefing"), dict) else None
    if frozen and frozen.get("ok"):
        briefing = {
            "ok": True,
            "pilares": frozen.get("pilares") if isinstance(frozen.get("pilares"), dict) else {},
            "alertas": list(frozen.get("alertas") or []),
            "lecciones_ids": list(frozen.get("lecciones_ids") or []),
            "resumen": frozen.get("resumen") or "",
            "fase": frozen.get("fase") or "t60",
            "creado_en": frozen.get("creado_en"),
        }
        lec_txt, lec_ids = _texto_lecciones(memoria)
        briefing["lecciones_txt"] = lec_txt
        if lec_ids and not briefing["lecciones_ids"]:
            briefing["lecciones_ids"] = lec_ids
        return briefing
    return construir_briefing(juego, memoria)


def _lado_del_pick(juego: dict) -> str | None:
    pick = str(juego.get("pick") or "")
    visitante = str(juego.get("visitante") or "")
    home = str(juego.get("home") or "")
    if visitante and visitante in pick:
        return "away"
    if home and home in pick:
        return "home"
    return None


def _reglas_duras(juego: dict, briefing: dict, modo: dict) -> dict[str, Any] | None:
    """
    Si dispara, devuelve conclusión inmediata (sin Groq).
    """
    lesiones = juego.get("lesiones") if isinstance(juego.get("lesiones"), dict) else {}
    scratch = juego.get("scratch_lineup") if isinstance(juego.get("scratch_lineup"), dict) else {}
    humanos = juego.get("factores_humanos") if isinstance(juego.get("factores_humanos"), dict) else {}
    pick = str(juego.get("pick") or "")
    visitante = str(juego.get("visitante") or "")
    home = str(juego.get("home") or "")
    lado = _lado_del_pick(juego)

    if lesiones.get("starter_riesgo"):
        if (lesiones.get("starter_away_lesionado") and visitante in pick) or (
            lesiones.get("starter_home_lesionado") and home in pick
        ):
            return _pack(
                "PASAR",
                0,
                ["Starter del pick en riesgo"],
                5,
                [],
                fuente="regla-local",
                briefing=briefing,
            )

    try:
        from lineup_scratch import pick_afectado_por_scratch

        if scratch.get("riesgo") and pick_afectado_por_scratch(pick, visitante, home, scratch):
            return _pack(
                "PASAR",
                0,
                ["Scratch/lineup debilita el pick"],
                5,
                [],
                fuente="regla-local",
                briefing=briefing,
            )
    except ImportError:
        if scratch.get("riesgo"):
            return _pack(
                "PASAR",
                0,
                ["Scratch/lineup con riesgo"],
                4,
                [],
                fuente="regla-local",
                briefing=briefing,
            )

    if lado and isinstance(humanos.get(lado), dict):
        fatiga = float((humanos[lado] or {}).get("fatiga_viaje") or 0)
        if fatiga >= 0.75:
            return _pack(
                "PASAR",
                0,
                [f"Fatiga de viaje extrema ({lado}={fatiga:.2f})"],
                4,
                [],
                fuente="regla-local",
                briefing=briefing,
            )

    # Historial oficial: forma fría del pick o SP castigado vs rival
    historico = juego.get("historico_oficial") if isinstance(juego.get("historico_oficial"), dict) else {}
    if historico.get("ok"):
        try:
            edge_h = float(juego.get("edge") or 0)
        except (TypeError, ValueError):
            edge_h = 0.0
        l10_key = "l10_away" if lado == "away" else ("l10_home" if lado == "home" else None)
        pvr_key = (
            "pitcher_vs_rival_away"
            if lado == "away"
            else ("pitcher_vs_rival_home" if lado == "home" else None)
        )
        if l10_key:
            forma = str((historico.get(l10_key) or {}).get("forma") or "")
            marca = (historico.get(l10_key) or {}).get("marca")
            if forma == "fria" and edge_h < 10:
                return _pack(
                    "PASAR",
                    0,
                    [f"Pick en L10 fría ({marca or '≤2-8'})"],
                    4,
                    ["forma_fria"],
                    fuente="regla-local",
                    briefing=briefing,
                )
        if pvr_key:
            pvr = historico.get(pvr_key) if isinstance(historico.get(pvr_key), dict) else {}
            if pvr.get("calidad") == "malo" and edge_h < 12:
                return _pack(
                    "PASAR",
                    0,
                    [f"SP del pick malo vs rival ({(pvr.get('motivo') or '')[:50]})"],
                    4,
                    ["pitcher_vs_rival"],
                    fuente="regla-local",
                    briefing=briefing,
                )

    fuente = str(juego.get("lineas_fuente") or "modelo").lower()
    sin_mercado = fuente in ("modelo", "", "none", "import")
    if modo.get("requiere_mercado") and sin_mercado:
        return _pack(
            "PASAR",
            0,
            ["Sin cuota real de mercado"],
            5,
            ["sin_cuota_real"],
            fuente="regla-local",
            briefing=briefing,
        )

    try:
        prob = float(juego.get("probPick") or 0)
        edge = float(juego.get("edge") or 0)
    except (TypeError, ValueError):
        prob, edge = 0.0, 0.0
    if not pick or prob < 52:
        return _pack(
            "ESPERAR",
            0,
            ["Pick o probabilidad insuficiente"],
            3,
            [],
            fuente="regla-local",
            briefing=briefing,
        )
    if edge < 3 and not sin_mercado:
        return _pack(
            "PASAR",
            0,
            ["Edge demasiado bajo vs mercado"],
            4,
            ["edge_falso"],
            fuente="regla-local",
            briefing=briefing,
        )
    return None


def _pack(
    decision: str,
    stake_pct: float,
    razones: list[str],
    confianza: int,
    lecciones_usadas: list[str],
    *,
    fuente: str,
    briefing: dict | None = None,
    modelo: str | None = None,
) -> dict[str, Any]:
    decision = str(decision or "PASAR").upper()
    if decision not in ("APOSTAR", "PASAR", "ESPERAR"):
        decision = "PASAR"
    conf = max(1, min(5, int(confianza or 3)))
    razones = [str(r)[:80] for r in (razones or []) if r][:4]
    if not razones:
        razones = [decision]
    stake = float(stake_pct or 0)
    if decision != "APOSTAR":
        stake = 0.0
    stake = max(0.0, min(5.0, stake))
    return {
        "ok": True,
        "decision": decision,
        "stake_pct": round(stake, 2),
        "razones": razones,
        "confianza": conf,
        "lecciones_usadas": list(lecciones_usadas or [])[:8],
        "fuente": fuente,
        "modelo": modelo,
        "briefing": None,  # interno; no se expone en panel
        "alertas": (briefing or {}).get("alertas") if briefing else [],
    }


def _parse_json_mente(texto: str) -> dict[str, Any] | None:
    raw = (texto or "").strip()
    if not raw:
        return None
    candidatos = [raw]
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        candidatos.insert(0, m.group(0))
    for c in candidatos:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _conclusion_groq(
    juego: dict,
    briefing: dict,
    cfg: dict,
    lec_ids: list[str],
) -> dict[str, Any] | None:
    key = _api_key(cfg)
    if not key:
        return None
    groq_cfg = cfg.get("groq") or {}
    model = str(groq_cfg.get("model") or DEFAULT_MODEL)
    timeout = min(float(groq_cfg.get("timeout_sec") or DEFAULT_TIMEOUT), 14.0)

    prompt = (
        "Eres la MENTE de un sistema de apuestas MLB. El modelo ya propuso un pick.\n"
        "Debes concluir UNA decisión con dinero en mente: APOSTAR, PASAR o ESPERAR.\n"
        "Sé estricto: sin cuota real, scratch del pick, starter en riesgo, fatiga alta, "
        "L10 fría del pick, pitcher malo vs rival o parecido a lecciones de fallos → PASAR. "
        "Edge sólido + contexto limpio + forma/historial a favor → APOSTAR.\n"
        "ESPERAR solo si faltan datos clave (pitcher TBD, lineup vacío).\n\n"
        f"Partido: {juego.get('visitante')} @ {juego.get('home')}\n"
        f"Pick: {juego.get('pick')} | prob={juego.get('probPick')} edge={juego.get('edge')} "
        f"odds={juego.get('odds')} fuente={juego.get('lineas_fuente')}\n"
        f"Pitchers: {juego.get('pitcherAway')} vs {juego.get('pitcherHome')}\n"
        f"Briefing: {briefing.get('resumen')}\n"
        f"Alertas: {', '.join(briefing.get('alertas') or []) or 'ninguna'}\n"
        f"{briefing.get('lecciones_txt')}\n\n"
        "Responde SOLO JSON:\n"
        '{"decision":"APOSTAR"|"PASAR"|"ESPERAR","stake_pct":0-5,'
        '"razones":["max 3","cortas"],"confianza":1-5,"lecciones_usadas":["id o patron"]}'
    )
    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.15,
                "max_tokens": 180,
                "messages": [
                    {
                        "role": "system",
                        "content": "Respondes solo JSON válido de decisión de apuestas MLB.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"[MENTE] Groq HTTP {r.status_code}")
            return None
        texto = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        parsed = _parse_json_mente(texto)
        if not parsed:
            return None
        razones = parsed.get("razones")
        if isinstance(razones, str):
            razones = [razones]
        if not isinstance(razones, list):
            razones = []
        usadas = parsed.get("lecciones_usadas") or []
        if not isinstance(usadas, list):
            usadas = []
        if not usadas:
            usadas = lec_ids[:3]
        try:
            stake = float(parsed.get("stake_pct") or 0)
        except (TypeError, ValueError):
            stake = 0.0
        try:
            conf = int(parsed.get("confianza") or 3)
        except (TypeError, ValueError):
            conf = 3
        return _pack(
            str(parsed.get("decision") or "PASAR"),
            stake,
            [str(x) for x in razones],
            conf,
            [str(x) for x in usadas],
            fuente="groq",
            briefing=briefing,
            modelo=model,
        )
    except Exception as e:
        print(f"[MENTE] Error Groq: {e}")
        return None


def _scratch_afecta_pick(juego: dict) -> bool:
    """Solo cuenta scratch/estrellas del lado del pick (no del rival)."""
    scratch = juego.get("scratch_lineup") if isinstance(juego.get("scratch_lineup"), dict) else {}
    if not scratch.get("riesgo"):
        return False
    pick = str(juego.get("pick") or "")
    visitante = str(juego.get("visitante") or "")
    home = str(juego.get("home") or "")
    try:
        from lineup_scratch import pick_afectado_por_scratch

        return bool(pick_afectado_por_scratch(pick, visitante, home, scratch))
    except ImportError:
        return True


def _starter_riesgo_del_pick(juego: dict) -> bool:
    lesiones = juego.get("lesiones") if isinstance(juego.get("lesiones"), dict) else {}
    if not lesiones.get("starter_riesgo"):
        return False
    pick = str(juego.get("pick") or "")
    visitante = str(juego.get("visitante") or "")
    home = str(juego.get("home") or "")
    if lesiones.get("starter_away_lesionado") and visitante and visitante in pick:
        return True
    if lesiones.get("starter_home_lesionado") and home and home in pick:
        return True
    return False


def _heuristica_conclusion(juego: dict, briefing: dict, modo: dict) -> dict[str, Any]:
    """Fallback sin Groq: decide con señales locales."""
    try:
        edge = float(juego.get("edge") or 0)
        prob = float(juego.get("probPick") or 0)
    except (TypeError, ValueError):
        edge, prob = 0.0, 0.0
    alertas = briefing.get("alertas") or []
    lec_ids = briefing.get("lecciones_ids") or []

    # Scratch/starter del RIVAL no debe tumbar el pick.
    if _scratch_afecta_pick(juego) or _starter_riesgo_del_pick(juego):
        return _pack(
            "PASAR",
            0,
            ["Alerta de roster/scratch en el lado del pick"],
            4,
            lec_ids[:2],
            fuente="heuristica",
            briefing=briefing,
        )
    if "humanos" in alertas and edge < 8:
        return _pack("PASAR", 0, ["Contexto humano feo y edge justo"], 3, lec_ids[:2], fuente="heuristica", briefing=briefing)

    lado = _lado_del_pick(juego)
    contra_pick = False
    a_favor = False
    if lado == "away":
        contra_pick = any(a in alertas for a in ("l10_fria_away", "pvr_malo_away"))
        a_favor = any(a in alertas for a in ("l10_caliente_away", "pvr_bueno_away", "l10_fria_home", "pvr_malo_home"))
    elif lado == "home":
        contra_pick = any(a in alertas for a in ("l10_fria_home", "pvr_malo_home"))
        a_favor = any(a in alertas for a in ("l10_caliente_home", "pvr_bueno_home", "l10_fria_away", "pvr_malo_away"))

    if contra_pick and edge < 10:
        return _pack(
            "PASAR",
            0,
            ["Historial oficial en contra del pick"],
            4,
            ["historico_oficial"],
            fuente="heuristica",
            briefing=briefing,
        )
    if "sin_mercado" in alertas and modo.get("requiere_mercado"):
        return _pack("PASAR", 0, ["Sin mercado"], 5, ["sin_cuota_real"], fuente="heuristica", briefing=briefing)

    conf_bonus = 1 if a_favor else 0

    if edge >= 6 and prob >= 55 and "sin_mercado" not in alertas:
        stake = 2.0 if edge < 8 else (3.0 if edge < 12 else 4.0)
        conf = 3 if edge < 8 else (4 if edge < 12 else 5)
        conf = min(5, conf + conf_bonus)
        return _pack(
            "APOSTAR",
            stake,
            [f"Edge +{edge:.1f}% con contexto limpio", f"Prob {prob:.0f}%"],
            conf,
            lec_ids[:2],
            fuente="heuristica",
            briefing=briefing,
        )
    if edge >= 4 and prob >= 58:
        return _pack(
            "ESPERAR",
            0,
            ["Spot marginal: esperar mejor precio o confirmación"],
            2,
            lec_ids[:1],
            fuente="heuristica",
            briefing=briefing,
        )
    return _pack(
        "PASAR",
        0,
        ["No hay valor claro"],
        3,
        lec_ids[:1],
        fuente="heuristica",
        briefing=briefing,
    )


def mente_conclusion(
    juego: dict[str, Any],
    cfg: dict | None = None,
    memoria: dict | None = None,
    *,
    forzar: bool = False,
    solo_local: bool = False,
) -> dict[str, Any]:
    """
    Conclusión única de la mente para un juego.
    solo_local=True: no llama a Groq (útil para refresco del panel).
    """
    cfg = cfg or {}
    gid = str(juego.get("id") or juego.get("game_id") or "")
    if gid and gid in _mente_cache and not forzar:
        return dict(_mente_cache[gid])

    if not cfg.get("usar_mente", True):
        out = {
            "ok": False,
            "decision": "ESPERAR",
            "stake_pct": 0,
            "razones": ["Mente desactivada"],
            "confianza": 0,
            "lecciones_usadas": [],
            "fuente": "off",
            "autoriza_dinero": False,
        }
        return out

    modo = _modo_cfg(cfg)
    briefing = _briefing_para_decision(juego, memoria)
    # Señales activas del briefing (+ extras)
    senales = list(briefing.get("alertas") or [])
    try:
        edge = float(juego.get("edge") or 0)
        prob = float(juego.get("probPick") or 0)
    except (TypeError, ValueError):
        edge, prob = 0.0, 0.0
    if edge < 5 and "edge_bajo" not in senales:
        senales.append("edge_bajo")
    if prob >= 62 and "favorito_alto" not in senales:
        senales.append("favorito_alto")
    if not senales:
        senales.append("limpio")

    # Inyectar texto de aprendizaje en briefing para Groq
    try:
        from mente_aprendizaje import texto_aprendizaje_para_prompt

        aprendizaje_txt = texto_aprendizaje_para_prompt(memoria)
        briefing["lecciones_txt"] = (
            (briefing.get("lecciones_txt") or "") + "\n" + aprendizaje_txt
        ).strip()
    except Exception:
        pass

    dura = _reglas_duras(juego, briefing, modo)
    if dura:
        out = dura
    else:
        out = None
        if not solo_local:
            out = _conclusion_groq(juego, briefing, cfg, briefing.get("lecciones_ids") or [])
        if not out:
            out = _heuristica_conclusion(juego, briefing, modo)

    # V2: ajustar confianza / suavizar PASAR soft según contadores
    try:
        from mente_aprendizaje import aplicar_aprendizaje_a_conclusion

        out = aplicar_aprendizaje_a_conclusion(out, memoria, senales)
    except Exception as e:
        print(f"[MENTE] aprendizaje aviso: {e}")
        out["senales"] = senales

    out["modo"] = modo["nombre"]
    out["min_confianza"] = modo["min_confianza"]
    out["shadow"] = bool(modo.get("shadow"))
    out["briefing_fase"] = briefing.get("fase")
    # No filtrar briefing resumen a clientes: se quita en el panel (fusionar).
    # ¿Autoriza dinero?
    autoriza = (
        out.get("decision") == "APOSTAR"
        and int(out.get("confianza") or 0) >= int(modo["min_confianza"])
        and not modo.get("shadow")
    )
    out["autoriza_dinero"] = bool(autoriza)
    if out.get("decision") == "APOSTAR" and not autoriza:
        razones = list(out.get("razones") or [])
        if modo.get("shadow"):
            razones.append("Modo shadow: decide sin mover dinero")
            out["dinero_bloqueado_por"] = "shadow"
        else:
            razones.append(
                f"Conf {out.get('confianza')} < mínimo {modo['min_confianza']}"
            )
            out["dinero_bloqueado_por"] = "confianza"
        out["razones"] = razones

    if gid and not solo_local:
        _mente_cache[gid] = dict(out)
    if not solo_local:
        print(
            f"[MENTE] {juego.get('pick')}: {out.get('decision')} "
            f"conf={out.get('confianza')} dinero={'sí' if out.get('autoriza_dinero') else 'no'} "
            f"({out.get('fuente')}) pen={out.get('penalizacion_aprendizaje')} "
            f"brief={out.get('briefing_fase')}"
        )
    return out


def aplicar_stake_mente(
    capital: float,
    conclusion: dict,
    cfg: dict | None = None,
    stake_base: float | None = None,
) -> float:
    """Traduce stake_pct (0-5) a dólares, acotado por config."""
    cfg = cfg or {}
    mente = cfg.get("mente") if isinstance(cfg.get("mente"), dict) else {}
    pct = float(conclusion.get("stake_pct") or 0)
    if pct <= 0:
        return float(stake_base or cfg.get("stake_por_juego") or 3.0)
    min_pct = float(mente.get("min_stake_pct") or (cfg.get("estrategia") or {}).get("min_stake_pct") or 1.0)
    max_pct = float(mente.get("max_stake_pct") or (cfg.get("estrategia") or {}).get("max_stake_pct") or 5.0)
    pct = max(min_pct, min(max_pct, pct))
    stake = round(float(capital) * (pct / 100.0), 2)
    # No por debajo de 1$ ni por encima del capital
    return max(1.0, min(float(capital), stake))
