"""Tests mente de errores (ops self-heal, no picks)."""

from __future__ import annotations

import json
from pathlib import Path

import mente_errores as me


def _cfg_base(**extra):
    cfg = {
        "usar_mente_errores": True,
        "mente_errores": {
            "activo": True,
            "auto_remediar": True,
            "notificar": False,
            "cooldown_alerta_min": 360,
        },
        "lineas": {"proveedor": "oddspapi", "fallback_internet": True},
        "mente": {"modo": "normal", "shadow": False},
        "telegram": {"activo": False},
    }
    cfg.update(extra)
    return cfg


def test_forzar_espn_si_circuito(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    circuit = tmp_path / "oddspapi_circuit.json"
    circuit.write_text(
        json.dumps(
            {
                "hasta": "2099-01-01T00:00:00",
                "hasta_hora": "00:00",
                "mensaje": "401",
                "http_status": 401,
            }
        ),
        encoding="utf-8",
    )
    # estado_circuito lee DATA_DIR via lineas_oddspapi
    import lineas_oddspapi as op

    monkeypatch.setattr(op, "DATA_DIR", Path(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))

    out = me.ejecutar_ciclo(_cfg_base())
    assert out["ok"]
    assert out["nivel"] == "alerta"
    assert (out.get("overrides") or {}).get("lineas.proveedor") == "espn"
    codigos = {h["codigo"] for h in out["hallazgos"]}
    assert "oddspapi_circuito" in codigos

    cfg2 = me.aplicar_overrides_config(_cfg_base())
    assert cfg2["lineas"]["proveedor"] == "espn"


def test_apaga_shadow(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))

    cfg = _cfg_base()
    cfg["mente"] = {"modo": "shadow", "shadow": True}
    out = me.ejecutar_ciclo(cfg)
    assert any(h["codigo"] == "mente_shadow" for h in out["hallazgos"])
    assert out["overrides"].get("mente.shadow") is False
    assert out["overrides"].get("mente.modo") == "normal"


def test_activa_fallback_si_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    cfg = _cfg_base()
    cfg["lineas"] = {"proveedor": "espn", "fallback_internet": False}
    out = me.ejecutar_ciclo(cfg)
    assert any(h["codigo"] == "fallback_internet_off" for h in out["hallazgos"])
    assert out["overrides"].get("lineas.fallback_internet") is True


def test_desactivada_no_remedia(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    cfg = _cfg_base(usar_mente_errores=False)
    cfg["mente_errores"]["activo"] = False
    out = me.ejecutar_ciclo(cfg)
    assert out["activo"] is False
    assert out["hallazgos"] == []


def test_no_duplica_incidentes_en_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    cfg = _cfg_base()
    cfg["mente"] = {"modo": "shadow", "shadow": True}
    me.ejecutar_ciclo(cfg)
    me.ejecutar_ciclo(cfg)
    estado = me._leer_estado()
    shadows = [i for i in estado["incidentes"] if i.get("codigo") == "mente_shadow"]
    assert len(shadows) == 1
