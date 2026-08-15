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


def test_guardar_no_crea_en_vivo():
    from servidor_mlb import guardar_prediccion

    dia = {"predicciones": []}
    juego = {
        "id": "999",
        "estado": "EN VIVO",
        "visitante": "Twins",
        "home": "Mariners",
        "pick": "Twins ML",
        "probPick": 63.4,
        "odds": 2.0,
        "inicio_juego": "2026-08-01T16:10:00-04:00",
    }
    assert guardar_prediccion(dia, juego) is False
    assert dia["predicciones"] == []


def test_guardar_en_vivo_con_gracia(monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import servidor_mlb as s

    tz = ZoneInfo("America/Puerto_Rico")
    inicio = datetime.now(tz) - timedelta(minutes=12)
    monkeypatch.setattr(
        s,
        "cargar_config",
        lambda: {
            "minutos_gracia_bloqueo": 30,
            "stake_por_juego": 5,
            "timezone": "America/Puerto_Rico",
        },
    )
    monkeypatch.setattr(s, "stake_virtual_prediccion", lambda *_a, **_k: 5.0)
    monkeypatch.setattr(s, "apostable_con_mercado", lambda *_a, **_k: False)
    monkeypatch.setattr(s, "tiene_cuota_mercado", lambda *_a, **_k: False)
    monkeypatch.setattr(s, "generar_briefing_juego", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(s, "cargar_memoria", lambda: {"dias": []})

    dia = {"predicciones": []}
    juego = {
        "id": "grace1",
        "estado": "EN VIVO",
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 60,
        "odds": 1.9,
        "inicio_juego": inicio.isoformat(),
        "lineas_fuente": "draftkings",
    }
    assert s.guardar_prediccion(dia, juego, permitir_gracia=True) is True
    assert len(dia["predicciones"]) == 1
    assert dia["predicciones"][0]["congelado_en_gracia"] is True


def test_guardar_no_crea_finalizado():
    from servidor_mlb import guardar_prediccion

    dia = {"predicciones": []}
    juego = {
        "id": "fin1",
        "estado": "FINALIZADO",
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 60,
        "odds": 1.9,
        "inicio_juego": "2026-08-14T14:00:00-04:00",
    }
    assert guardar_prediccion(dia, juego, permitir_gracia=True) is False
    assert dia["predicciones"] == []
