"""ML: bloqueo/peso de features sintéticas y meta en ml_meta."""

from __future__ import annotations

import json
from pathlib import Path

import ml_predictor as ml


def _memoria_liquidada(*, con_features: bool, n: int = 8) -> dict:
    preds = []
    for i in range(n):
        p = {
            "game_id": str(9000 + i),
            "pick": "Team A ML",
            "home": "Team A",
            "visitante": "Team B",
            "probPick": 58.0,
            "edge": 8.0,
            "estado": "liquidado",
            "resultado": "acierto" if i % 2 == 0 else "fallo",
            "con_dinero": False,
            "lineas_fuente": "draftkings",
        }
        if con_features:
            p["ml_features"] = {
                "era_pitcher": 3.8,
                "whip_pitcher": 1.1,
                "k9_pitcher": 9.0,
                "woba_equipo": 0.32,
                "ops_equipo": 0.72,
                "win_pct_equipo": 0.55,
                "es_local": 1.0,
                "park_factor": 1.0,
                "fatiga_bullpen": 0.2,
                "matchup_zurdo_diestro": 0.0,
                "edge_estadistico": 8.0,
                "bb9_pitcher": 2.8,
                "hr9_pitcher": 1.0,
                "racha_equipo": 0.55,
                "diferencia_run": 1.0,
                "vs_pitcher_hand": 0.0,
                "temp_f": 72.0,
                "viento_mph": 5.0,
                "run_env": 0.0,
                "fip_pitcher": 3.9,
                "xfip_pitcher": 4.0,
                "k_pct_pitcher": 22.0,
                "bb_pct_pitcher": 7.0,
                "fatiga_viaje": 0.0,
                "dias_descanso": 1.0,
                "cambio_zona": 0.0,
                "leverage_serie": 0.0,
                "umpire_runs": 0.0,
            }
        preds.append(p)
    return {"dias": [{"fecha": "2026-08-30", "predicciones": preds, "apuestas": []}]}


def test_features_desde_registro_real_vs_sintetica():
    real, fuente = ml.features_desde_registro({"ml_features": {"era_pitcher": 4.0, "whip_pitcher": 1.2}})
    assert fuente == "real"
    assert real["era_pitcher"] == 4.0

    _, fuente2 = ml.features_desde_registro({"probPick": 60, "edge": 5, "pick": "X ML", "home": "X"})
    assert fuente2 == "sintetica"


def test_peso_sintetica_reducido(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config_experimento.json"
    cfg_path.write_text(
        json.dumps({"aprendizaje": {"ml_peso_features_sinteticas": 0.05}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ml, "BASE_DIR", tmp_path)

    mem = _memoria_liquidada(con_features=False, n=4)
    datos = ml.cargar_datos_entrenamiento_desde_memoria(mem)
    assert len(datos) == 4
    assert all(d["_fuente_features"] == "sintetica" for d in datos)
    assert all(abs(d["_peso"] - 0.05) < 1e-9 for d in datos)


def test_auto_entrenar_bloqueado_pocas_reales(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config_experimento.json"
    cfg_path.write_text(
        json.dumps(
            {
                "aprendizaje": {
                    "ml_min_muestras_reales": 10,
                    "ml_min_ratio_features_reales": 0.5,
                    "ml_peso_features_sinteticas": 0.05,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ml, "BASE_DIR", tmp_path)
    monkeypatch.setattr(ml, "_modelo_path", lambda: tmp_path / "modelo_rf_mlb.pkl")

    mem = _memoria_liquidada(con_features=False, n=12)
    meta = ml.auto_entrenar_ml(mem, min_muestras=5)
    assert meta["entreno_bloqueado"] is True
    assert meta["muestras_reales"] == 0
    assert meta["muestras_sinteticas"] == 12
    assert meta["ratio_features_reales"] == 0.0
    assert "Entreno pausado" in meta["mensaje"]


def test_auto_entrenar_permite_suficientes_reales(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config_experimento.json"
    cfg_path.write_text(
        json.dumps(
            {
                "aprendizaje": {
                    "ml_min_muestras_reales": 5,
                    "ml_min_ratio_features_reales": 0.5,
                    "ml_peso_features_sinteticas": 0.05,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ml, "BASE_DIR", tmp_path)
    monkeypatch.setattr(ml, "_modelo_path", lambda: tmp_path / "no_existe.pkl")
    monkeypatch.setattr(ml, "_modelo_xgb_path", lambda: tmp_path / "no_xgb.pkl")

    mem = _memoria_liquidada(con_features=True, n=10)
    meta = ml.auto_entrenar_ml(mem, min_muestras=5)
    assert meta["entreno_bloqueado"] is False
    assert meta["muestras_reales"] == 10
    assert meta["ratio_features_reales"] == 1.0
    assert meta.get("ok") is True


def test_config_ml_aprendizaje_presente():
    cfg = json.loads(Path("config_experimento.json").read_text(encoding="utf-8"))
    ap = cfg.get("aprendizaje") or {}
    assert "ml_min_muestras_reales" in ap
    assert "ml_min_ratio_features_reales" in ap
    assert "ml_peso_features_sinteticas" in ap
