"""El filtro por % se deshace: todos los picks de quién gana vuelven a contar."""

from servidor_mlb import (
    guardar_prediccion,
    limpiar_predicciones_confianza_baja,
    prediccion_valida_para_stats,
)


def test_limpiar_devuelve_leans_al_wr():
    mem = {
        "dias": [
            {
                "predicciones": [
                    {
                        "probPick": 51.1,
                        "pick": "Nationals ML",
                        "resultado": "fallo",
                        "confianza_baja": True,
                        "valida_stats": False,
                        "motivo_apuesta": "Sin valor · Lean débil · se muestra quién gana, no cuenta en precisión",
                    },
                    {
                        "probPick": 71.6,
                        "pick": "Yankees ML",
                        "resultado": "acierto",
                        "valida_stats": True,
                    },
                    {
                        "probPick": 64.0,
                        "pick": "Tigers ML",
                        "invalida_tarde": True,
                        "valida_stats": False,
                        "confianza_baja": True,
                    },
                ]
            }
        ]
    }
    n = limpiar_predicciones_confianza_baja(mem)
    assert n == 2
    lean, fuerte, tarde = mem["dias"][0]["predicciones"]
    assert lean["confianza_baja"] is False
    assert lean["valida_stats"] is True
    assert prediccion_valida_para_stats(lean) is True
    assert "Lean débil" not in (lean.get("motivo_apuesta") or "")
    assert fuerte["valida_stats"] is True
    assert tarde["invalida_tarde"] is True
    assert tarde["confianza_baja"] is False


def test_guardar_51_cuenta_en_wr(monkeypatch):
    monkeypatch.setattr(
        "servidor_mlb.cargar_config",
        lambda: {
            "minutos_gracia_bloqueo": 30,
            "stake_por_juego": 5,
            "timezone": "America/Puerto_Rico",
            "estrategia": {"min_prob_modelo": 58.0},
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
    assert not pred.get("confianza_baja")
    assert pred["valida_stats"] is True
    assert prediccion_valida_para_stats(pred) is True


def test_html_sin_filtro_porcentaje():
    from pathlib import Path

    html = Path("QuantumMLB.html").read_text(encoding="utf-8")
    assert "cuentan picks ≥58%" not in html
    assert "leans <58% no cuentan" not in html
    assert "LEAN &lt;58%" not in html
    assert "Quién gana" in html
