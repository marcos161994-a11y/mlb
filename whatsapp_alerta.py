"""
Alertas T-60: avisa el equipo/pick elegido 1 hora antes del juego.

Canal recomendado — Telegram Bot oficial (BotFather):
  1) Telegram → @BotFather → /newbot → copia el token
  2) Render: TELEGRAM_BOT_TOKEN=...
  3) Abre TU bot y mándale "hola"
  4) GET /api/telegram-vincular  (guarda tu chat_id solo)
  5) POST /api/telegram-test

Fallback CallMeBot (a menudo falla por permisos):
  TELEGRAM_USER=@usuario + autorizar en callmebot

WhatsApp opcional: WHATSAPP_PHONE + CALLMEBOT_APIKEY
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

CALLMEBOT_WA_URL = "https://api.callmebot.com/whatsapp.php"
CALLMEBOT_TG_URL = "https://api.callmebot.com/text.php"
TG_API = "https://api.telegram.org"


def _data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent)))


def _chat_id_file() -> Path:
    return _data_dir() / "telegram_chat_id.txt"


def leer_chat_id_guardado() -> str:
    p = _chat_id_file()
    try:
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""


def guardar_chat_id(chat_id: str | int) -> None:
    _data_dir().mkdir(parents=True, exist_ok=True)
    _chat_id_file().write_text(str(chat_id).strip(), encoding="utf-8")


def _cfg_telegram(cfg: dict | None) -> dict[str, Any]:
    cfg = cfg or {}
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    token = (
        str(tg.get("bot_token") or tg.get("token") or "").strip()
        or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or os.environ.get("TELEGRAM_TOKEN", "").strip()
    )
    chat_id = (
        str(tg.get("chat_id") or "").strip()
        or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        or leer_chat_id_guardado()
    )
    user = (
        str(tg.get("user") or tg.get("username") or "").strip()
        or os.environ.get("TELEGRAM_USER", "").strip()
        or os.environ.get("CALLMEBOT_TELEGRAM_USER", "").strip()
    )
    if user and not user.startswith("@"):
        user = "@" + user.lstrip("@")
    modo = "bot" if token else ("callmebot" if user else "ninguno")
    activo = tg.get("activo")
    if activo is None:
        activo = bool(token or user)
    return {
        "activo": bool(activo),
        "modo": modo,
        "bot_token": token,
        "chat_id": chat_id,
        "user": user,
        "solo_apostables": bool(tg.get("solo_apostables", False)),
        "incluir_mente": bool(tg.get("incluir_mente", True)),
        "timeout_sec": float(tg.get("timeout_sec") or 12),
        "html": bool(tg.get("html", False)),
    }


def _telegram_listo(t: dict[str, Any]) -> bool:
    if not t.get("activo"):
        return False
    if t.get("modo") == "bot":
        return bool(t.get("bot_token") and t.get("chat_id"))
    if t.get("modo") == "callmebot":
        return bool(t.get("user"))
    return False


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
    tg = _cfg_telegram(cfg)
    wa = _cfg_whatsapp(cfg)
    prefer = str(
        ((cfg or {}).get("alertas") or {}).get("canal")
        or os.environ.get("ALERTA_CANAL", "")
        or ""
    ).lower()
    tg_ok = _telegram_listo(tg)
    wa_ok = bool(wa["activo"] and wa["phone"] and wa["apikey"])
    if prefer in ("telegram", "tg") and tg_ok:
        canal = "telegram"
    elif prefer in ("whatsapp", "wa") and wa_ok:
        canal = "whatsapp"
    elif tg_ok:
        canal = "telegram"
    elif wa_ok:
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
    if t["modo"] == "bot":
        if not t["bot_token"]:
            return {
                "ok": False,
                "motivo": "Falta TELEGRAM_BOT_TOKEN (BotFather → /newbot)",
                "modo": "bot",
                "setup": (
                    "1) Telegram → @BotFather → /newbot\n"
                    "2) Render: TELEGRAM_BOT_TOKEN=...\n"
                    "3) Ábrele chat a TU bot y escribe hola\n"
                    "4) Abre /api/telegram-vincular\n"
                    "5) /api/telegram-test"
                ),
            }
        if not t["chat_id"]:
            return {
                "ok": False,
                "motivo": "Falta vincular: escribe hola a tu bot y abre /api/telegram-vincular",
                "modo": "bot",
                "tiene_token": True,
                "setup": "Abre tu bot en Telegram → envía hola → GET /api/telegram-vincular",
            }
        return {
            "ok": True,
            "canal": "telegram",
            "modo": "bot",
            "chat_id": str(t["chat_id"]),
            "setup": "Bot oficial listo",
        }
    if not t["user"]:
        return {
            "ok": False,
            "motivo": "Configura TELEGRAM_BOT_TOKEN (recomendado) o TELEGRAM_USER",
            "setup": "Usa @BotFather → /newbot (más fácil que CallMeBot)",
        }
    return {
        "ok": True,
        "canal": "telegram",
        "modo": "callmebot",
        "user": t["user"],
        "setup": "CallMeBot legacy — si falla, usa BotFather",
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
        "motivo": tg.get("motivo") or "Sin canal listo",
        "telegram": tg,
        "whatsapp": wa,
        "recomendado": "telegram_bot",
    }


def _enmascarar_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 6:
        return "***"
    return f"+{digits[:3]}***{digits[-2:]}"


def _normalizar_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def equipo_del_pick(pick: str, visitante: str = "", home: str = "") -> str:
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


def enviar_telegram_bot(
    texto: str,
    *,
    token: str,
    chat_id: str,
    timeout: float = 12.0,
) -> dict[str, Any]:
    try:
        r = requests.post(
            f"{TG_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": texto},
            timeout=timeout,
        )
        data = r.json() if r.content else {}
        ok = bool(r.ok and data.get("ok"))
        return {
            "ok": ok,
            "http": r.status_code,
            "canal": "telegram",
            "modo": "bot",
            "chat_id": str(chat_id),
            "detalle": str(data.get("description") or data)[:140],
            "enviado_en": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "motivo": None if ok else str(data.get("description") or f"HTTP {r.status_code}")[:160],
        }
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120], "canal": "telegram", "modo": "bot"}


def vincular_telegram_chat(cfg: dict | None = None) -> dict[str, Any]:
    """Lee getUpdates y guarda chat_id tras que el usuario escriba al bot."""
    t = _cfg_telegram(cfg)
    token = t.get("bot_token") or ""
    if not token:
        return {"ok": False, "motivo": "Falta TELEGRAM_BOT_TOKEN"}
    try:
        r = requests.get(
            f"{TG_API}/bot{token}/getUpdates",
            params={"limit": 20, "timeout": 0},
            timeout=t.get("timeout_sec") or 12,
        )
        data = r.json() if r.ok else {}
        if not data.get("ok"):
            return {
                "ok": False,
                "motivo": (data.get("description") or f"HTTP {r.status_code}")[:160],
            }
        chat_id = None
        username = None
        for upd in reversed(data.get("result") or []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") == "private" and chat.get("id") is not None:
                chat_id = chat["id"]
                username = (chat.get("username") and f"@{chat['username']}") or chat.get("first_name")
                break
        if chat_id is None:
            return {
                "ok": False,
                "motivo": "No hay mensajes aún. Abre TU bot en Telegram y escribe: hola",
                "ayuda": "Luego vuelve a abrir /api/telegram-vincular",
            }
        guardar_chat_id(chat_id)
        ping = enviar_telegram_bot(
            "Quantum MLB vinculado. Ya puedes recibir los picks T-60.",
            token=token,
            chat_id=str(chat_id),
            timeout=float(t.get("timeout_sec") or 12),
        )
        return {
            "ok": True,
            "chat_id": str(chat_id),
            "usuario": username,
            "confirmacion_enviada": bool(ping.get("ok")),
            "detalle": ping.get("detalle") or ping.get("motivo"),
        }
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120]}


def enviar_telegram(
    texto: str,
    cfg: dict | None = None,
    *,
    forzar: bool = False,
) -> dict[str, Any]:
    t = _cfg_telegram(cfg)
    if not forzar and not t["activo"]:
        return {"ok": False, "motivo": "Telegram desactivado"}

    if t.get("bot_token"):
        chat_id = t.get("chat_id") or ""
        if not chat_id:
            return {
                "ok": False,
                "motivo": "Escribe hola a tu bot y abre /api/telegram-vincular",
                "canal": "telegram",
                "modo": "bot",
            }
        return enviar_telegram_bot(
            texto,
            token=t["bot_token"],
            chat_id=str(chat_id),
            timeout=float(t.get("timeout_sec") or 12),
        )

    if not t["user"]:
        return {"ok": False, "motivo": "Falta TELEGRAM_BOT_TOKEN o telegram.user"}

    try:
        params: dict[str, Any] = {"user": t["user"], "text": texto}
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
                "CallMeBot bloqueado. Usa BotFather: TELEGRAM_BOT_TOKEN + /api/telegram-vincular"
            )
        if "denied" in low or "permission" in low or "invalid" in low or "error:" in low:
            ok = False
        if r.status_code == 200 and not body.strip():
            ok = True
        out = {
            "ok": bool(ok),
            "http": r.status_code,
            "canal": "telegram",
            "modo": "callmebot",
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
    # Si hay token sin chat_id, devolver motivo útil
    tg = telegram_disponible(cfg)
    return {"ok": False, "motivo": tg.get("motivo") or "Sin canal configurado"}


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
    pred["whatsapp_enviado"] = ok
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
    dest = resultado.get("user") or resultado.get("chat_id") or resultado.get("phone_mascara") or a.get("canal")
    if resultado.get("ok"):
        juego["alerta_enviado"] = True
        juego["whatsapp_enviado"] = True
        print(f"[ALERTA/{resultado.get('canal')}] Pick T-60: {pick} → {dest}")
    else:
        print(f"[ALERTA] Fallo: {resultado.get('motivo') or resultado.get('detalle')}")
    resultado["texto_preview"] = texto[:180]
    return resultado
