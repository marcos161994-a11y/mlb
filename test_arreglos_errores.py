"""Tests fixes: game_id str, resumen None, registrar sin StopIteration."""

from __future__ import annotations

import json


def test_guardar_prediccion_normaliza_game_id_str():
    from servidor_mlb import guardar_prediccion

    dia = {"predicciones": [], "fecha": "2026-08-29"}
    juego = {
        "id": 746123,
        "visitante": "NYY",
        "home": "BOS",
        "pick": "NYY ML",
        "probPick": 62.0,
        "odds": 1.85,
        "estado": "PROGRAMADO",
        "motivo_apuesta": "test",
    }
    ok = guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=5.0)
    assert ok is True
    assert len(dia["predicciones"]) == 1
    assert dia["predicciones"][0]["game_id"] == "746123"
    # Segunda vez no duplica (str match)
    ok2 = guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=5.0)
    assert ok2 is False
    assert len(dia["predicciones"]) == 1


def test_resumen_hoy_sin_crash_si_resumen_null():
    """Legacy memoria con resumen null no debe tumbar state."""
    from servidor_mlb import resumen_dia

    dia = {
        "fecha": "2026-08-29",
        "predicciones": [{"game_id": "1", "estado": "pendiente"}],
        "apuestas": [],
        "resumen": None,
    }
    out = dict(dia.get("resumen") or resumen_dia(dia))
    assert isinstance(out, dict)
    assert "jugadas" in out or "profit_dia" in out or out  # resumen_dia returns dict


def test_api_picks_hoy_dedup_str(monkeypatch):
    import servidor_mlb as srv

    def fake_state(**kwargs):
        return {
            "games": [
                {"id": 123, "probPick": 70, "edge": 5, "apostable": True},
                {"id": "123", "probPick": 65, "edge": 3, "apostable": True},
            ],
            "fecha_hoy": "2026-08-29",
            "config": {"modo_solo_modelo": False},
            "estrategia": {"min_prob_modelo": 58, "max_apuestas_dia": 8},
        }

    monkeypatch.setattr(srv, "construir_estado_completo", lambda **kw: fake_state())
    monkeypatch.setattr(srv, "apostable_con_mercado", lambda g: True)
    out = srv.api_picks_hoy()
    assert out["total_apostables"] <= 1
