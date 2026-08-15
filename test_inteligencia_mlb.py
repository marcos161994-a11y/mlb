"""Tests de las 5 capas de inteligencia."""

from __future__ import annotations

from unittest.mock import patch

import inteligencia_mlb as intel


def test_odds_justas_sin_vig():
    fair = intel.odds_justas_sin_vig(2.10, 1.80)
    assert fair["ok"] is True
    assert abs(fair["prob_away"] + fair["prob_home"] - 100) < 0.2
    assert fair["vig_pct"] > 0


def test_consenso_una_casa():
    juego = {
        "odds_away_decimal": 2.20,
        "odds_home_decimal": 1.70,
        "lineas_fuente": "espn",
    }
    c = intel.consenso_mercado(juego)
    assert c["ok"] is True
    assert c["n_fuentes"] == 1
    assert c["prob_away"] is not None


def test_consenso_multi_casa():
    juego = {
        "odds_away_decimal": 2.10,
        "odds_home_decimal": 1.80,
        "lineas_fuente": "draftkings",
        "lineas_libros": [
            {"casa": "draftkings", "away": 2.10, "home": 1.80},
            {"casa": "pinnacle", "away": 2.05, "home": 1.85},
            {"casa": "betmgm", "away": 2.15, "home": 1.75},
        ],
    }
    c = intel.consenso_mercado(juego)
    assert c["ok"] is True
    assert c["n_fuentes"] == 3
    assert c["discrepancia_casas_pct"] >= 0


def test_clasificar_tipo_pick():
    assert (
        intel.clasificar_tipo_pick(
            {"odds": 2.40, "probPick": 48, "scratch_lineup": {}},
            prob=48,
            odds=2.40,
        )
        == "underdog"
    )
    assert (
        intel.clasificar_tipo_pick(
            {"odds": 1.40, "probPick": 68, "scratch_lineup": {}},
            prob=68,
            odds=1.40,
        )
        == "favorito_alto"
    )
    assert (
        intel.clasificar_tipo_pick(
            {"scratch_lineup": {"riesgo": True}, "odds": 1.9, "probPick": 55},
            prob=55,
            odds=1.9,
        )
        == "scratch"
    )


def test_monte_carlo_determinista():
    mc1 = intel.monte_carlo_probs(1580, 1500, home_adv=24, n=400, seed=7)
    mc2 = intel.monte_carlo_probs(1580, 1500, home_adv=24, n=400, seed=7)
    assert mc1["ok"] and mc2["ok"]
    assert mc1["prob_home"] == mc2["prob_home"]
    assert mc1["prob_home"] > mc1["prob_away"]


def test_fusion_mc_suave():
    a, h = intel.fusionar_con_montecarlo(
        50.0,
        50.0,
        {"ok": True, "prob_away": 40.0, "prob_home": 60.0},
        peso_mc=0.25,
    )
    assert abs(a + h - 100) < 0.2
    assert h > a


def test_park_umpire_mueve():
    a, h, meta = intel.aplicar_park_umpire_a_probs(
        50.0, 50.0, park_factor=1.20, sesgo_umpire=0.5
    )
    assert meta.get("ok") is True
    assert abs(a + h - 100) < 0.3


def test_enriquecer_probs_pipeline():
    juego = {
        "id": 42,
        "away_id": None,
        "home_id": None,
        "visitante": "Away",
        "home": "Home",
        "odds_away_decimal": 2.05,
        "odds_home_decimal": 1.85,
        "lineas_fuente": "espn",
        "elo": {"ok": True, "elo_adj_away": 1490, "elo_adj_home": 1520},
        "factores_humanos": {"sesgo_umpire_runs": 0.0},
    }
    cfg = {
        "usar_inteligencia": True,
        "inteligencia": {
            "bullpen_dia": False,  # evita red en test
            "park_umpire": True,
            "monte_carlo": True,
            "consenso_mercado": True,
            "peso_mc": 0.2,
            "mc_sims": 200,
            "peso_consenso": 0.1,
        },
        "elo": {"home_adv": 24},
        "estrategia": {"analizar_bullpen": True},
    }
    with patch.object(intel, "analizar_bullpen_dia", return_value={"ok": False, "fatiga": 0.3}):
        a, h, meta = intel.enriquecer_probs(juego, 52.0, 48.0, cfg, park_factor=1.05, season=2026)
    assert abs(a + h - 100) < 0.3
    assert meta.get("ok") is True
    assert "consenso" in (meta.get("capas") or []) or meta.get("consenso", {}).get("ok")
    assert juego.get("inteligencia") is meta
