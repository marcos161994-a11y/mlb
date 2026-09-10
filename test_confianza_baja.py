"""Leans <58% se muestran (quién gana) pero no cuentan en precisión."""

from servidor_mlb import (
    aplicar_confianza_prediccion,
    guardar_prediccion,
    marcar_predicciones_confianza_baja,
    prediccion_valida_para_stats,
)


CFG = {"estrategia": {"min_prob_modelo": 58.0, "min_prob_stats": 58.0}}


def test_lean_51_no_cuenta():
    pred = {"probPick": 51.1, "pick": "Nationals ML", "resultado": "fallo"}
    assert aplicar_confianza_prediccion(pred, CFG) is True
    assert pred["confianza_baja"] is True
    assert pred["valida_stats"] is False
    assert prediccion_valida_para_stats(pred) is False


def test_pick_59_si_cuenta():
    pred = {"probPick": 59.4, "pick": "Red Sox ML", "resultado": "acierto"}
    assert aplicar_confianza_prediccion(pred, CFG) is False
    assert not pred.get("confianza_baja")
    assert prediccion_valida_para_stats(pred) is True


def test_backfill_separa_leans_de_tarde():
    mem = {
        "dias": [
            {
                "predicciones": [
                    {"probPick": 52.7, "pick": "Pirates ML", "resultado": "fallo"},
                    {"probPick": 71.6, "pick": "Yankees ML", "resultado": "acierto"},
                    {
                        "probPick": 64.0,
                        "pick": "Tigers ML",
                        "invalida_tarde": True,
                        "valida_stats": False,
                    },
                ]
            }
        ]
    }
    n = marcar_predicciones_confianza_baja(mem, CFG)
    assert n == 1
    lean, fuerte, tarde = mem["dias"][0]["predicciones"]
    assert lean["confianza_baja"] is True
    assert fuerte.get("confianza_baja") in (None, False)
    assert tarde.get("confianza_baja") in (None, False)
    assert tarde["invalida_tarde"] is True


def test_guardar_marca_lean(monkeypatch):
    monkeypatch.setattr(
        "servidor_mlb.cargar_config",
        lambda: {
            "minutos_gracia_bloqueo": 30,
            "stake_por_juego": 5,
            "timezone": "America/Puerto_Rico",
            "estrategia": {"min_prob_modelo": 58.0, "min_prob_stats": 58.0},
        },
    )
    monkeypatch.setattr("servidor_mlb.stake_virtual_prediccion", lambda *_a, **_k: 5.0)
    monkeypatch.setattr("servidor_mlb.apostable_con_mercado", lambda *_a, **_k: False)
    monkeypatch.setattr("servidor_mlb.generar_briefing_juego", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr("servidor_mlb.cargar_memoria", lambda: {"dias": []})

    dia = {"predicciones": []}
    juego = {
        "id": "lean1",
        "estado": "PROGRAMADO",
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 51.3,
        "odds": 1.9,
        "inicio_juego": "2026-09-12T20:00:00-04:00",
        "lineas_fuente": "draftkings",
        "edge": -1.4,
    }
    assert guardar_prediccion(dia, juego) is True
    pred = dia["predicciones"][0]
    assert pred["confianza_baja"] is True
    assert pred["valida_stats"] is False
    assert prediccion_valida_para_stats(pred) is False


def test_html_quien_gana_no_cuenta_lean():
    from pathlib import Path

    html = Path("QuantumMLB.html").read_text(encoding="utf-8")
    assert "Quién gana" in html
    assert "confianza_baja" in html
    assert "leans <58% no cuentan" in html or "leans se muestran" in html
