"""Tests OddsPapi moneyline parsing (sin red)."""

from lineas_oddspapi import (
    _limpiar_key,
    _mejor_ml_fixture,
    buscar_lineas_partido,
    fingerprint_key,
    normalizar_nombre_equipo,
)


def test_mejor_ml_elige_mejor_cuota_v4():
    books = {
        "draftkings": {
            "markets": {
                "131": {
                    "outcomes": {
                        "131": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "home",
                                    "price": 1.80,
                                }
                            }
                        },
                        "132": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "away",
                                    "price": 2.10,
                                }
                            }
                        },
                    }
                }
            }
        },
        "fanduel": {
            "markets": {
                "131": {
                    "outcomes": {
                        "131": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "home",
                                    "price": 1.91,
                                }
                            }
                        },
                        "132": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "away",
                                    "price": 2.00,
                                }
                            }
                        },
                    }
                }
            }
        },
    }
    ml = _mejor_ml_fixture(books, ["draftkings", "fanduel"], api="v4")
    assert ml is not None
    assert ml["home"]["decimal"] == 1.91
    assert ml["home"]["casa"] == "fanduel"
    assert ml["away"]["decimal"] == 2.10
    assert ml["away"]["casa"] == "draftkings"


def test_mejor_ml_v5_oddquote():
    books = {
        "pinnacle": {
            "a": {
                "outcomeId": 131,
                "marketId": 131,
                "bookmakerOutcomeId": "1",
                "price": 1.85,
                "active": True,
            },
            "b": {
                "outcomeId": 132,
                "marketId": 131,
                "bookmakerOutcomeId": "2",
                "price": 2.05,
                "active": True,
            },
        },
        "draftkings": {
            "a": {
                "outcomeId": 131,
                "marketId": 131,
                "bookmakerOutcomeId": "home",
                "price": 1.90,
                "active": True,
            },
            "b": {
                "outcomeId": 132,
                "marketId": 131,
                "bookmakerOutcomeId": "away",
                "price": 1.95,
                "active": True,
            },
        },
    }
    ml = _mejor_ml_fixture(books, ["pinnacle", "draftkings"], api="v5")
    assert ml is not None
    assert ml["home"]["decimal"] == 1.90
    assert ml["home"]["casa"] == "draftkings"
    assert ml["away"]["decimal"] == 2.05
    assert ml["away"]["casa"] == "pinnacle"


def test_buscar_partido():
    mapa = {
        (
            normalizar_nombre_equipo("New York Yankees"),
            normalizar_nombre_equipo("Boston Red Sox"),
        ): {
            "away": {"decimal": 2.2, "american": 120, "casa": "pinnacle"},
            "home": {"decimal": 1.7, "american": -143, "casa": "pinnacle"},
        }
    }
    hit = buscar_lineas_partido(mapa, "New York Yankees", "Boston Red Sox")
    assert hit and hit["away"]["decimal"] == 2.2
    hit2 = buscar_lineas_partido(mapa, "Boston Red Sox", "New York Yankees")
    assert hit2 and hit2["home"]["decimal"] == 2.2


def test_limpiar_key_pegada_en_url():
    assert _limpiar_key('apiKey=abc123XYZ') == "abc123XYZ"
    assert _limpiar_key('  "abc123XYZ"  ') == "abc123XYZ"
    assert fingerprint_key("abcdefghijklmnop") == "abcd…mnop (len=16)"
