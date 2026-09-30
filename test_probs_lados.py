"""probAway/probHome en el panel: 1 decimal, suman ~100 y el lado del pick es probPick."""
from __future__ import annotations

from pathlib import Path

import servidor_mlb as srv
from modelo_mlb import publicar_probs_lados


def _lado(juego: dict) -> float:
    pick = juego["pick"]
    if juego["visitante"] in pick and juego["home"] not in pick:
        return float(juego["probAway"])
    return float(juego["probHome"])


def test_publicar_alinea_el_lado_del_pick_y_no_toca_la_apuesta():
    juego = {
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick": "New York Yankees ML",
        "probPick": 50.1,
        "probAway": 50.14,
        "probHome": 49.86,
        "odds": 1.91,
        "edge": 4.2,
        "stake": 3,
        "apostable": True,
    }
    publicar_probs_lados(juego)
    assert juego["probAway"] == juego["probPick"]
    assert abs(juego["probAway"] + juego["probHome"] - 100) < 0.15
    assert juego["odds"] == 1.91
    assert juego["edge"] == 4.2
    assert juego["stake"] == 3
    assert juego["apostable"] is True
    assert juego["probPick"] == 50.1


def test_publicar_lado_local():
    juego = {
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick": "Boston Red Sox ML",
        "pick_lado": "home",
        "probPick": 55.5,
        "probAway": 40.0,
        "probHome": 60.0,
    }
    publicar_probs_lados(juego)
    assert juego["probHome"] == juego["probPick"]
    assert abs(juego["probAway"] + juego["probHome"] - 100) < 0.15


def test_guardar_prediccion_arrastra_probs():
    dia = {"predicciones": [], "fecha": "2026-09-30"}
    juego = {
        "id": 746123,
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick": "Boston Red Sox ML",
        "pick_lado": "home",
        "probPick": 62.3,
        "probAway": 37.7,
        "probHome": 62.3,
        "odds": 1.85,
        "odds_american": -118,
        "estado": "PROGRAMADO",
        "motivo_apuesta": "test",
        "stake": 3,
    }
    assert srv.guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=3.0) is True
    pred = dia["predicciones"][0]
    assert pred["probHome"] == pred["probPick"]
    assert abs(pred["probAway"] + pred["probHome"] - 100) < 0.15
    assert pred["odds"] == 1.85
    assert pred["stake_virtual"] == 3.0
    assert pred["probPick"] == 62.3


def _memoria(pred: dict | None = None, apuesta: dict | None = None) -> dict:
    return {
        "stake_por_juego": 3,
        "dia_actual": 1,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-09-30",
                "predicciones": [pred] if pred else [],
                "apuestas": [apuesta] if apuesta else [],
            }
        ],
    }


def _vivo(**extra) -> dict:
    base = {
        "id": "9",
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick": "Boston Red Sox ML",
        "pick_lado": "home",
        "probPick": 61.0,
        "probAway": 39.0,
        "probHome": 61.0,
        "odds": 2.1,
        "estado": "PROGRAMADO",
        "stake": 9,
    }
    base.update(extra)
    return base


def test_juegos_hoy_conserva_probs_del_modelo_en_vivo(monkeypatch):
    monkeypatch.setattr(srv, "fecha_str", lambda: "2026-09-30")
    monkeypatch.setattr(srv, "cargar_config", lambda: {"usar_mente": False})
    vivo = _vivo(
        pick="New York Yankees ML",
        pick_lado="away",
        probPick=50.1,
        probAway=50.14,
        probHome=49.8,
        odds=1.91,
        stake=3,
    )
    fusion = srv.fusionar_apuestas_con_juegos([vivo], _memoria())
    panel = srv._juegos_para_panel(fusion)
    juego = panel[0]
    assert juego["probAway"] == juego["probPick"] == 50.1
    assert abs(juego["probAway"] + juego["probHome"] - 100) < 0.15
    assert juego["odds"] == 1.91
    assert juego["stake"] == 3


