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


def test_top_n_solo_programados():
    from modelo_mlb import seleccionar_favorables_del_dia

    cfg = {"estrategia": {"max_apuestas_dia": 1}}
    juegos = [
        {"id": "1", "apostable": True, "edge": 20, "estado": "FINALIZADO"},
        {"id": "2", "apostable": True, "edge": 10, "estado": "PROGRAMADO"},
    ]
    seleccionar_favorables_del_dia(juegos, cfg)
    assert juegos[0]["apostable"] is False
    assert juegos[1]["apostable"] is True


def test_mano_pitcher_dict():
    from modelo_mlb import _normalizar_mano, ajuste_matchup_zurdo_diestro

    assert _normalizar_mano({"code": "L", "description": "Left"}) == "L"
    assert _normalizar_mano("R") == "R"
    assert ajuste_matchup_zurdo_diestro({"code": "L"}, 1.0) == -2.0


def test_ml_sin_doble_muestra():
    from ml_predictor import cargar_datos_entrenamiento_desde_memoria

    memoria = {
        "dias": [
            {
                "apuestas": [
                    {
                        "game_id": "9",
                        "estado": "ganada",
                        "probPick": 60,
                        "edge": 5,
                    }
                ],
                "predicciones": [
                    {
                        "game_id": "9",
                        "estado": "liquidado",
                        "resultado": "acierto",
                        "probPick": 60,
                        "edge": 5,
                    },
                    {
                        "game_id": "10",
                        "estado": "liquidado",
                        "resultado": "fallo",
                        "probPick": 55,
                        "edge": 2,
                    },
                ],
            }
        ]
    }
    datos = cargar_datos_entrenamiento_desde_memoria(memoria)
    assert len(datos) == 2


def test_capital_bruto_no_infla():
    from servidor_mlb import resumen_banca

    memoria = {
        "capital": 100.0,
        "capital_inicial": 100.0,
        "stake_por_juego": 5.0,
        "dia_actual": 1,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-07-21",
                "apuestas": [
                    {"estado": "pendiente", "stake": 5.0, "profit": None}
                ],
            }
        ],
    }
    b = resumen_banca(memoria)
    assert b["capital_bruto"] == 100.0
    assert b["disponible"] == 95.0


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
    test_top_n_solo_programados()
    test_mano_pitcher_dict()
    test_ml_sin_doble_muestra()
    test_capital_bruto_no_infla()
    print("OK: tests de liquidación pasaron")
