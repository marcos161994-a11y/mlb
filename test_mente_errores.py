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


def test_no_repite_mensaje_ni_incidente_segundo_ciclo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    cfg = _cfg_base()
    meta = {"ok": False, "mensaje": "Sin cuotas de mercado"}
    out1 = me.ejecutar_ciclo(cfg, lineas_meta=meta)
    out2 = me.ejecutar_ciclo(cfg, lineas_meta=meta)
    assert out1.get("hallazgos_nuevos") or out1.get("hallazgos")
    assert "sin novedad" in (out2.get("mensaje") or "").lower()
    assert out2.get("hallazgos_nuevos") == []
    assert len(out2.get("hallazgos_repetidos") or []) >= 1
    estado = me._leer_estado()
    cuotas = [i for i in estado["incidentes"] if i.get("codigo") == "cuotas_fallo"]
    assert len(cuotas) == 1


def test_runtime_no_duplica_mismo_origen(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    me.registrar_error_runtime("cron", "timeout state", codigo="runtime")
    me.registrar_error_runtime("cron", "timeout state otra vez", codigo="runtime")
    estado = me._leer_estado()
    runtimes = [i for i in estado["incidentes"] if i.get("codigo") == "runtime"]
    assert len(runtimes) == 1


def test_incidentes_recientes_uno_por_codigo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    estado = me._leer_estado()
    for i in range(3):
        me._push_incidente(
            estado,
            {
                "hora": me._iso(),
                "codigo": "cuotas_fallo",
                "mensaje": f"intento {i}",
                "severidad": "alta",
            },
        )
    me._push_incidente(
        estado,
        {
            "hora": me._iso(),
            "codigo": "vigilancia_t60",
            "mensaje": "sin pick",
            "severidad": "alta",
        },
    )
    dedup = me._incidentes_recientes_dedup(estado, limite=5)
    codigos = [d.get("codigo") for d in dedup]
    assert codigos.count("cuotas_fallo") == 1
    assert "vigilancia_t60" in codigos


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


def test_telegram_ok_no_dispara_hallazgo(tmp_path, monkeypatch):
    """Bug fix: usaba 'listo' en vez de 'ok' y siempre alertaba."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    cfg = _cfg_base()
    cfg["telegram"] = {
        "activo": True,
        "bot_token": "123:ABC",
        "chat_id": "999",
    }
    out = me.ejecutar_ciclo(cfg)
    codigos = {h["codigo"] for h in out["hallazgos"]}
    assert "telegram_no_listo" not in codigos


def test_restaurar_telegram_desde_memoria(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    import whatsapp_alerta as wa

    monkeypatch.setattr(wa, "_data_dir", lambda: Path(tmp_path))

    memoria = {
        "telegram": {
            "bot_token": "222:FROM_MEM",
            "chat_id": "5423229687",
            "bot": "@TestBot",
        }
    }
    cfg = _cfg_base()
    cfg["telegram"] = {"activo": True}
    out = me.ejecutar_ciclo(cfg, memoria=memoria)
    assert out.get("telegram_restaurado") is True
    assert wa.leer_bot_token_guardado() == "222:FROM_MEM"
    assert str(wa.leer_chat_id_guardado()) == "5423229687"
    codigos = {h["codigo"] for h in out["hallazgos"]}
    assert "telegram_no_listo" not in codigos


def test_sincronizar_telegram_desde_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    import whatsapp_alerta as wa

    monkeypatch.setattr(wa, "_data_dir", lambda: Path(tmp_path))
    out = wa.sincronizar_telegram_persistencia({}, {})
    assert out["ok"] is True
    assert wa.leer_bot_token_guardado() == "111:TOK"
    assert str(wa.leer_chat_id_guardado()) == "42"


def test_vigilancia_t60_fuerza_registro(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    called = {"reg": 0}

    def fake_reg(forzar=False):
        called["reg"] += 1
        assert forzar is True
        return {"predicciones_nuevas": 2}

    def fake_bloq(forzar=False):
        return {"ok": True}

    import servidor_mlb as srv

    monkeypatch.setattr(srv, "registrar_predicciones_del_dia", fake_reg)
    monkeypatch.setattr(srv, "bloquear_apuestas_del_dia", fake_bloq)

    vig = {
        "nivel": "alerta",
        "mensaje": "⚠ 1 juego sin pick",
        "total_riesgo": 1,
        "total_perdidos": 0,
        "en_riesgo": [{"visitante": "A", "home": "B"}],
    }
    out = me.ejecutar_ciclo(_cfg_base(), vigilancia=vig)
    assert any(h["codigo"] == "vigilancia_t60" for h in out["hallazgos"])
    assert called["reg"] >= 1
    acciones = []
    for a in out.get("acciones") or []:
        acciones.extend(a.get("acciones") or [])
    assert me.ACCION_FORZAR_REGISTRO_T60 in acciones


def test_juegos_perdidos_hallazgo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    vig = {
        "nivel": "alerta",
        "mensaje": "⚠ 2 juegos sin predicción",
        "total_riesgo": 0,
        "total_perdidos": 2,
        "perdidos": [
            {"visitante": "New York Yankees", "home": "Toronto Blue Jays"},
            {"visitante": "Chi", "home": "Det"},
        ],
    }
    out = me.ejecutar_ciclo(_cfg_base(), vigilancia=vig)
    codigos = {h["codigo"] for h in out["hallazgos"]}
    assert "juegos_sin_pick_perdidos" in codigos


def test_historial_wipeado_restaura(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    called = {"n": 0}

    def fake_restore():
        called["n"] += 1
        return True

    monkeypatch.setattr("servidor_mlb._intentar_recuperar_wipe", fake_restore)
    monkeypatch.setattr(
        "servidor_mlb._backup_tiene_dias_que_el_disco_perdio",
        lambda b, d: True,
    )
    monkeypatch.setattr("servidor_mlb._contar_historial", lambda m: (2, 10))
    bundled = {"dias": [{"fecha": "2026-08-15", "predicciones": [{"game_id": "1"}]}]}
    disk = {"dias": [{"fecha": "2026-08-17", "predicciones": [{"game_id": "2"}]}]}
    import servidor_mlb as srv

    (tmp_path / "memoria_auditoria.json").write_text(json.dumps(bundled), encoding="utf-8")
    disk_path = tmp_path / "disk.json"
    disk_path.write_text(json.dumps(disk), encoding="utf-8")
    monkeypatch.setattr(srv, "BASE_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", disk_path)
    out = me.ejecutar_ciclo(_cfg_base())
    assert any(h["codigo"] == "historial_wipeado" for h in out["hallazgos"])
    assert called["n"] >= 1
