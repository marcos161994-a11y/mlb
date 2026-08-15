"""Tests fusión Elo + ajuste pitcher."""

from __future__ import annotations

from pathlib import Path

import elo_mlb as elo


def test_prob_desde_elo_favorito_local(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    p_h, p_a = elo.prob_desde_elo(1600, 1500, home_adv=24)
    assert p_h > p_a
    assert abs(p_h + p_a - 100) < 0.1


def test_ajuste_pitcher_fip_mejor_suma():
    adj = elo.ajuste_pitcher_fip(3.20, fip_liga=4.20, scale=28)
    assert adj > 0
    adj_malo = elo.ajuste_pitcher_fip(5.20, fip_liga=4.20, scale=28)
    assert adj_malo < 0


def test_actualizar_resultado_idempotente(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    r1 = elo.actualizar_resultado(
        away_id=111,
        home_id=112,
        score_away=2,
        score_home=5,
        game_id="g1",
        away_nombre="Away",
        home_nombre="Home",
        k=20,
    )
    assert r1["ok"] and r1["ganador"] == "home"
    e_home = elo.elo_equipo(112)
    r2 = elo.actualizar_resultado(
        away_id=111,
        home_id=112,
        score_away=2,
        score_home=5,
        game_id="g1",
        k=20,
    )
    assert r2.get("omitido") is True
    assert elo.elo_equipo(112) == e_home


def test_fusion_mueve_probs_con_pitcher(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    juego = {
        "away_id": 200,
        "home_id": 201,
        "visitante": "Away FC",
        "home": "Home FC",
    }
    # Modelo 50/50; home pitcher mucho mejor FIP → fusión favorece home
    pa = {"fip": 5.5, "nombre": "Bad"}
    ph = {"fip": 2.8, "nombre": "Ace"}
    cfg = {"usar_elo": True, "elo": {"peso_elo": 0.5, "pitcher_scale": 28, "home_adv": 24}}
    a, h, meta = elo.fusionar_probs_elo(juego, 50.0, 50.0, pa, ph, cfg)
    assert meta["ok"] is True
    assert h > a
    assert meta["adj_pitcher_home"] > 0
    assert meta["adj_pitcher_away"] < 0
    assert Path(tmp_path, "elo_ratings.json").exists()


def test_fusion_desactivada(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    juego = {"away_id": 1, "home_id": 2, "visitante": "A", "home": "H"}
    a, h, meta = elo.fusionar_probs_elo(
        juego, 62.0, 38.0, {}, {}, {"usar_elo": False}
    )
    assert a == 62.0 and h == 38.0
    assert meta.get("activo") is False
