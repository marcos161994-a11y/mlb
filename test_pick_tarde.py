"""Tests: picks tardíos / EN VIVO no cuentan en precisión."""

from servidor_mlb import prediccion_valida_para_stats, marcar_predicciones_tardias


def test_yankees_tarde_invalido():
    pred = {
        "pick": "New York Yankees ML",
        "inicio_juego": "2026-07-31T14:20:00-04:00",
        "predicho_en": "2026-07-31T15:32:16-04:00",
        "resultado": "acierto",
        "estado": "liquidado",
    }
    assert prediccion_valida_para_stats(pred) is False


def test_pick_pre_inicio_valido():
    pred = {
        "pick": "Dodgers ML",
        "inicio_juego": "2026-07-31T22:10:00-04:00",
        "predicho_en": "2026-07-31T21:10:00-04:00",
        "resultado": "acierto",
        "estado": "liquidado",
    }
    assert prediccion_valida_para_stats(pred) is True


def test_motivo_en_vivo_invalido():
    pred = {
        "pick": "White Sox ML",
        "motivo_apuesta": "Juego EN VIVO",
        "inicio_juego": "2026-07-30T14:10:00-04:00",
        "predicho_en": "2026-07-30T14:05:00-04:00",
    }
    assert prediccion_valida_para_stats(pred) is False


def test_marcar_memoria():
    mem = {
        "dias": [
            {
                "predicciones": [
                    {
                        "pick": "A ML",
                        "inicio_juego": "2026-07-31T14:00:00-04:00",
                        "predicho_en": "2026-07-31T16:00:00-04:00",
                    }
                ]
            }
        ]
    }
    n = marcar_predicciones_tardias(mem)
    assert n == 1
    assert mem["dias"][0]["predicciones"][0]["invalida_tarde"] is True
    assert mem["dias"][0]["predicciones"][0]["valida_stats"] is False
