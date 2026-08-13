"""
Lecciones de experiencia (post-mortem IA) para la memoria del experimento.

Al liquidar un fallo, se guarda una lección estructurada.
Esas lecciones se reinyectan en el veto IA para que "aprenda".
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"
MAX_LECCIONES = 80
MAX_PARA_PROMPT = 8

PATRONES = (
    "favorito_inflado",
    "sin_cuota_real",
    "scratch_lineup",
    "starter_riesgo",
    "bullpen",
    "clima_park",
    "edge_falso",
    "underdog_trampa",
    "otro",
)


def _api_key(cfg: dict | None = None) -> str:
    env = (os.environ.get("GROQ_API_KEY") or "").strip()
    if env:
        return env
    if cfg:
        return str((cfg.get("groq") or {}).get("api_key") or "").strip()
    return ""


def asegurar_lista_lecciones(memoria: dict) -> list:
    lec = memoria.get("lecciones")
    if not isinstance(lec, list):
        memoria["lecciones"] = []
    return memoria["lecciones"]


def resumen_lecciones(memoria: dict, limite: int = 12) -> dict[str, Any]:
    lec = asegurar_lista_lecciones(memoria)
    por_patron: dict[str, int] = {}
    for item in lec:
        if not isinstance(item, dict):
            continue
        p = str(item.get("patron") or "otro")
        por_patron[p] = por_patron.get(p, 0) + 1
    recientes = [x for x in lec if isinstance(x, dict)][-limite:]
    recientes = list(reversed(recientes))
    return {
        "total": len(lec),
        "por_patron": por_patron,
        "recientes": recientes,
    }


def texto_lecciones_para_prompt(memoria: dict | None, max_n: int = MAX_PARA_PROMPT) -> str:
    if not memoria:
        return "Lecciones previas: ninguna aún."
    lec = [x for x in asegurar_lista_lecciones(memoria) if isinstance(x, dict)]
    if not lec:
        return "Lecciones previas: ninguna aún."
    ultimas = lec[-max_n:]
    lineas = []
    for i, item in enumerate(reversed(ultimas), 1):
        lineas.append(
            f"{i}. [{item.get('patron') or 'otro'}] {item.get('leccion') or item.get('motivo') or ''}"
            f" (pick={item.get('pick') or '?'}, {item.get('fecha') or ''})"
        )
    return "Lecciones de fallos recientes (aplícalas si el spot se parece):\n" + "\n".join(lineas)


def _ya_existe_leccion(memoria: dict, game_id: str | None, pick: str | None) -> bool:
    if not game_id and not pick:
        return False
    for item in asegurar_lista_lecciones(memoria):
        if not isinstance(item, dict):
            continue
        if game_id and str(item.get("game_id") or "") == str(game_id):
            return True
    return False


def _heuristica_leccion(pred: dict, juego: dict | None = None) -> dict[str, Any]:
    """Fallback sin Groq: clasifica el fallo con señales locales."""
    motivo = str(pred.get("motivo_apuesta") or "").lower()
    fuente = str(pred.get("lineas_fuente") or (juego or {}).get("lineas_fuente") or "modelo")
    lesiones = pred.get("lesiones") if isinstance(pred.get("lesiones"), dict) else {}
    scratch = pred.get("scratch_lineup") if isinstance(pred.get("scratch_lineup"), dict) else {}
    edge = pred.get("edge")
    try:
        edge_f = float(edge) if edge is not None else 0.0
    except (TypeError, ValueError):
        edge_f = 0.0

    patron = "otro"
    leccion = "Revisar edge real vs cuota y contexto de roster antes de dinero."
    if fuente in ("modelo", "", "None", "none") or "sin mercado" in motivo or "solo stats" in motivo:
        patron = "sin_cuota_real"
        leccion = "No apostar con dinero si no hay cuota de mercado (Pinnacle/DK)."
    elif scratch.get("riesgo") or "scratch" in motivo:
        patron = "scratch_lineup"
        leccion = "PASAR si scratch/lineup debilita el lado del pick."
    elif lesiones.get("starter_riesgo") or "starter" in motivo or "lesion" in motivo:
        patron = "starter_riesgo"
        leccion = "PASAR si el abridor del pick está en riesgo o lesionado."
    elif edge_f > 0 and edge_f < 6:
        patron = "edge_falso"
        leccion = "Exigir edge >= 6% contra cuota real; edge bajo no basta."
    elif "bullpen" in motivo or float((pred.get("ml_features") or {}).get("fatiga_bullpen") or 0) >= 0.7:
        patron = "bullpen"
        leccion = "Desconfiar de favoritos con bullpen cargado / fatiga alta."
    elif float(pred.get("probPick") or 0) >= 60:
        patron = "favorito_inflado"
        leccion = "Favorito con % alto también falla; validar vs mercado y matchup."

    return {
        "patron": patron,
        "leccion": leccion,
        "motivo": f"Heurística post-fallo ({patron})",
        "confianza": 3,
        "fuente": "heuristica",
    }


def _parse_json_leccion(texto: str) -> dict[str, Any] | None:
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


def _postmortem_groq(pred: dict, cfg: dict, juego: dict | None = None) -> dict[str, Any] | None:
    key = _api_key(cfg)
    if not key:
        return None
    groq_cfg = cfg.get("groq") or {}
    model = str(groq_cfg.get("model") or DEFAULT_MODEL)
    timeout = min(float(groq_cfg.get("timeout_sec") or 8), 12.0)

    visitante = pred.get("visitante") or (juego or {}).get("visitante") or "?"
    home = pred.get("home") or (juego or {}).get("home") or "?"
    pick = pred.get("pick") or "?"
    marcador = pred.get("marcador_final") or ""
    feats = pred.get("ml_features") if isinstance(pred.get("ml_features"), dict) else {}
    patrones = ", ".join(PATRONES)

    prompt = (
        "Eres coach de apuestas MLB. El pick FALLÓ. Extrae UNA lección accionable.\n"
        f"Partido: {visitante} @ {home}\n"
        f"Pick: {pick}\n"
        f"Prob: {pred.get('probPick')}% edge={pred.get('edge')} odds={pred.get('odds')}\n"
        f"Fuente cuotas: {pred.get('lineas_fuente') or 'desconocida'}\n"
        f"Pitchers: {pred.get('pitcherAway')} vs {pred.get('pitcherHome')}\n"
        f"Marcador: {marcador}\n"
        f"Motivo original: {pred.get('motivo_apuesta') or 'n/a'}\n"
        f"Features clave: era={feats.get('era_pitcher')} fip={feats.get('fip_pitcher')} "
        f"bullpen={feats.get('fatiga_bullpen')} run_env={feats.get('run_env')}\n"
        f"Patrón debe ser uno de: {patrones}\n"
        "Responde SOLO JSON:\n"
        '{"patron":"...","leccion":"max 18 palabras","motivo":"max 12 palabras","confianza":1-5}'
    )
    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.2,
                "max_tokens": 140,
                "messages": [
                    {"role": "system", "content": "Respondes solo JSON válido."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"[LECCIONES] Groq HTTP {r.status_code}")
            return None
        texto = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        parsed = _parse_json_leccion(texto)
        if not parsed:
            return None
        patron = str(parsed.get("patron") or "otro").strip().lower()
        if patron not in PATRONES:
            patron = "otro"
        leccion = str(parsed.get("leccion") or "").strip()[:160]
        if not leccion:
            return None
        try:
            conf = int(parsed.get("confianza") or 3)
        except (TypeError, ValueError):
            conf = 3
        return {
            "patron": patron,
            "leccion": leccion,
            "motivo": str(parsed.get("motivo") or "post-mortem")[:120],
            "confianza": max(1, min(5, conf)),
            "fuente": "groq",
            "modelo": model,
        }
    except Exception as e:
        print(f"[LECCIONES] Error Groq: {e}")
        return None


def registrar_leccion_desde_fallo(
    memoria: dict,
    pred: dict,
    cfg: dict | None = None,
    juego: dict | None = None,
    cuando: str | None = None,
) -> dict[str, Any] | None:
    """
    Crea y guarda una lección si el pick falló.
    Idempotente por game_id.
    """
    cfg = cfg or {}
    if pred.get("resultado") != "fallo":
        return None
    if pred.get("valida_stats") is False or pred.get("invalida_tarde"):
        return None

    game_id = pred.get("game_id")
    if _ya_existe_leccion(memoria, game_id, pred.get("pick")):
        return None

    detalle = _postmortem_groq(pred, cfg, juego) if cfg.get("usar_ia_veto", True) else None
    if not detalle:
        detalle = _heuristica_leccion(pred, juego)

    item = {
        "id": f"lec-{game_id or 'x'}-{int(datetime.utcnow().timestamp())}",
        "fecha": cuando or datetime.utcnow().strftime("%Y-%m-%d"),
        "game_id": game_id,
        "visitante": pred.get("visitante"),
        "home": pred.get("home"),
        "pick": pred.get("pick"),
        "probPick": pred.get("probPick"),
        "edge": pred.get("edge"),
        "lineas_fuente": pred.get("lineas_fuente"),
        "marcador_final": pred.get("marcador_final"),
        "patron": detalle.get("patron") or "otro",
        "leccion": detalle.get("leccion") or "",
        "motivo": detalle.get("motivo") or "",
        "confianza": detalle.get("confianza") or 3,
        "fuente": detalle.get("fuente") or "heuristica",
        "modelo": detalle.get("modelo"),
        "creada_en": datetime.utcnow().isoformat() + "Z",
    }

    lista = asegurar_lista_lecciones(memoria)
    lista.append(item)
    if len(lista) > MAX_LECCIONES:
        memoria["lecciones"] = lista[-MAX_LECCIONES:]
    print(f"[LECCIONES] + {item['patron']}: {item['leccion']}")
    return item


def procesar_lecciones_de_dia(
    memoria: dict,
    dia: dict,
    cfg: dict | None = None,
    juegos_por_id: dict | None = None,
) -> int:
    """Genera lecciones para fallos del día aún no registrados."""
    cfg = cfg or {}
    juegos_por_id = juegos_por_id or {}
    n = 0
    fecha = dia.get("fecha")
    for pred in dia.get("predicciones") or []:
        if not isinstance(pred, dict) or pred.get("resultado") != "fallo":
            continue
        juego = juegos_por_id.get(str(pred.get("game_id") or ""))
        if registrar_leccion_desde_fallo(memoria, pred, cfg, juego, cuando=fecha):
            n += 1
    return n


def backfill_lecciones_si_vacio(memoria: dict, max_dias: int = 21) -> int:
    """
    Si aún no hay lecciones, genera heurísticas para fallos liquidados.
    Evita llamadas a Groq (solo patrón local). Idempotente: marca flag en memoria.
    """
    if memoria.get("lecciones_backfill_hecho"):
        return 0
    lec = asegurar_lista_lecciones(memoria)
    if lec:
        memoria["lecciones_backfill_hecho"] = True
        return 0

    # Solo heurística: no gastar cuota Groq en histórico.
    cfg_h = {"usar_ia_veto": False}
    n = 0
    dias = list(memoria.get("dias") or [])[-max_dias:]
    for dia in dias:
        if not isinstance(dia, dict):
            continue
        n += procesar_lecciones_de_dia(memoria, dia, cfg=cfg_h)
    memoria["lecciones_backfill_hecho"] = True
    return n
