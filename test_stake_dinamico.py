"""Stake dinámico: el rango 2–3% varía y nunca supera max_stake_pct."""

import pytest

from modelo_mlb import calcular_stake_dinamico


def _cfg(**estrategia):
    base = {
        "gestion_bankroll_dinamica": True,
        "min_stake_pct": 2.0,
        "max_stake_pct": 3.0,
    }
    base.update(estrategia)
    return {"stake_por_juego": 3.0, "estrategia": base}


def _como_servidor(edge: float) -> float:
    """Misma confianza que servidor_mlb al bloquear una apuesta."""
    return min(max((edge - 5.0) / 10.0, 0.5), 1.0)


def _stake(capital: float, edge: float, confianza: float | None = None, cfg: dict | None = None) -> float:
    if confianza is None:
        confianza = _como_servidor(edge)
    return calcular_stake_dinamico(capital, edge, confianza, cfg or _cfg())


@pytest.mark.parametrize("capital", [50.0, 97.66, 100.0, 150.0])
def test_rango_varia_dentro_del_techo(capital):
    bajo = _stake(capital, edge=5.0)
    medio = _stake(capital, edge=10.0)
    alto = _stake(capital, edge=15.0)
    piso = capital * 0.02
    techo = capital * 0.03

    assert bajo == pytest.approx(piso)
    assert medio == pytest.approx(capital * 0.0225)
    assert alto == pytest.approx(techo)
    assert bajo < medio < alto
    assert alto <= techo + 1e-9
    assert bajo >= piso - 1e-9


def test_banca_100_no_queda_plana_en_3():
    assert _stake(100.0, 5.0) == pytest.approx(2.0)
    assert _stake(100.0, 15.0) == pytest.approx(3.0)


def test_banca_9766_el_piso_de_3_no_rompe_el_3_pct():
    alto = _stake(97.66, 20.0, confianza=1.0)
    bajo = _stake(97.66, 0.0, confianza=0.0)
    assert alto == pytest.approx(97.66 * 0.03)
    assert bajo == pytest.approx(97.66 * 0.02)
    assert alto < 3.0
    assert bajo < alto


def test_banca_50_el_techo_gana_sobre_el_piso_de_3():
    alto = _stake(50.0, 15.0)
    bajo = _stake(50.0, 0.0, confianza=0.0)
    assert bajo == pytest.approx(1.0)
    assert alto == pytest.approx(1.5)
    assert alto < 3.0


def test_banca_150_recorre_de_3_a_4_50():
    assert _stake(150.0, 5.0) == pytest.approx(3.0)
    assert _stake(150.0, 15.0) == pytest.approx(4.5)


def test_piso_en_dolares_no_levanta_el_stake_sobre_el_techo():
    cfg = _cfg()
    cfg["stake_por_juego"] = 10.0
    for capital in (50.0, 97.66, 100.0, 150.0):
        stake = calcular_stake_dinamico(capital, 25.0, 1.0, cfg)
        assert stake == pytest.approx(capital * 0.03)
        assert stake < 10.0


def test_techo_gana_si_min_stake_pct_supera_max():
    cfg = _cfg(min_stake_pct=8.0, max_stake_pct=3.0)
    assert calcular_stake_dinamico(100.0, 0.0, 0.0, cfg) == pytest.approx(3.0)
    assert calcular_stake_dinamico(100.0, 20.0, 1.0, cfg) == pytest.approx(3.0)


def test_sin_gestion_dinamica_devuelve_la_unidad_fija():
    cfg = _cfg()
    cfg["estrategia"]["gestion_bankroll_dinamica"] = False
    assert calcular_stake_dinamico(50.0, 15.0, 1.0, cfg) == pytest.approx(3.0)


def test_confianza_fuera_de_rango_no_supera_el_techo():
    assert calcular_stake_dinamico(100.0, 50.0, 5.0, _cfg()) == pytest.approx(3.0)
    assert calcular_stake_dinamico(80.0, 50.0, -2.0, _cfg()) == pytest.approx(1.6)
