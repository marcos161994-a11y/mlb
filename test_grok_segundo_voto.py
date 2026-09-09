"""Tests segundo voto Grok (xAI) solo para dinero."""

from ia_grok import (
    _debe_consultar_grok,
    grok_segundo_voto_disponible,
    modelo_grok,
    segundo_voto_dinero,
)


CFG_ON = {
    "usar_grok_segundo_voto": True,
    "estrategia": {"min_edge_pct": 8.0},
    "grok": {
        "model": "grok-4-1-fast",
        "timeout_sec": 8,
        "api_key": "test-key",
        "min_edge_pct": 8.0,
    },
}


def test_modelo_default():
    assert modelo_grok({}) == "grok-4-1-fast"
    assert modelo_grok({"grok": {"model": "grok-4.6"}}) == "grok-4.6"


def test_disponible_requiere_flag_y_key():
    assert grok_segundo_voto_disponible({"usar_grok_segundo_voto": False, "grok": {"api_key": "x"}}) is False
    assert grok_segundo_voto_disponible({"usar_grok_segundo_voto": True, "grok": {"api_key": ""}}) is False
    assert grok_segundo_voto_disponible(CFG_ON) is True


def test_no_consulta_si_mente_no_autoriza():
    juego = {"pick": "X ML", "edge": 10.0, "id": "1"}
    ok, motivo = _debe_consultar_grok(juego, {"autoriza_dinero": False}, CFG_ON)
    assert ok is False
    assert "mente" in motivo


def test_no_consulta_si_edge_bajo():
    juego = {"pick": "X ML", "edge": 5.0, "id": "1"}
    ok, motivo = _debe_consultar_grok(juego, {"autoriza_dinero": True}, CFG_ON)
    assert ok is False
    assert "edge" in motivo


def test_consulta_si_mente_ok_y_edge_ok():
    juego = {"pick": "X ML", "edge": 9.0, "id": "1"}
    ok, motivo = _debe_consultar_grok(juego, {"autoriza_dinero": True}, CFG_ON)
    assert ok is True
    assert motivo == "ok"


def test_segundo_voto_pasar_cancela(monkeypatch):
    def fake_post(*_a, **_k):
        class R:
            status_code = 200

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '{"decision":"PASAR","motivo":"favorito corto","confianza":5}'
                            }
                        }
                    ]
                }

        return R()

    monkeypatch.setattr("ia_grok.requests.post", fake_post)
    out = segundo_voto_dinero(
        {"id": "g1", "pick": "Dodgers ML", "probPick": 64, "edge": 9.0, "odds": 1.45, "lineas_fuente": "draftkings"},
        CFG_ON,
        mente={"autoriza_dinero": True, "confianza": 4, "razones": ["ok"]},
        memoria={"lecciones": []},
    )
    assert out["ok"] is True
    assert out["decision"] == "PASAR"
    assert out["fuente"] == "grok"


def test_segundo_voto_omitido_sin_bloquear():
    out = segundo_voto_dinero(
        {"id": "g2", "pick": "X ML", "edge": 3.0},
        CFG_ON,
        mente={"autoriza_dinero": True},
    )
    assert out.get("omitido") is True
    assert out["decision"] == "SKIP"


def test_config_tiene_grok():
    import json
    from pathlib import Path

    cfg = json.loads(Path("config_experimento.json").read_text(encoding="utf-8"))
    assert cfg.get("usar_grok_segundo_voto") is True
    assert (cfg.get("grok") or {}).get("model")
    assert float((cfg.get("grok") or {}).get("min_edge_pct") or 0) >= 8.0
