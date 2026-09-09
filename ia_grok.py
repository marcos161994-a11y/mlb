"""
Segundo voto con Grok (xAI) — solo para dinero.

Flujo: mente/Groq autoriza APOSTAR → Grok confirma o veta.
Si no hay XAI_API_KEY o está desactivado → no bloquea (se sigue con la mente).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

XAI_URL = "https://api.x.ai/v1/chat/completions"
# Rápido/barato para clasificación APOSTAR|PASAR; override en config → grok.model
DEFAULT_MODEL = "grok-4-1-fast"
DEFAULT_TIMEOUT = 12.0

_voto_cache: dict[str, dict[str, Any]] = {}


def _api_key(cfg: dict | None = None) -> str:
    env = (os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY") or "").strip()
    if env:
        return env
    if cfg:
        return str((cfg.get("grok") or {}).get("api_key") or "").strip()
    return ""


def modelo_grok(cfg: dict | None = None) -> str:
    grok_cfg = (cfg or {}).get("grok") if isinstance((cfg or {}).get("grok"), dict) else {}
    raw = str((grok_cfg or {}).get("model") or DEFAULT_MODEL).strip()
    return raw or DEFAULT_MODEL


def grok_segundo_voto_disponible(cfg: dict | None = None) -> bool:
    cfg = cfg or {}
    if not bool(cfg.get("usar_grok_segundo_voto", False)):
        return False
    return bool(_api_key(cfg))


def probar_conexion_grok(cfg: dict | None = None) -> dict[str, Any]:
    """Ping corto a xAI (no expone la key)."""
    cfg = cfg or {}
    key = _api_key(cfg)
    model = modelo_grok(cfg)
    if not key:
        return {"ok": False, "motivo": "Sin XAI_API_KEY", "modelo": model}
    timeout = min(float((cfg.get("grok") or {}).get("timeout_sec") or DEFAULT_TIMEOUT), 12.0)
    try:
        r = requests.post(
            XAI_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0,
                "max_tokens": 24,
                "messages": [
                    {
                        "role": "user",
                        "content": 'Responde solo JSON: {"decision":"APOSTAR","motivo":"ok","confianza":5}',
                    }
                ],
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            det = (r.text or "")[:120]
            return {
                "ok": False,
                "motivo": f"HTTP {r.status_code}" + (f" · {det}" if det else ""),
                "modelo": model,
            }
        return {"ok": True, "motivo": "Grok/xAI responde", "modelo": model}
    except requests.Timeout:
        return {"ok": False, "motivo": "Timeout", "modelo": model}
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120], "modelo": model}


def _parse_respuesta(texto: str) -> dict[str, Any] | None:
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
        except Exception:
            continue
    return None


def _debe_consultar_grok(juego: dict, mente: dict | None, cfg: dict) -> tuple[bool, str]:
    """Solo gastar Grok si la mente ya autorizó y hay edge mínimo."""
    if not grok_segundo_voto_disponible(cfg):
        return False, "desactivado_o_sin_key"
    if not isinstance(mente, dict) or not mente.get("autoriza_dinero"):
        return False, "mente_no_autoriza"
    grok_cfg = cfg.get("grok") if isinstance(cfg.get("grok"), dict) else {}
    estr = cfg.get("estrategia") if isinstance(cfg.get("estrategia"), dict) else {}
    try:
        min_edge = float(grok_cfg.get("min_edge_pct", estr.get("min_edge_pct", 8.0)))
    except (TypeError, ValueError):
        min_edge = 8.0
    try:
        edge = float(juego.get("edge") or 0)
    except (TypeError, ValueError):
        edge = 0.0
    if edge < min_edge:
        return False, f"edge<{min_edge:g}"
    return True, "ok"


def segundo_voto_dinero(
    juego: dict[str, Any],
    cfg: dict | None = None,
    *,
    mente: dict | None = None,
    memoria: dict | None = None,
) -> dict[str, Any]:
    """
    Confirma o veta un APOSTAR de la mente.

    Returns dict con ok/decision/motivo/confianza/fuente.
    Si no aplica o falla la API → ok=False y decision=SKIP (no cancela el dinero).
    Si decision=PASAR → el caller debe cancelar dinero.
    """
    cfg = cfg or {}
    gid = str(juego.get("id") or juego.get("game_id") or "")
    base: dict[str, Any] = {
        "ok": False,
        "decision": "SKIP",
        "motivo": "",
        "confianza": 0,
        "fuente": "grok",
        "modelo": modelo_grok(cfg),
    }

    consultar, por_que = _debe_consultar_grok(juego, mente, cfg)
    if not consultar:
        base["motivo"] = por_que
        base["omitido"] = True
        return base

    if gid and gid in _voto_cache:
        return dict(_voto_cache[gid])

    key = _api_key(cfg)
    model = modelo_grok(cfg)
    grok_cfg = cfg.get("grok") or {}
    timeout = float(grok_cfg.get("timeout_sec") or DEFAULT_TIMEOUT)

    pick = str(juego.get("pick") or "")
    visitante = str(juego.get("visitante") or "")
    home = str(juego.get("home") or "")
    try:
        prob = float(juego.get("probPick") or 0)
        edge = float(juego.get("edge") or 0)
        odds = float(juego.get("odds") or 0)
    except (TypeError, ValueError):
        prob, edge, odds = 0.0, 0.0, 0.0

    bloque_lecciones = "Lecciones: no disponibles."
    try:
        from ia_lecciones import texto_lecciones_para_prompt

        bloque_lecciones = texto_lecciones_para_prompt(memoria, juego=juego, cfg=cfg)
    except Exception:
        pass

    mente_txt = ""
    if isinstance(mente, dict):
        mente_txt = (
            f"Mente ya dijo APOSTAR conf={mente.get('confianza')} "
            f"razones={'; '.join(mente.get('razones') or [])[:160]}"
        )

    alertas = []
    for k in ("scratch_lineup", "lesiones", "factores_humanos", "historico_oficial"):
        info = juego.get(k) if isinstance(juego.get(k), dict) else {}
        if info.get("riesgo") or info.get("starter_riesgo"):
            alertas.append(f"{k}: {(info.get('alerta') or info.get('resumen') or 'riesgo')[:80]}")

    prompt = (
        "Eres Grok, analista senior de apuestas MLB. La MENTE del sistema YA autorizó dinero.\n"
        "Tu trabajo: SEGUNDO VOTO — confirmar (APOSTAR) o vetar (PASAR).\n"
        "Sé estricto: solo confirma si hay valor real vs cuota de mercado y el spot no es favorito inflado,\n"
        "scratch, starter dudoso, L10 fría del pick, o se parece a una lección de fallo reciente.\n"
        "Si la cuota es corta (<1.70) sin edge excepcional → PASAR.\n"
        "Si edge < 8% → PASAR.\n\n"
        f"Partido: {visitante} @ {home}\n"
        f"Pick: {pick}\n"
        f"Prob modelo: {prob:.1f}% | Edge: {edge:.1f}% | Odds: {odds:.3f}\n"
        f"Fuente cuotas: {juego.get('lineas_fuente') or '?'}\n"
        f"{mente_txt}\n"
        f"Alertas: {'; '.join(alertas) if alertas else 'ninguna'}\n"
        f"{bloque_lecciones}\n\n"
        "Responde SOLO JSON válido (sin markdown):\n"
        '{"decision":"APOSTAR"|"PASAR","motivo":"max 14 palabras","confianza":1-5}'
    )

    try:
        r = requests.post(
            XAI_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0.2,
                "max_tokens": 120,
                "messages": [
                    {
                        "role": "system",
                        "content": "Respondes solo JSON. decision debe ser APOSTAR o PASAR.",
                    },
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            base["motivo"] = f"Grok HTTP {r.status_code}"
            print(f"[GROK] Error HTTP {r.status_code}: {(r.text or '')[:200]}")
            return base

        body = r.json()
        texto = (
            ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        )
        parsed = _parse_respuesta(texto)
        if not parsed:
            base["motivo"] = "Respuesta Grok ilegible"
            print(f"[GROK] No parseable: {texto[:200]}")
            return base

        decision = str(parsed.get("decision") or "").strip().upper()
        if decision not in ("APOSTAR", "PASAR"):
            base["motivo"] = f"Decision invalida: {decision}"
            return base

        try:
            conf = int(parsed.get("confianza") or 3)
        except (TypeError, ValueError):
            conf = 3
        conf = max(1, min(5, conf))
        motivo = str(parsed.get("motivo") or decision)[:160]

        out = {
            "ok": True,
            "decision": decision,
            "motivo": motivo,
            "confianza": conf,
            "fuente": "grok",
            "modelo": model,
            "omitido": False,
        }
        if gid:
            _voto_cache[gid] = dict(out)
        print(f"[GROK] {pick}: {decision} (conf {conf}) — {motivo}")
        return out
    except requests.Timeout:
        base["motivo"] = f"Timeout Grok ({timeout}s)"
        print(f"[GROK] Timeout tras {timeout}s — se mantiene voto de la mente")
        return base
    except Exception as e:
        base["motivo"] = f"Error Grok: {e}"
        print(f"[GROK] Error: {e}")
        return base
