"""Schema v4: el bosque y el scaler deben coincidir, y el ensemble reparte pesos."""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler

import ml_predictor as ml

PESOS = {"estadistico": 0.40, "rf": 0.25, "xgb": 0.35, "ia": 0.0}
RAIZ = Path(__file__).resolve().parent


def _bosque(n_features: int) -> RandomForestClassifier:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(12, n_features))
    y = np.array([0, 1] * 6)
    return RandomForestClassifier(n_estimators=4, max_depth=2, random_state=0).fit(X, y)


def _scaler(n_features: int, columnas: list[str] | None = None) -> StandardScaler:
    if columnas is None:
        X = np.zeros((4, n_features))
        return StandardScaler().fit(X)
    import pandas as pd

    X = pd.DataFrame(np.zeros((4, len(columnas))), columns=columnas)
    return StandardScaler().fit(X)


def test_predecir_rf_omite_si_modelo_y_scaler_no_coinciden(capsys, monkeypatch):
    monkeypatch.setattr(ml, "_modelo_rf", _bosque(16))
    monkeypatch.setattr(ml, "_scaler", _scaler(28, list(ml.FEATURE_COLUMNS)))
    ml._avisos_schema.clear()

    prob = ml.predecir_rf({"era_pitcher": 3.5})

    assert prob is None
    out = capsys.readouterr().out
    assert "incompatible" in out
    assert "modelo=16" in out
    assert "scaler=28" in out
    assert "schema v4=28" in out


def test_predecir_rf_omite_si_ambos_quedaron_en_el_schema_viejo(capsys, monkeypatch):
    monkeypatch.setattr(ml, "_modelo_rf", _bosque(16))
    monkeypatch.setattr(ml, "_scaler", _scaler(16))
    ml._avisos_schema.clear()

    assert ml.predecir_rf({"era_pitcher": 3.5}) is None
    assert "modelo=16" in capsys.readouterr().out


def test_predecir_xgb_usa_el_mismo_guardia(capsys, monkeypatch):
    pytest.importorskip("xgboost")
    from xgboost import XGBClassifier

    X = np.zeros((8, 16))
    y = np.array([0, 1] * 4)
    xgb = XGBClassifier(n_estimators=2, max_depth=1, random_state=0, n_jobs=1)
    xgb.fit(X, y)
    monkeypatch.setattr(ml, "_modelo_xgb", xgb)
    monkeypatch.setattr(ml, "_scaler", _scaler(28, list(ml.FEATURE_COLUMNS)))
    ml._avisos_schema.clear()

    assert ml.predecir_xgb({"era_pitcher": 4.0}) is None
    assert "XGBoost incompatible" in capsys.readouterr().out


def test_predecir_rf_acepta_par_alineado_al_schema(monkeypatch):
    columnas = list(ml.FEATURE_COLUMNS)
    rng = np.random.default_rng(1)
    X = rng.normal(size=(20, len(columnas)))
    y = (X[:, 0] > 0).astype(int)
    import pandas as pd

    df = pd.DataFrame(X, columns=columnas)
    scaler = StandardScaler().fit(df)
    bosque = RandomForestClassifier(n_estimators=8, max_depth=3, random_state=0).fit(
        scaler.transform(df), y
    )
    monkeypatch.setattr(ml, "_modelo_rf", bosque)
    monkeypatch.setattr(ml, "_scaler", scaler)

    prob = ml.predecir_rf({col: 0.2 for col in columnas})

    assert prob is not None
    assert 0.0 <= prob <= 100.0


def test_ensemble_renormaliza_si_falta_xgb():
    #  (50*0.40 + 80*0.25) / 0.65 = 61.538… → 61.5
    #  El reparto viejo volcaba el 0.35 de XGB al estadístico y daba 57.5.
    p = ml.ensemble_prediction(50.0, pesos=PESOS, prob_rf=80.0, prob_xgb=None)
    assert p == 61.5


def test_ensemble_cae_al_estadistico_solo_si_no_hay_ml():
    p = ml.ensemble_prediction(55.4, pesos=PESOS, prob_rf=None, prob_xgb=None)
    assert p == 55.4


def test_ensemble_respeta_pesos_cuando_los_tres_votan():
    # 50*0.40 + 80*0.25 + 60*0.35 = 61.0
    p = ml.ensemble_prediction(50.0, pesos=PESOS, prob_rf=80.0, prob_xgb=60.0)
    assert p == 61.0


def test_ensemble_renormaliza_ia_ausente_en_config_legacy():
    pesos = {"estadistico": 0.4, "ml": 0.4, "ia": 0.2}
    # Solo estadístico e IA: ml se descarta,  (50*0.4 + 70*0.2) / 0.6 = 56.666 → 56.7
    p = ml.ensemble_prediction(50.0, prob_ia=70.0, pesos=pesos, prob_rf=None, prob_xgb=None)
    assert p == 56.7


def test_artefactos_en_disco_coinciden_con_schema_v4():
    with open(RAIZ / "modelo_rf_mlb.pkl", "rb") as f:
        bosque = pickle.load(f)
    with open(RAIZ / "scaler_rf_mlb.pkl", "rb") as f:
        scaler = pickle.load(f)
    with open(RAIZ / "modelo_xgb_mlb.pkl", "rb") as f:
        xgb = pickle.load(f)

    esperado = len(ml.FEATURE_COLUMNS)
    assert int(bosque.n_features_in_) == esperado
    assert int(scaler.n_features_in_) == esperado
    assert int(xgb.n_features_in_) == esperado
    assert list(scaler.feature_names_in_) == list(ml.FEATURE_COLUMNS)
    assert ml.artefactos_alineados_con_schema(bosque, scaler, "Random Forest")
    assert ml.artefactos_alineados_con_schema(xgb, scaler, "XGBoost")


def test_modelos_del_repo_entran_al_ensemble(monkeypatch):
    monkeypatch.setattr(ml, "_modelo_rf", None)
    monkeypatch.setattr(ml, "_scaler", None)
    monkeypatch.setattr(ml, "_modelo_xgb", None)
    monkeypatch.setattr(ml, "_modelo_path", lambda: RAIZ / "modelo_rf_mlb.pkl")
    monkeypatch.setattr(ml, "_scaler_path", lambda: RAIZ / "scaler_rf_mlb.pkl")
    monkeypatch.setattr(ml, "_modelo_xgb_path", lambda: RAIZ / "modelo_xgb_mlb.pkl")

    feats = {col: 0.0 for col in ml.FEATURE_COLUMNS}
    feats.update(
        {
            "era_pitcher": 2.4,
            "whip_pitcher": 0.95,
            "k9_pitcher": 11.0,
            "fip_pitcher": 2.8,
            "edge_estadistico": 12.0,
            "es_local": 1.0,
        }
    )
    p_rf = ml.predecir_rf(feats)
    p_xgb = ml.predecir_xgb(feats)
    assert p_rf is not None and p_xgb is not None

    combinada = ml.ensemble_prediction(10.0, pesos=PESOS, prob_rf=p_rf, prob_xgb=p_xgb)
    esperado = round((10.0 * 0.40 + p_rf * 0.25 + p_xgb * 0.35), 1)
    assert combinada == esperado
    assert combinada != 10.0
