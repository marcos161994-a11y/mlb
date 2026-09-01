"""Tests selector contextual de lecciones para Groq."""

from aprendizaje_mlb import (
    lecciones_seleccionadas_para_prompt,
    perfil_spot_aprendizaje,
    relevancia_leccion_para_spot,
)
from ia_lecciones import max_lecciones_prompt, texto_lecciones_para_prompt


def test_perfil_spot_detecta_scratch_y_f5():
    juego = {
        "pick": "Team A ML",
        "visitante": "Team A",
        "home": "Team B",
        "probPick": 58,
        "edge": 8,
        "odds": 1.9,
        "lineas_fuente": "draftkings",
        "scratch_lineup": {"ok": True, "riesgo": True},
        "inteligencia": {"ok": True, "preferir_f5": True, "capas": ["bullpen", "monte_carlo"]},
    }
    perfil = perfil_spot_aprendizaje(juego)
    assert "scratch" in perfil["senales"]
    assert perfil["preferir_f5"] is True


def test_relevancia_prioriza_patron_del_spot():
    perfil = {"senales": {"scratch", "mc_over"}, "segmento": "general", "capas": {"bullpen"}, "preferir_f5": False, "mc_senal": "over"}
    alta = relevancia_leccion_para_spot({"patron": "scratch_lineup"}, perfil)
    baja = relevancia_leccion_para_spot({"patron": "underdog_valor"}, perfil)
    assert alta > baja


def test_selector_contextual_elige_scratch_sobre_generica():
    lec = [
        {"patron": "otro", "leccion": "genérica", "confianza": 5},
        {"patron": "scratch_lineup", "leccion": "scratch importa", "confianza": 3},
        {"patron": "sin_cuota_real", "leccion": "ruido", "confianza": 5},
    ]
    juego = {
        "pick": "X ML",
        "visitante": "X",
        "home": "Y",
        "probPick": 55,
        "edge": 7,
        "odds": 1.95,
        "lineas_fuente": "draftkings",
        "scratch_lineup": {"ok": True, "riesgo": True},
    }
    sel = lecciones_seleccionadas_para_prompt(lec, max_n=2, juego=juego)
    patrones = [x["patron"] for x in sel]
    assert "scratch_lineup" in patrones


def test_max_lecciones_prompt_default_20():
    assert max_lecciones_prompt({"max_lecciones_prompt": 20}) == 20


def test_texto_prompt_con_contexto():
    mem = {
        "lecciones": [
            {
                "patron": "edge_falso",
                "leccion": "edge bajo falló",
                "tipo": "fallo_postmortem",
                "confianza": 4,
            },
            {
                "patron": "underdog_valor",
                "leccion": "underdog ganó",
                "tipo": "acierto_refuerzo",
                "confianza": 4,
            },
        ]
    }
    juego = {
        "pick": "Dog ML",
        "visitante": "Dog",
        "home": "Fav",
        "probPick": 60,
        "edge": 9,
        "odds": 2.1,
        "lineas_fuente": "draftkings",
    }
    txt = texto_lecciones_para_prompt(mem, max_n=1, juego=juego)
    assert "underdog" in txt.lower() or "Underdog" in txt
