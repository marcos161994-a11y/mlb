"""Tests de alertas WhatsApp T-60."""

from whatsapp_alerta import (
    equipo_del_pick,
    formatear_mensaje_pick,
    notificar_pick_t60,
    whatsapp_disponible,
)


def test_equipo_del_pick():
    assert equipo_del_pick("Yankees ML", "Mariners", "Yankees") == "Yankees"
    assert equipo_del_pick("Mariners ML", "Mariners", "Yankees") == "Mariners"
    assert "Red Sox" in equipo_del_pick("Boston Red Sox ML", "Yankees", "Boston Red Sox")


def test_formatear_incluye_equipo_y_partido():
    txt = formatear_mensaje_pick(
        {
            "visitante": "Mariners",
            "home": "Yankees",
            "pick": "Yankees ML",
            "probPick": 61,
            "edge": 8.5,
            "odds": 1.9,
            "hora_inicio_txt": "07:05 PM",
            "ia_mente": {"decision": "APOSTAR", "confianza": 4, "razones": ["Edge sólido"]},
        },
        cfg={"mente": {"shadow": True}, "whatsapp": {"incluir_mente": True}},
        fase="t60",
    )
    assert "Yankees" in txt
    assert "Mariners @ Yankees" in txt
    assert "T-60" in txt
    assert "shadow" in txt.lower()
    assert "APOSTAR" in txt


def test_disponible_sin_credenciales():
    d = whatsapp_disponible({"whatsapp": {"activo": True, "phone": "", "apikey": ""}})
    assert d["ok"] is False


def test_notificar_dedup(monkeypatch):
    enviados = []

    def fake_enviar(texto, cfg=None, forzar=False):
        enviados.append(texto)
        return {"ok": True, "enviado_en": "2026-08-13T00:00:00Z"}

    monkeypatch.setattr("whatsapp_alerta.enviar_whatsapp", fake_enviar)
    cfg = {
        "whatsapp": {
            "activo": True,
            "phone": "+17875551212",
            "apikey": "999",
            "incluir_mente": True,
        }
    }
    juego = {
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 60,
        "edge": 7,
        "odds": 2.0,
    }
    pred = {"pick": "B ML", "probPick": 60, "edge": 7, "odds": 2.0}
    r1 = notificar_pick_t60(juego, pred, cfg)
    r2 = notificar_pick_t60(juego, pred, cfg)
    assert r1["ok"] is True
    assert r2.get("omitido") is True
    assert len(enviados) == 1
    assert pred.get("whatsapp_enviado") is True
    assert "B" in enviados[0]


def test_omitir_sin_pick(monkeypatch):
    monkeypatch.setattr(
        "whatsapp_alerta.enviar_whatsapp",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe enviar")),
    )
    cfg = {"whatsapp": {"activo": True, "phone": "+1", "apikey": "x"}}
    r = notificar_pick_t60({"visitante": "A", "home": "B"}, {}, cfg)
    assert r.get("omitido") is True
