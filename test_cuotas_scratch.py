"""Tests unitarios: edge vs mercado + scratch/lineup (sin red)."""

from __future__ import annotations

from lineas_betmgm import american_a_decimal, normalizar_nombre_equipo
from lineup_scratch import analizar_scratch_lineup, pick_afectado_por_scratch, parsear_lineups_juego
from modelo_mlb import edge_pct, prob_implicita


def test_aliases_odds():
    assert normalizar_nombre_equipo("Oakland Athletics") == "athletics"
    assert normalizar_nombre_equipo("LA Dodgers") == "los angeles dodgers"
    assert normalizar_nombre_equipo("St. Louis Cardinals") == "st louis cardinals"


def test_american_decimal_roundtrip():
    assert american_a_decimal(-110) == 1.909
    assert american_a_decimal(150) == 2.5


def test_edge_vs_mercado():
    # Modelo 60% vs cuota -110 (~47.6% implícita) → edge claro
    dec = american_a_decimal(-110)
    e = edge_pct(60.0, dec)
    assert e > 6.0
    # Modelo 48% vs -110 → sin valor
    assert edge_pct(48.0, dec) < 6.0


def test_parsear_lineups():
    raw = {
        "lineups": {
            "awayPlayers": [{"id": 1, "fullName": "A"}, {"id": 2, "fullName": "B"}],
            "homePlayers": [{"id": 3, "fullName": "C"}],
        }
    }
    lu = parsear_lineups_juego(raw)
    assert lu["confirmado"] is True
    assert len(lu["away"]) == 2
    assert lu["home"][0]["id"] == 3


def test_scratch_sp_cambia():
    info = analizar_scratch_lineup(
        away_id=None,
        home_id=None,
        pitcher_away_id=10,
        pitcher_home_id=20,
        pitcher_away_nombre="Nuevo",
        pitcher_home_nombre="Igual",
        lineups={"away": [], "home": [], "confirmado": False},
        season=2026,
        pred_congelada={
            "pitcher_away_id": 99,
            "pitcher_home_id": 20,
            "pitcherAway": "Viejo",
            "pitcherHome": "Igual",
        },
        min_estrellas_fuera=2,
    )
    assert info["scratch_away"] is True
    assert info["scratch_home"] is False
    assert info["riesgo"] is True
    assert pick_afectado_por_scratch("Yankees ML", "Yankees", "Red Sox", info) is True


def test_estrellas_fuera_bloquea_pick():
    info = {
        "ok": True,
        "scratch_away": False,
        "scratch_home": False,
        "estrellas_fuera_away": [{"id": 1}, {"id": 2}],
        "estrellas_fuera_home": [],
        "min_estrellas_fuera": 2,
        "riesgo": True,
    }
    assert pick_afectado_por_scratch("Mets ML", "Mets", "Phillies", info) is True
    assert pick_afectado_por_scratch("Phillies ML", "Mets", "Phillies", info) is False


def test_prob_implicita():
    assert abs(prob_implicita(2.0) - 50.0) < 0.01
