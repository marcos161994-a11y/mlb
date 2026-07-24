"""
Veto de apuestas con Groq (gratis, API key).

Flujo: el modelo propone → Groq dice APOSTAR o PASAR → solo entonces dinero.
Si no hay key, timeout o error → no bloquea el sistema (sigue el modelo).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"
DEFAULT_TIMEOUT = 8.0

# Cache por game_id para no spamear la API en el mismo ciclo
_veto_cache: dict[str, dict[str, Any]] = {}


def _api_key(cfg: dict | None = None) -> str:
    env = (os.environ.get("GROQ_API_KEY") or "").strip()
    if env:
        return env
    if cfg:
        return str((cfg.get("groq") or {}).get("api_key") or "").strip()
    return ""


def ia_veto_disponible(cfg: dict | None = None) -> bool:
    cfg = cfg or {}
    if not cfg.get("usar_ia_veto", False):
        return False
    return bool(_api_key(cfg))


def _parse_respuesta(texto: str) -> dict[str, Any] | None:
    raw = (texto or "").strip()
    if not raw:
        return None
    # Intentar JSON directo o dentro de fences
    candidatos = [raw]
    m = re.search(r"\{[^{}]*\}", raw, re.S)
    if m:
        candidatos.insert(0, m.group(0))
    for c in candidatos:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    # Fallback: buscar APOSTAR/PASAR en texto
    up = raw.upper()
    if "PASAR" in up and "APOSTAR" not in up.split("PASAR")[0][-20:]:
        # si dice PASAR claramente
        if re.search(r"\bPASAR\b", up):
            return {"decision": "PASAR", "motivo": raw[:160], "confianza": 3}
    if re.search(r"\bAPOSTAR\b", up):
        return {"decision": "APOSTAR", "motivo": raw[:160], "confianza": 3}
    return None


def veto_apuesta(juego: dict[str, Any], cfg: dict | None = None) -> dict[str, Any]:
    """
    Decide APOSTAR o PASAR sobre un pick ya propuesto por el modelo.

    Returns:
        {
          ok, decision ('APOSTAR'|'PASAR'|'SKIP'),
          motivo, confianza (1-5), fuente, modelo
        }
    """
    cfg = cfg or {}
    gid = str(juego.get("id") or juego.get("game_id") or "")
    if gid and gid in _veto_cache:
        return dict(_veto_cache[gid])

    base = {
        "ok": False,
        "decision": "SKIP",
        "motivo": "",
        "confianza": 0,
        "fuente": "ninguna",
        "modelo": None,
    }

    if not cfg.get("usar_ia_veto", False):
        base["motivo"] = "IA veto desactivada"
        return base

    key = _api_key(cfg)
    if not key:
        base["motivo"] = "Sin GROQ_API_KEY"
        return base

    groq_cfg = cfg.get("groq") or {}
    model = str(groq_cfg.get("model") or DEFAULT_MODEL)
    timeout = float(groq_cfg.get("timeout_sec") or DEFAULT_TIMEOUT)

    pick = (juego.get("pick") or "").strip()
    prob = float(juego.get("probPick") or 0)
    edge = juego.get("edge")
    visitante = juego.get("visitante") or "?"
    home = juego.get("home") or "?"
    pa = juego.get("pitcherAway") or "TBD"
    ph = juego.get("pitcherHome") or "TBD"
    motivo_modelo = juego.get("motivo_apuesta") or ""

    prompt = (
        "Eres analista de apuestas MLB. El MODELO ya propuso un pick.\n"
        "Tu trabajo: confirmar (APOSTAR) o vetar (PASAR) esa apuesta con dinero.\n"
        "Veta si hay riesgo claro: pitcher dudoso, bullpen fundido, lineup débil, "
        "favorito inflado, spot feo, o confianza baja pese al %.\n"
        "Confirma si el spot se ve sólido con los datos dados.\n\n"
        f"Partido: {visitante} @ {home}\n"
        f"Pick del modelo: {pick}\n"
        f"Prob modelo: {prob:.1f}%\n"
        f"Edge/conf: {edge}\n"
        f"Pitchers: away={pa} | home={ph}\n"
        f"Motivo modelo: {motivo_modelo}\n\n"
        "Responde SOLO un JSON válido (sin markdown) con exactamente:\n"
        '{"decision":"APOSTAR"|"PASAR","motivo":"max 12 palabras","confianza":1-5}'
    )

    try:
        r = requests.post(
            GROQ_URL,
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
            base["motivo"] = f"Groq HTTP {r.status_code}"
            print(f"[IA-VETO] Error HTTP {r.status_code}: {r.text[:200]}")
            return base

        body = r.json()
        texto = (
            ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        )
        parsed = _parse_respuesta(texto)
        if not parsed:
            base["motivo"] = "Respuesta IA ilegible"
            print(f"[IA-VETO] No parseable: {texto[:200]}")
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
            "fuente": "groq",
            "modelo": model,
        }
        if gid:
            _veto_cache[gid] = out
        print(f"[IA-VETO] {pick}: {decision} (conf {conf}) — {motivo}")
        return out
    except requests.Timeout:
        base["motivo"] = f"Timeout Groq ({timeout}s)"
        print(f"[IA-VETO] Timeout tras {timeout}s — se sigue con el modelo")
        return base
    except Exception as e:
        base["motivo"] = f"Error Groq: {e}"
        print(f"[IA-VETO] Error: {e}")
        return base
