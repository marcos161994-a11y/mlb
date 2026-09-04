"""Tests: papel solo congela picks con valor vs mercado."""

from servidor_mlb import omitir_congelar_papel


CFG = {
    "estrategia": {
        "min_edge_pct": 6.0,
        "min_prob_modelo": 58.0,
        "papel_solo_valor": True,
        "papel_min_edge_pct": 6.0,
        "papel_respeta_favorito_inflado": True,
        "favorito_inflado": {
            "activo": True,
            "umbral_prob": 62.0,
            "min_edge_pct": 15.0,
        },
    }
}


def test_omite_edge_negativo():
    juego = {
        "pick": "Orioles ML",
        "probPick": 60.5,
        "odds": 1.552,
        "edge": -3.9,
        "lineas_fuente": "draftkings",
    }
    omitir, motivo = omitir_congelar_papel(juego, CFG)
    assert omitir is True
    assert "edge" in motivo


def test_omite_sin_cuota_mercado():
    juego = {
        "pick": "X ML",
        "probPick": 60.0,
        "odds": 1.8,
        "edge": 8.0,
        "lineas_fuente": "modelo",
    }
    omitir, motivo = omitir_congelar_papel(juego, CFG)
    assert omitir is True
    assert "cuota" in motivo or "mercado" in motivo


def test_omite_favorito_inflado():
    juego = {
        "pick": "Rangers ML",
        "probPick": 71.8,
        "odds": 1.617,
        "edge": 10.0,
        "lineas_fuente": "draftkings",
    }
    omitir, motivo = omitir_congelar_papel(juego, CFG)
    assert omitir is True
    assert "favorito" in motivo.lower() or "62" in motivo or "15" in motivo


def test_acepta_pick_con_valor():
    juego = {
        "pick": "Twins ML",
        "probPick": 60.1,
        "odds": 1.87,
        "edge": 6.6,
        "lineas_fuente": "draftkings",
    }
    omitir, motivo = omitir_congelar_papel(juego, CFG)
    assert omitir is False
    assert motivo == ""


def test_desactivado_deja_pasar_sin_valor():
    cfg = {"estrategia": {**CFG["estrategia"], "papel_solo_valor": False}}
    juego = {
        "pick": "Pirates ML",
        "probPick": 52.7,
        "odds": 1.641,
        "edge": -8.2,
        "lineas_fuente": "draftkings",
    }
    omitir, _ = omitir_congelar_papel(juego, cfg)
    assert omitir is False


def test_omite_prob_bajo_aunque_edge_ok():
    juego = {
        "pick": "Giants ML",
        "probPick": 52.2,
        "odds": 2.41,
        "edge": 10.7,
        "lineas_fuente": "draftkings",
    }
    omitir, motivo = omitir_congelar_papel(juego, CFG)
    assert omitir is True
    assert "prob" in motivo
