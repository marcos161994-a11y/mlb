"""Tests integridad + panel health para mente de errores."""

from __future__ import annotations

import json
from pathlib import Path

import mente_errores as me
import mente_integridad as mi


def test_auditar_pred_duplicada():
    mem = {
        "capital": 100,
        "capital_inicial": 100,
        "dias": [
            {
                "fecha": "2026-08-29",
                "predicciones": [
                    {"game_id": "123", "pick": "A"},
                    {"game_id": 123, "pick": "B"},
                ],
            }
        ],
    }
    h = mi.auditar_integridad_memoria(mem)
    codigos = {x["codigo"] for x in h}
    assert "memoria_pred_duplicada" in codigos or "memoria_game_id_mixto" in codigos


def test_verificar_panel_ok():
    out = mi.verificar_panel_html(Path(__file__).resolve().parent / "QuantumMLB.html")
    assert out["ok"] is True
    assert out["checks"].get("dias_orden_ok") is True
    assert out["checks"].get("reporte_cliente") is True


def test_verificar_panel_dias_orden_roto(tmp_path):
    bad = tmp_path / "bad.html"
    bad.write_text(
        "function pintarPredicciones(){\n"
        "let html=`${diasOrden.map(x=>x)}`;\n"
        "const diasOrden=[];\n"
        "const PANEL_VER = '2026-08-29-mente';\n"
        "function reportarErrorPanel(){}\n"
        "}",
        encoding="utf-8",
    )
    out = mi.verificar_panel_html(bad)
    assert out["ok"] is False
    assert any(h["codigo"] == "panel_js_dias_orden" for h in out["hallazgos"])


def test_backup_tiene_dias_extra(tmp_path):
    main = tmp_path / "main.json"
    back = tmp_path / "back.json"
    main.write_text(
        json.dumps({"dias": [{"fecha": "2026-08-29", "predicciones": [{"game_id": "1"}]}]}),
        encoding="utf-8",
    )
    back.write_text(
        json.dumps(
            {
                "dias": [
                    {"fecha": "2026-08-28", "predicciones": [{"game_id": "a"}]},
                    {"fecha": "2026-08-29", "predicciones": [{"game_id": "1"}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    h = mi.auditar_backup_local(main, back)
    assert any(x["codigo"] == "backup_tiene_dias_extra" for x in h)


def test_registrar_error_cliente_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    me.registrar_error_cliente("fallo test", codigo="panel_js", panel_ver="v1")
    r2 = me.registrar_error_cliente("fallo test", codigo="panel_js", panel_ver="v1")
    assert r2.get("registrado") is False
    est = me._leer_estado()
    assert len(est.get("errores_cliente") or []) == 1


def test_diagnosticar_incluye_integridad(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(me, "DATA_DIR", Path(tmp_path))
    import servidor_mlb as srv

    monkeypatch.setattr(srv, "BASE_DIR", Path(__file__).resolve().parent)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "m.json")
    monkeypatch.setattr(srv, "MEMORIA_BACKUP_PATH", tmp_path / "b.json")
    (tmp_path / "m.json").write_text(json.dumps({"dias": [], "capital": 100, "capital_inicial": 100}), encoding="utf-8")
    mem = {
        "capital": 100,
        "capital_inicial": 100,
        "dias": [{"fecha": "2026-08-29", "predicciones": [{"game_id": "1"}, {"game_id": "1"}]}],
    }
    h = me.diagnosticar({}, memoria=mem)
    codigos = {x["codigo"] for x in h}
    assert "memoria_pred_duplicada" in codigos
    assert any(c.startswith("panel_") or c == "panel_js_dias_orden" for c in codigos) or "panel_sin_version" not in codigos
