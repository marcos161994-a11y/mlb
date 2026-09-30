"""Stake fijo: apuesta_fija, sin rango 2–3% ni Kelly."""

import pytest

from modelo_mlb import calcular_stake_dinamico


def _cfg(**extra):
    cfg = {
        "apuesta_fija": 3.0,
        "stake_por_juego": 10.0,
        "estrategia": {
            "gestion_bankroll_dinamica": True,
            "min_stake_pct": 2.0,
            "max_stake_pct": 3.0,
        },
    }
    cfg.update(extra)
    return cfg


@pytest.mark.parametrize("capital", [50.0, 97.66, 100.0, 150.0, 1000.0])
@pytest.mark.parametrize("edge", [0.0, 5.0, 15.0, 40.0])
def test_stake_fijo_ignora_banca_y_edge(capital, edge):
    assert calcular_stake_dinamico(capital, edge, 1.0, _cfg()) == pytest.approx(3.0)
    assert calcular_stake_dinamico(capital, edge, 0.0, _cfg()) == pytest.approx(3.0)


def test_apuesta_fija_es_configurable():
    cfg = _cfg(apuesta_fija=4.5)
    assert calcular_stake_dinamico(97.66, 20.0, 1.0, cfg) == pytest.approx(4.5)


def test_sin_clave_el_default_es_3():
    cfg = _cfg()
    del cfg["apuesta_fija"]
    assert calcular_stake_dinamico(80.0, 0.0, 0.0, cfg) == pytest.approx(3.0)


def test_valor_invalido_vuelve_a_3():
    assert calcular_stake_dinamico(100.0, 10.0, 1.0, _cfg(apuesta_fija=0)) == pytest.approx(3.0)
    assert calcular_stake_dinamico(100.0, 10.0, 1.0, _cfg(apuesta_fija=-2)) == pytest.approx(3.0)
    assert calcular_stake_dinamico(100.0, 10.0, 1.0, _cfg(apuesta_fija="no")) == pytest.approx(3.0)