def test_pick_congelado_muestra_las_probs_guardadas_no_las_vivas(monkeypatch):
    monkeypatch.setattr(srv, "fecha_str", lambda: "2026-09-30")
    monkeypatch.setattr(srv, "cargar_config", lambda: {"usar_mente": False})
    pred = {
        "game_id": "9",
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick": "New York Yankees ML",
        "pick_lado": "away",
        "probPick": 50.1,
        "probAway": 50.1,
        "probHome": 49.9,
        "odds": 1.91,
        "edge": 3.0,
    }
    fusion = srv.fusionar_apuestas_con_juegos([_vivo()], _memoria(pred))
    panel = srv._juegos_para_panel(fusion)
    juego = panel[0]
    assert juego["pick"] == "New York Yankees ML"
    assert juego["probPick"] == 50.1
    assert _lado(juego) == juego["probPick"]
    assert abs(juego["probAway"] + juego["probHome"] - 100) < 0.15
    assert juego["odds"] == 1.91
    assert juego["stake"] == 3


def test_apuesta_congelada_usa_el_par_guardado(monkeypatch):
    monkeypatch.setattr(srv, "fecha_str", lambda: "2026-09-30")
    monkeypatch.setattr(srv, "cargar_config", lambda: {"usar_mente": False})
    apuesta = {
        "game_id": "9",
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick": "Boston Red Sox ML",
        "pick_lado": "home",
        "probPick": 55.5,
        "probAway": 44.5,
        "probHome": 55.5,
        "odds": 1.8,
        "stake": 3,
        "estado": "pendiente",
        "edge": 2.0,
    }
    fusion = srv.fusionar_apuestas_con_juegos([_vivo()], _memoria(apuesta=apuesta))
    juego = srv._juegos_para_panel(fusion)[0]
    assert juego["probHome"] == juego["probPick"] == 55.5
    assert abs(juego["probAway"] + juego["probHome"] - 100) < 0.15
    assert juego["stake"] == 3
    assert juego["odds"] == 1.8


def test_congelado_viejo_sin_probs_no_hereda_el_modelo_vivo(monkeypatch):
    monkeypatch.setattr(srv, "fecha_str", lambda: "2026-09-30")
    monkeypatch.setattr(srv, "cargar_config", lambda: {"usar_mente": False})
    pred = {
        "game_id": "9",
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick": "New York Yankees ML",
        "probPick": 50.1,
        "odds": 1.91,
    }
    fusion = srv.fusionar_apuestas_con_juegos([_vivo()], _memoria(pred))
    juego = srv._juegos_para_panel(fusion)[0]
    assert "probAway" not in juego
    assert "probHome" not in juego
    assert juego["probPick"] == 50.1
    assert juego["odds"] == 1.91


def test_api_predicciones_incluye_el_par(monkeypatch):
    mem = _memoria(
        {
            "game_id": "9",
            "visitante": "New York Yankees",
            "home": "Boston Red Sox",
            "pick": "Boston Red Sox ML",
            "probPick": 55.5,
            "probAway": 44.5,
            "probHome": 55.5,
        }
    )
    mem["capital"] = 100.0
    monkeypatch.setattr(srv, "cargar_memoria", lambda: mem)
    monkeypatch.setattr(srv, "fecha_str", lambda: "2026-09-30")
    out = srv.api_predicciones()
    pred = out["predicciones_hoy"][0]
    assert pred["probHome"] == pred["probPick"]
    assert abs(pred["probAway"] + pred["probHome"] - 100) < 0.15
    vieja = out["historial"][0]["predicciones"][0]
    assert vieja["probHome"] == vieja["probPick"]


def test_panel_oculta_la_linea_si_faltan_probs():
    html = Path(__file__).resolve().parent.joinpath("QuantumMLB.html").read_text(encoding="utf-8")
    assert "j.probAway||'?'" not in html
    assert "j.probHome||'?'" not in html
    assert "a == null || h == null" in html
    assert "Modelo ${na.toFixed(1)}% / ${nh.toFixed(1)}%" in html
