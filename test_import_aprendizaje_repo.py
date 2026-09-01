"""Test import aprendizaje desde repo bundled."""

from pathlib import Path

import pytest
from fastapi import HTTPException

import servidor_mlb as srv


def test_ejecutar_import_aprendizaje_desde_repo(tmp_path, monkeypatch):
    repo_dump = {
        "capital": 100,
        "dias": [
            {
                "fecha": "2026-07-01",
                "predicciones": [
                    {
                        "game_id": "repo-only-1",
                        "pick": "Cubs ML",
                        "estado": "liquidado",
                        "resultado": "fallo",
                        "lineas_fuente": "draftkings",
                    }
                ],
            }
        ],
        "lecciones": [
            {
                "game_id": "repo-only-1",
                "tipo": "fallo_postmortem",
                "patron": "edge_falso",
                "leccion": "Test import",
            }
        ],
    }
    memoria = {"capital": 100.68, "dias": [], "lecciones": []}
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "memoria_auditoria.json")
    monkeypatch.setattr(srv, "BASE_DIR", Path("/workspace"))
    monkeypatch.setattr(srv, "auto_entrenar_ml", lambda m: {"ok": True, "omitido": True})

    out = srv._ejecutar_import_aprendizaje(memoria, repo_dump)
    assert out["ok"] is True
    assert out["import"]["dump"]["preds_nuevas"] == 1
    assert out["import"]["dump"]["lecciones_importadas"] == 1
    assert memoria["dias"][0]["predicciones"][0]["retroactivo"] is True


def test_ejecutar_import_sin_datos():
    with pytest.raises(HTTPException) as exc:
        srv._ejecutar_import_aprendizaje({"dias": [], "lecciones": []}, None)
    assert exc.value.status_code == 400
