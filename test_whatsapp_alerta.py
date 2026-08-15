"""Tests de alertas T-60 (Telegram bot / WhatsApp)."""

from whatsapp_alerta import (
    equipo_del_pick,
    formatear_mensaje_pick,
    notificar_pick_t60,
    whatsapp_disponible,
    telegram_disponible,
    alerta_disponible,
    guardar_chat_id,
    leer_chat_id_guardado,
)


def test_equipo_del_pick():
    assert equipo_del_pick("Yankees ML", "Mariners", "Yankees") == "Yankees"


def test_formatear_incluye_equipo():
    txt = formatear_mensaje_pick(
        {
            "visitante": "Mariners",
            "home": "Yankees",
            "pick": "Yankees ML",
            "probPick": 61,
            "edge": 8.5,
            "odds": 1.9,
            "hora_inicio_txt": "07:05 PM",
            "ia_mente": {"decision": "APOSTAR", "confianza": 4, "razones": ["Edge"]},
        },
        cfg={
            "mente": {"shadow": True},
            "telegram": {"activo": True, "bot_token": "1:x", "chat_id": "99", "incluir_mente": True},
        },
        fase="t60",
    )
    assert "Yankees" in txt and "APOSTAR" in txt and "shadow" in txt.lower()


def test_bot_sin_chat_id_no_listo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_USER", raising=False)
    d = telegram_disponible(
        {"telegram": {"activo": True, "bot_token": "123:ABC", "chat_id": ""}}
    )
    assert d["ok"] is False
    assert "vincular" in (d.get("motivo") or "").lower() or "hola" in (d.get("motivo") or "").lower()


def test_user_solo_no_cuenta_como_listo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_USER", "@Marquitos053")
    d = telegram_disponible({"telegram": {"activo": True}})
    assert d["ok"] is False
    assert d.get("modo") in ("pendiente_bot", "ninguno") or "BotFather" in str(d.get("setup") or "")


def test_persist_memoria_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from whatsapp_alerta import telegram_a_memoria, restaurar_telegram_desde_memoria, leer_bot_token_guardado

    mem = telegram_a_memoria({}, token="999:AAA", chat_id="42", bot="@x_bot")
    assert mem["telegram"]["bot_token"] == "999:AAA"
    # wipe files and restore
    for name in ("telegram_bot_token.txt", "telegram_chat_id.txt", "telegram_creds.json"):
        p = tmp_path / name
        if p.exists():
            p.unlink()
    r = restaurar_telegram_desde_memoria(mem)
    assert r["ok"]
    assert leer_bot_token_guardado() == "999:AAA"


def test_bot_con_token_y_chat_listo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    d = telegram_disponible(
        {"telegram": {"activo": True, "bot_token": "123:ABC", "chat_id": "555"}}
    )
    assert d["ok"] is True
    assert d["modo"] == "bot"


def test_guardar_chat_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    guardar_chat_id(777)
    assert leer_chat_id_guardado() == "777"
    guardar_chat_id("TELEGRAM_CHAT_ID = 999888777")
    assert leer_chat_id_guardado() == "999888777"


def test_normalizar_chat_id_sucio():
    from whatsapp_alerta import normalizar_chat_id

    assert normalizar_chat_id("5423229687") == "5423229687"
    assert normalizar_chat_id("TELEGRAM_CHAT_ID = 5423229687") == "5423229687"
    assert normalizar_chat_id("chat_id: 5423229687") == "5423229687"
    assert normalizar_chat_id("  5423229687  ") == "5423229687"
    assert normalizar_chat_id("") == ""
    assert normalizar_chat_id("solo texto") == ""


def test_env_chat_id_sucio_sigue_listo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID = 5423229687")
    from whatsapp_alerta import telegram_disponible

    st = telegram_disponible({"telegram": {"activo": True}})
    assert st["ok"] is True
    assert st["chat_id"] == "5423229687"


def test_notificar_usa_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    enviados = []

    def fake_enviar(texto, cfg=None, forzar=False):
        enviados.append(texto)
        return {"ok": True, "canal": "telegram", "modo": "bot", "chat_id": "1", "enviado_en": "Z"}

    monkeypatch.setattr("whatsapp_alerta.enviar_alerta", fake_enviar)
    cfg = {
        "telegram": {"activo": True, "bot_token": "1:x", "chat_id": "1"},
        "alertas": {"canal": "telegram"},
    }
    pred = {"pick": "B ML", "probPick": 60, "edge": 7, "odds": 2.0}
    r = notificar_pick_t60({"visitante": "A", "home": "B", "pick": "B ML"}, pred, cfg)
    assert r["ok"] and pred.get("alerta_enviado")
    assert len(enviados) == 1


def test_guardar_token_invalido():
    from whatsapp_alerta import configurar_bot_token
    r = configurar_bot_token("sin_dos_puntos")
    assert r["ok"] is False


def test_whatsapp_off():
    assert whatsapp_disponible({"whatsapp": {"activo": False}})["ok"] is False


def test_alerta_sin_canal():
    a = alerta_disponible({"telegram": {"activo": True}, "whatsapp": {"activo": False}})
    assert a["ok"] is False
