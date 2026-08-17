"""Tests de recuperación de historial tras wipe de Render."""

from __future__ import annotations

import json
from pathlib import Path

import servidor_mlb as srv


def test_fusionar_trae_dia_perdido():
    bundled = {
        "capital_inicial": 100,
        "capital": 92,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-08-15",
                "predicciones": [
                    {"game_id": "822775", "pick": "NYY ML", "resultado": "acierto", "estado": "liquidado"}
                ],
                "apuestas": [
                    {"game_id": "x", "estado": "perdida", "profit": -8}
                ],
            }
        ],
    }
    disk = {
        "capital_inicial": 100,
        "capital": 100,
        "dia_actual": 2,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-08-16",
                "predicciones": [{"game_id": "hoy", "pick": "BOS ML", "estado": "pendiente"}],
                "apuestas": [],
            }
        ],
    }
    merged = srv._fusionar_memoria(bundled, disk)
    fechas = [d["fecha"] for d in merged["dias"]]
    assert "2026-08-15" in fechas
    assert "2026-08-16" in fechas
    assert merged["capital"] == 92.0


def test_backup_detecta_dia_faltante():
    bundled = {"dias": [{"fecha": "2026-08-15", "predicciones": [{"game_id": "1"}]}]}
    disk = {"dias": [{"fecha": "2026-08-16", "predicciones": [{"game_id": "2"}]}]}
    assert srv._backup_tiene_dias_que_el_disco_perdio(bundled, disk) is True
    assert srv._backup_tiene_dias_que_el_disco_perdio(bundled, bundled) is False


def test_recuperar_wipe_aunque_sea_dia_2(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "BASE_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "disk.json")
    bundled = {
        "capital_inicial": 100,
        "capital": 92,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-08-15",
                "predicciones": [
                    {"game_id": "yankees", "pick": "NYY ML", "resultado": "acierto", "estado": "liquidado"}
                ],
                "apuestas": [],
            }
        ],
    }
    disk = {
        "capital_inicial": 100,
        "capital": 100,
        "dia_actual": 2,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-08-17",
                "predicciones": [{"game_id": "hoy", "pick": "X ML", "estado": "pendiente"}],
            }
        ],
    }
    (tmp_path / "memoria_auditoria.json").write_text(json.dumps(bundled), encoding="utf-8")
    (tmp_path / "disk.json").write_text(json.dumps(disk), encoding="utf-8")
    assert srv._memoria_parece_reinicio(disk) is False
    assert srv._intentar_recuperar_wipe() is True
    out = json.loads((tmp_path / "disk.json").read_text(encoding="utf-8"))
    fechas = {d["fecha"] for d in out["dias"]}
    assert "2026-08-15" in fechas
    assert "2026-08-17" in fechas
