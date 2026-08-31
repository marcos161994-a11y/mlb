"""Tests aprendizaje amplio: señales, lecciones, pesos, calibración."""

from aprendizaje_mlb import (
    TIPO_ACIERTO,
    calcular_movimiento_linea,
    extraer_senales_aprendizaje,
    lecciones_seleccionadas_para_prompt,
    peso_muestra_aprendizaje,
    segmento_calibracion,
)
from ia_lecciones import registrar_leccion_desde_acierto, texto_lecciones_para_prompt
from mente_aprendizaje import actualizar_stats_tras_liquidar, senales_de_pred


def test_senales_mc_over_y_favorito_inflado():
    pred = {
        "pick": "Team A ML",
        "visitante": "Team A",
        "home": "Team B",
        "probPick": 63,
        "edge": 10,
        "odds": 1.8,
        "lineas_fuente": "draftkings",
        "inteligencia": {"ok": True, "totales": {"ok": True, "señal": "over"}, "preferir_f5": True},
        "preferir_f5": True,
    }
    cfg = {"estrategia": {"favorito_inflado": {"activo": True, "umbral_prob": 62, "min_edge_pct": 15}}}
    s = extraer_senales_aprendizaje(pred, cfg=cfg)
    assert "mc_over" in s
    assert "preferir_f5" in s
    assert "favorito_inflado" in s


def test_peso_dinero_triple():
    assert peso_muestra_aprendizaje({"con_dinero": True, "lineas_fuente": "draftkings"}) == 3.0
    assert peso_muestra_aprendizaje({"lineas_fuente": "draftkings"}) == 1.0
    assert peso_muestra_aprendizaje({"lineas_fuente": "modelo"}) == 0.5
    assert peso_muestra_aprendizaje({"invalida_tarde": True}) == 0.0


def test_lecciones_dedup_sin_cuota():
    lec = []
    for i in range(5):
        lec.append({"patron": "sin_cuota_real", "leccion": f"x{i}", "tipo": "sin_cuota_real"})
    lec.append({"patron": "favorito_inflado", "leccion": "importante", "tipo": "fallo_postmortem"})
    sel = lecciones_seleccionadas_para_prompt(lec, max_n=4)
    patrones = [x["patron"] for x in sel]
    assert patrones.count("sin_cuota_real") <= 1
    assert "favorito_inflado" in patrones


def test_leccion_positiva_underdog():
    mem = {}
    pred = {
        "game_id": "u1",
        "estado": "liquidado",
        "resultado": "acierto",
        "pick": "Royals ML",
        "probPick": 60.7,
        "edge": 9.7,
        "odds": 2.1,
        "lineas_fuente": "draftkings",
        "valida_stats": True,
    }
    out = registrar_leccion_desde_acierto(mem, pred)
    assert out is not None
    assert out["tipo"] == TIPO_ACIERTO
    assert out["patron"] == "underdog_valor"
    txt = texto_lecciones_para_prompt(mem)
    assert "underdog_valor" in txt or "Underdog" in txt


def test_movimiento_linea_en_contra():
    reg = {"odds_congelada": 2.0}
    mov = calcular_movimiento_linea(reg, 1.85)
    assert mov is not None
    assert mov < 0
    s = extraer_senales_aprendizaje({**reg, "linea_movimiento_pct": mov, "lineas_fuente": "draftkings"})
    assert "linea_en_contra" in s


def test_stats_ponderados_mc_over():
    mem = {}
    pred = {
        "game_id": "m1",
        "estado": "liquidado",
        "resultado": "fallo",
        "pick": "X ML",
        "edge": 12,
        "probPick": 63,
        "lineas_fuente": "draftkings",
        "con_dinero": True,
        "ia_mente": {"decision": "APOSTAR", "autoriza_dinero": True, "alertas": ["mc_over"]},
        "inteligencia": {"totales": {"señal": "over"}},
    }
    actualizar_stats_tras_liquidar(mem, pred)
    p = mem["mente_stats"]["patrones"]["mc_over"]
    assert float(p["n"]) == 3.0
    assert float(p["apostar_fail"]) == 3.0


def test_segmento_calibracion_prob_alta():
    assert segmento_calibracion({"probPick": 65, "odds": 1.7}) == "prob_alta"
    assert segmento_calibracion({"probPick": 55, "odds": 2.2}) == "underdog_cuota"


def test_calcular_bias_desde_papel():
    from servidor_mlb import calcular_bias_aprendizaje

    mem = {
        "dias": [
            {
                "predicciones": [
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True, "lineas_fuente": "dk"},
                ]
            }
        ]
    }
    assert calcular_bias_aprendizaje(mem) == 0.5
