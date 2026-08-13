"""
Alertas WhatsApp (T-60): avisa el equipo/pick elegido 1 hora antes del juego.

Proveedor por defecto: CallMeBot (uso personal).
  1) Agregar el bot de CallMeBot en WhatsApp
  2) Enviar: I allow callmebot to send me messages
  3) Guardar el apikey y tu teléfono (con código país) en config o env

Env:
  WHATSAPP_PHONE=+1787...
  CALLMEBOT_APIKEY=123456
  (o WHATSAPP_APIKEY)

Config (config_experimento.json → "whatsapp"):
  activo, phone, apikey, proveedor=callmebot
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
import requests

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


def _cfg_whatsapp(cfg: dict | None) -> dict[str, Any]:
    cfg = cfg or {}
    wa = cfg.get("whatsapp") if isinstance(cfg.get("whatsapp"), dict) else {}
    phone = (
        str(wa.get("phone") or "").strip()
        or os.environ.get("WHATSAPP_PHONE", "").strip()
        or os.environ.get("CALLMEBOT_PHONE", "").strip()
    )
    apikey = (
        str(wa.get("apikey") or wa.get("api_key") or "").strip()
        or os.environ.get("CALLMEBOT_APIKEY", "").strip()
        or os.environ.get("WHATSAPP_APIKEY", "").strip()
    )
    activo = wa.get("activo")
    if activo is None:
        activo = bool(phone and apikey)
    return {
        "activo": bool(activo),
        "proveedor": str(wa.get("proveedor") or "callmebot").lower(),
        "phone": phone,
        "apikey": apikey,
        "solo_apostables": bool(wa.get("solo_apostables", False)),
        "incluir_mente": bool(wa.get("incluir_mente", True)),
        "timeout_sec": float(wa.get("timeout_sec") or 12),
    }


def whatsapp_disponible(cfg: dict | None = None) -> dict[str, Any]:
    w = _cfg_whatsapp(cfg)
    if not w["activo"]:
        return {"ok": False, "motivo": "whatsapp.activo=false"}
    if not w["phone"]:
        return {"ok": False, "motivo": "Falta WHATSAPP_PHONE / whatsapp.phone"}
    if not w["apikey"]:
        return {"ok": False, "motivo": "Falta CALLMEBOT_APIKEY / whatsapp.apikey"}
    return {
        "ok": True,
        "proveedor": w["proveedor"],
        "phone_mascara": _enmascarar_phone(w["phone"]),
    }


def _enmascarar_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 6:
        return "***"
    return f"+{digits[:3]}***{digits[-2:]}"


def _normalizar_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits  # CallMeBot acepta con o sin +; usamos dígitos


def equipo_del_pick(pick: str, visitante: str = "", home: str = "") -> str:
    """Extrae el nombre del equipo del pick (sin ML/F5)."""
    raw = str(pick or "").strip()
    if not raw:
        return "?"
    limpio = re.sub(r"\s+(ML|F5|Moneyline|Spread|Total)\s*$", "", raw, flags=re.I).strip()
    if visitante and visitante in limpio:
        return visitante
    if home and home in limpio:
        return home
    return limpio or raw


def formatear_mensaje_pick(
    juego: dict[str, Any],
    *,
    cfg: dict | None = None,
    fase: str = "t60",
) -> str:
    """Texto WhatsApp: equipo elegido 1h antes."""
    visitante = str(juego.get("visitante") or "?")
    home = str(juego.get("home") or "?")
    pick = str(juego.get("pick") or "").strip()
    equipo = equipo_del_pick(pick, visitante, home)
    try:
        prob = float(juego.get("probPick") or 0)
    except (TypeError, ValueError):
        prob = 0.0
    try:
        edge = float(juego.get("edge") or 0)
    except (TypeError, ValueError):
        edge = 0.0
    odds = juego.get("odds")
    amer = juego.get("odds_american")
    hora = juego.get("hora_inicio_txt") or ""
    if not hora and juego.get("inicio_juego"):
        try:
            dt = datetime.fromisoformat(str(juego["inicio_juego"]).replace("Z", "+00:00"))
            hora = dt.strftime("%I:%M %p")
        except Exception:
            hora = str(juego.get("inicio_juego"))[:16]

    mente = juego.get("ia_mente") if isinstance(juego.get("ia_mente"), dict) else {}
    wa = _cfg_whatsapp(cfg)
    shadow = bool(((cfg or {}).get("mente") or {}).get("shadow"))

    lineas = [
        "*Quantum MLB — Pick T-60*",
        f"{visitante} @ {home}",
        f"Equipo: *{equipo}*",
    ]
    if pick and pick != equipo:
        lineas.append(f"Mercado: {pick}")
    if prob:
        lineas.append(f"Prob: {prob:.0f}%")
    if edge:
        lineas.append(f"Edge: +{edge:.1f}%")
    if odds:
        cuota = f"{float(odds):.2f}"
        if amer is not None:
            try:
                cuota += f" ({int(amer):+d})"
            except (TypeError, ValueError):
                pass
        lineas.append(f"Cuota: {cuota}")
    if hora:
        lineas.append(f"Inicio: {hora}")
    if wa.get("incluir_mente") and mente.get("decision"):
        lineas.append(
            f"Mente: {mente.get('decision')}"
            + (f" (conf {mente.get('confianza')})" if mente.get("confianza") else "")
        )
        razones = mente.get("razones") or []
        if razones:
            lineas.append(f"Motivo: {razones[0]}")
    if shadow:
        lineas.append("_Modo shadow: aviso sin mover dinero_")
    lineas.append(f"Fase: {fase}")
    return "\n".join(lineas)


def enviar_whatsapp(
    texto: str,
    cfg: dict | None = None,
    *,
    forzar: bool = False,
) -> dict[str, Any]:
    """Envía texto por CallMeBot. No expone apikey en la respuesta."""
    w = _cfg_whatsapp(cfg)
    if not forzar and not w["activo"]:
        return {"ok": False, "motivo": "WhatsApp desactivado"}
    if not w["phone"] or not w["apikey"]:
        return {"ok": False, "motivo": "Falta phone o apikey"}

    phone = _normalizar_phone(w["phone"])
    if w["proveedor"] not in ("callmebot", "callme", "whatsapp"):
        return {"ok": False, "motivo": f"Proveedor no soportado: {w['proveedor']}"}

    try:
        r = requests.get(
            CALLMEBOT_URL,
            params={
                "source": "quantummlb",
                "phone": phone,
                "text": texto,
                "apikey": w["apikey"],
            },
            timeout=w["timeout_sec"],
        )
        body = (r.text or "")[:200]
        low = body.lower()
        ok = r.status_code == 200 and (
            "queued" in low
            or "message to" in low
            or ("ok" in low and "invalid" not in low)
        )
        if "invalid" in low or ("error" in low and "queued" not in low):
            ok = False
        if r.status_code == 200 and not body.strip():
            ok = True  # algunos entornos responden vacío al encolar
        return {
            "ok": bool(ok),
            "http": r.status_code,
            "proveedor": "callmebot",
            "phone_mascara": _enmascarar_phone(phone),
            "detalle": body[:120],
            "enviado_en": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120]}


def ya_enviado(pred_o_juego: dict | None) -> bool:
    if not isinstance(pred_o_juego, dict):
        return False
    return bool(pred_o_juego.get("whatsapp_enviado"))


def marcar_enviado(pred: dict, resultado: dict[str, Any]) -> None:
    pred["whatsapp_enviado"] = bool(resultado.get("ok"))
    pred["whatsapp_enviado_en"] = resultado.get("enviado_en")
    if not resultado.get("ok"):
        pred["whatsapp_error"] = (resultado.get("motivo") or resultado.get("detalle") or "")[:120]


def notificar_pick_t60(
    juego: dict[str, Any],
    pred: dict | None,
    cfg: dict | None,
    *,
    fase: str = "t60",
) -> dict[str, Any]:
    """
    Envía WhatsApp del equipo elegido si aún no se envió.
    Marca la predicción para no repetir.
    """
    w = _cfg_whatsapp(cfg)
    disp = whatsapp_disponible(cfg)
    if not disp.get("ok"):
        return {"ok": False, "motivo": disp.get("motivo") or "no disponible", "omitido": True}

    if pred is not None and ya_enviado(pred):
        return {"ok": True, "omitido": True, "motivo": "ya enviado"}
    if ya_enviado(juego):
        return {"ok": True, "omitido": True, "motivo": "ya enviado (juego)"}

    pick = str((pred or juego).get("pick") or juego.get("pick") or "").strip()
    if not pick:
        return {"ok": False, "omitido": True, "motivo": "sin pick"}

    if w.get("solo_apostables"):
        apostable = bool((pred or {}).get("apostable") if pred else juego.get("apostable"))
        if not apostable:
            return {"ok": False, "omitido": True, "motivo": "no apostable"}

    payload = dict(juego)
    if pred:
        for k in ("pick", "probPick", "edge", "odds", "odds_american", "ia_mente", "inicio_juego"):
            if pred.get(k) is not None and not payload.get(k):
                payload[k] = pred.get(k)
        if pred.get("ia_mente"):
            payload["ia_mente"] = pred["ia_mente"]

    texto = formatear_mensaje_pick(payload, cfg=cfg, fase=fase)
    resultado = enviar_whatsapp(texto, cfg)
    if pred is not None:
        marcar_enviado(pred, resultado)
    if resultado.get("ok"):
        juego["whatsapp_enviado"] = True
        print(f"[WHATSAPP] Enviado pick T-60: {pick} → {resultado.get('phone_mascara')}")
    else:
        print(f"[WHATSAPP] Fallo: {resultado.get('motivo') or resultado.get('detalle')}")
    resultado["texto_preview"] = texto[:180]
    return resultado
