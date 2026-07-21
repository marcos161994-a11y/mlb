"""Pruebas rápidas de liquidación / ganador oficial."""

from servidor_mlb import (
    _score_equipo,
    _juego_finalizado,
    _ganador_oficial,
    liquidar_apuesta,
    _revertir_liquidacion_prematura,
)


def test_score_cero_no_se_pierde():
    assert _score_equipo({"runs": 0}, {"score": 5}) == 0
    assert _score_equipo({}, {"score": 3}) == 3
    assert _score_equipo({}, {}) == 0


def test_no_ganador_en_vivo():
    juego = {
        "estado": "EN VIVO",
        "ganador": None,
        "visitante": "Yankees",
        "home": "Red Sox",
        "scoreAway": 5,
        "scoreHome": 1,
    }
    assert _juego_finalizado(juego) is False
    assert _ganador_oficial(juego) == ""


def test_ganador_oficial_final():
    juego = {
        "estado": "FINALIZADO",
        "ganador": "Boston Red Sox",
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "scoreAway": 2,
        "scoreHome": 3,
    }
    assert _ganador_oficial(juego) == "boston red sox"


def test_revertir_prematura():
    apuesta = {
        "pick": "New York Yankees ML",
        "estado": "ganada",
        "odds": 2.0,
        "profit": 5.0,
        "marcador_final": "x",
    }
    juego = {"id": "1", "estado": "EN VIVO", "ganador": None}
    assert _revertir_liquidacion_prematura(apuesta, juego) is True
    assert apuesta["estado"] == "pendiente"


def test_liquidar_solo_al_final():
    apuesta = {
        "pick": "Boston Red Sox ML",
        "estado": "pendiente",
        "odds": 2.0,
        "profit": None,
    }
    vivo = {
        "id": "9",
        "estado": "EN VIVO",
        "ganador": None,
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "scoreAway": 0,
        "scoreHome": 4,
    }
    assert liquidar_apuesta(apuesta, vivo, 5.0) is False
    assert apuesta["estado"] == "pendiente"

    final = {
        **vivo,
        "estado": "FINALIZADO",
        "ganador": "New York Yankees",
        "scoreAway": 5,
        "scoreHome": 4,
    }
    assert liquidar_apuesta(apuesta, final, 5.0) is True
    assert apuesta["estado"] == "perdida"


def test_no_revertir_final_sin_ganador_momentaneo():
    apuesta = {
        "pick": "Boston Red Sox ML",
        "estado": "ganada",
        "odds": 2.0,
        "profit": 5.0,
        "marcador_final": "x",
    }
    final_sin_winner = {
        "id": "2",
        "estado": "FINALIZADO",
        "ganador": None,
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "scoreAway": 2,
        "scoreHome": 2,
    }
    assert _revertir_liquidacion_prematura(apuesta, final_sin_winner) is False
    assert apuesta["estado"] == "ganada"


def test_probs_suman_100():
    from modelo_mlb import prob_logistica
    a, h = prob_logistica(10.0, 12.0)
    assert abs((a + h) - 100.0) < 0.2


def test_cuota_desde_prob_no_fija_15():
    from modelo_mlb import cuota_desde_prob
    dec, amer = cuota_desde_prob(54.0)
    assert dec > 1.5
    assert abs(dec - (100.0 / 54.0)) < 0.05


def test_prediccion_no_apostable_usa_cuota_justa():
    """Sin mercado, picks bajo min_prob no deben quedar en odds=1.5."""
    from modelo_mlb import cuota_desde_prob

    # Simula rama else de analizar_juego
    prob = 54.3
    dec, amer = cuota_desde_prob(prob)
    assert abs(dec - 1.5) > 0.2
    assert amer != 150 or dec >= 2.0


def test_reparar_odds_papel_default_roto():
    from servidor_mlb import reparar_odds_papel

    memoria = {
        "stake_por_juego": 5.125,
        "capital": 100.0,
        "capital_inicial": 100.0,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-07-20",
                "apuestas": [],
                "predicciones": [
                    {
                        "game_id": "1",
                        "pick": "Boston Red Sox ML",
                        "odds": 1.5,
                        "odds_american": 150,
                        "probPick": 54.0,
                        "estado": "liquidado",
                        "resultado": "acierto",
                        "stake_virtual": 5.0,
                        "profit": 2.5,
                    }
                ],
                "resumen": {},
            }
        ],
    }
    n = reparar_odds_papel(memoria, persistir=False)
    assert n >= 1
    pred = memoria["dias"][0]["predicciones"][0]
    assert abs(pred["odds"] - (100.0 / 54.0)) < 0.05
    assert abs(pred["profit"] - round(5.0 * (pred["odds"] - 1), 2)) < 0.01
    assert abs(float(memoria["stake_por_juego"]) - 5.0) < 0.01


if __name__ == "__main__":
    test_score_cero_no_se_pierde()
    test_no_ganador_en_vivo()
    test_ganador_oficial_final()
    test_revertir_prematura()
    test_liquidar_solo_al_final()
    test_no_revertir_final_sin_ganador_momentaneo()
    test_probs_suman_100()
    test_cuota_desde_prob_no_fija_15()
    test_prediccion_no_apostable_usa_cuota_justa()
    test_reparar_odds_papel_default_roto()
    print("OK: tests de liquidación pasaron")
