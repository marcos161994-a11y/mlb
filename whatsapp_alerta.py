"""
Alertas T-60: avisa el equipo/pick elegido 1 hora antes del juego.

Canales (CallMeBot):
  1) Telegram (recomendado, fácil):
       - Abre Telegram → busca @CallMeBot_txtbot → /start
       - Env: TELEGRAM_USER=@tuusuario
  2) WhatsApp (si hay cupo del bot):
       - Env: WHATSAPP_PHONE + CALLMEBOT_APIKEY

Config:
  "telegram": { "activo": true, "user": "@tuusuario" }
  "whatsapp": { "activo": false, "phone": "", "apikey": "" }
  Preferencia: telegram si está listo; si no, WhatsApp.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

CALLMEBOT_WA_URL = "https://api.callmebot.com/whatsapp.php"
CALLMEBOT_TG_URL = "https://api.callmebot.com/text.php"


def _cfg_telegram(cfg: dict | None) -> dict[str, Any]:
    cfg = cfg or {}
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    user = (
        str(tg.get("user") or tg.get("username") or "").strip()
        or os.environ.get("TELEGRAM_USER", "").strip()
        or os.environ.get("CALLMEBOT_TELEGRAM_USER", "").strip()
    )
    if user and not user.startswith("@"):
        user = "@" + user.lstrip("@")
    activo = tg.get("activo")
    if activo is None:
        activo = bool(user)
    return {
        "activo": bool(activo),
        "user": user,
        "solo_apostables": bool(tg.get("solo_apostables", False)),
        "incluir_mente": bool(tg.get("incluir_mente", True)),
        "timeout_sec": float(tg.get("timeout_sec") or 12),
        "html": bool(tg.get("html", False)),
    }


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


def _cfg_alertas(cfg: dict | None) -> dict[str, Any]:
    """Canal activo preferido + flags comunes."""
    tg = _cfg_telegram(cfg)
    wa = _cfg_whatsapp(cfg)
    # Preferencia explícita o auto
    prefer = str(
        ((cfg or {}).get("alertas") or {}).get("canal")
        or os.environ.get("ALERTA_CANAL", "")
        or ""
    ).lower()
    if prefer in ("telegram", "tg") and tg["activo"] and tg["user"]:
        canal = "telegram"
    elif prefer in ("whatsapp", "wa") and wa["activo"] and wa["phone"] and wa["apikey"]:
        canal = "whatsapp"
    elif tg["activo"] and tg["user"]:
        canal = "telegram"
    elif wa["activo"] and wa["phone"] and wa["apikey"]:
        canal = "whatsapp"
    else:
        canal = None
    return {
        "canal": canal,
        "telegram": tg,
        "whatsapp": wa,
        "solo_apostables": bool(
            (tg.get("solo_apostables") if canal == "telegram" else wa.get("solo_apostables"))
        ),
        "incluir_mente": bool(
            (tg.get("incluir_mente") if canal == "telegram" else wa.get("incluir_mente"))
            if canal
            else True
        ),
    }


def telegram_disponible(cfg: dict | None = None) -> dict[str, Any]:
    t = _cfg_telegram(cfg)
    if not t["activo"]:
        return {"ok": False, "motivo": "telegram.activo=false"}
    if not t["user"]:
        return {"ok": False, "motivo": "Falta TELEGRAM_USER / telegram.user (@usuario)"}
    return {
        "ok": True,
        "canal": "telegram",
        "user": t["user"],
        "setup": "Telegram → @CallMeBot_txtbot → /start → TELEGRAM_USER=@tuusuario en Render",
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
        "canal": "whatsapp",
        "proveedor": w["proveedor"],
        "phone_mascara": _enmascarar_phone(w["phone"]),
    }


def alerta_disponible(cfg: dict | None = None) -> dict[str, Any]:
    a = _cfg_alertas(cfg)
    if a["canal"] == "telegram":
        return telegram_disponible(cfg)
    if a["canal"] == "whatsapp":
        return whatsapp_disponible(cfg)
    tg = telegram_disponible(cfg)
    wa = whatsapp_disponible(cfg)
    return {
        "ok": False,
        "motivo": "Sin canal listo (configura TELEGRAM_USER o WhatsApp)",
        "telegram": tg,
        "whatsapp": wa,
        "recomendado": "telegram",
    }


def _enmascarar_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 6:
        return "***"
    return f"+{digits[:3]}***{digits[-2:]}"


def _normalizar_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


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
    """Texto de alerta: equipo elegido 1h antes."""
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
    a = _cfg_alertas(cfg)
    shadow = bool(((cfg or {}).get("mente") or {}).get("shadow"))

    lineas = [
        "Quantum MLB — Pick T-60",
        f"{visitante} @ {home}",
        f"Equipo: {equipo}",
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
    if a.get("incluir_mente") and mente.get("decision"):
        lineas.append(
            f"Mente: {mente.get('decision')}"
            + (f" (conf {mente.get('confianza')})" if mente.get("confianza") else "")
        )
        razones = mente.get("razones") or []
        if razones:
            lineas.append(f"Motivo: {razones[0]}")
    if shadow:
        lineas.append("Modo shadow: aviso sin mover dinero")
    lineas.append(f"Fase: {fase}")
    return "\n".join(lineas)


def enviar_telegram(
    texto: str,
    cfg: dict | None = None,
    *,
    forzar: bool = False,
) -> dict[str, Any]:
    t = _cfg_telegram(cfg)
    if not forzar and not t["activo"]:
        return {"ok": False, "motivo": "Telegram desactivado"}
    if not t["user"]:
        return {"ok": False, "motivo": "Falta telegram.user"}

    try:
        params: dict[str, Any] = {
            "user": t["user"],
            "text": texto,
        }
        if t.get("html"):
            params["html"] = "yes"
        r = requests.get(CALLMEBOT_TG_URL, params=params, timeout=t["timeout_sec"])
        body = (r.text or "")[:240]
        low = body.lower()
        ok = r.status_code == 200 and (
            "queued" in low
            or "message" in low
            or "sent" in low
            or ("ok" in low and "error" not in low and "denied" not in low)
        )
        motivo = None
        if "permission denied" in low or "authorize" in low:
            ok = False
            motivo = (
                "Falta autorizar CallMeBot: en Telegram busca @CallMeBot_txtbot → Start, "
                "o abre https://api2.callmebot.com/txt/login.php"
            )
        if "denied" in low or "permission" in low or "invalid" in low or "error:" in low:
            ok = False
        if r.status_code == 200 and not body.strip():
            ok = True
        out = {
            "ok": bool(ok),
            "http": r.status_code,
            "canal": "telegram",
            "user": t["user"],
            "detalle": body[:140],
            "enviado_en": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if motivo:
            out["motivo"] = motivo
        return out
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120], "canal": "telegram"}


def enviar_whatsapp(
    texto: str,
    cfg: dict | None = None,
    *,
    forzar: bool = False,
) -> dict[str, Any]:
    """Envía texto por CallMeBot WhatsApp. No expone apikey."""
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
            CALLMEBOT_WA_URL,
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
            ok = True
        return {
            "ok": bool(ok),
            "http": r.status_code,
            "canal": "whatsapp",
            "proveedor": "callmebot",
            "phone_mascara": _enmascarar_phone(phone),
            "detalle": body[:120],
            "enviado_en": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120], "canal": "whatsapp"}


def enviar_alerta(
    texto: str,
    cfg: dict | None = None,
    *,
    forzar: bool = False,
) -> dict[str, Any]:
    a = _cfg_alertas(cfg)
    if a["canal"] == "telegram":
        return enviar_telegram(texto, cfg, forzar=forzar)
    if a["canal"] == "whatsapp":
        return enviar_whatsapp(texto, cfg, forzar=forzar)
    return {"ok": False, "motivo": "Sin canal configurado (Telegram o WhatsApp)"}


def ya_enviado(pred_o_juego: dict | None) -> bool:
    if not isinstance(pred_o_juego, dict):
        return False
    return bool(
        pred_o_juego.get("alerta_enviado")
        or pred_o_juego.get("whatsapp_enviado")
        or pred_o_juego.get("telegram_enviado")
    )


def marcar_enviado(pred: dict, resultado: dict[str, Any]) -> None:
    ok = bool(resultado.get("ok"))
    pred["alerta_enviado"] = ok
    pred["alerta_canal"] = resultado.get("canal")
    pred["whatsapp_enviado"] = ok  # compat
    pred["telegram_enviado"] = ok and resultado.get("canal") == "telegram"
    pred["whatsapp_enviado_en"] = resultado.get("enviado_en")
    if not ok:
        pred["whatsapp_error"] = (resultado.get("motivo") or resultado.get("detalle") or "")[:120]


def notificar_pick_t60(
    juego: dict[str, Any],
    pred: dict | None,
    cfg: dict | None,
    *,
    fase: str = "t60",
) -> dict[str, Any]:
    """
    Envía alerta (Telegram o WhatsApp) del equipo elegido si aún no se envió.
    """
    a = _cfg_alertas(cfg)
    disp = alerta_disponible(cfg)
    if not disp.get("ok"):
        return {"ok": False, "motivo": disp.get("motivo") or "no disponible", "omitido": True}

    if pred is not None and ya_enviado(pred):
        return {"ok": True, "omitido": True, "motivo": "ya enviado"}
    if ya_enviado(juego):
        return {"ok": True, "omitido": True, "motivo": "ya enviado (juego)"}

    pick = str((pred or juego).get("pick") or juego.get("pick") or "").strip()
    if not pick:
        return {"ok": False, "omitido": True, "motivo": "sin pick"}

    if a.get("solo_apostables"):
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
    resultado = enviar_alerta(texto, cfg)
    if pred is not None:
        marcar_enviado(pred, resultado)
    dest = resultado.get("user") or resultado.get("phone_mascara") or a.get("canal")
    if resultado.get("ok"):
        juego["alerta_enviado"] = True
        juego["whatsapp_enviado"] = True
        print(f"[ALERTA/{resultado.get('canal')}] Pick T-60: {pick} → {dest}")
    else:
        print(f"[ALERTA] Fallo: {resultado.get('motivo') or resultado.get('detalle')}")
    resultado["texto_preview"] = texto[:180]
    return resultado
