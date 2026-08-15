"""Tests vigilancia T-60 (juegos sin pick congelado cerca del inicio)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from servidor_mlb import vigilancia_t60


TZ = ZoneInfo("America/Puerto_Rico")


def _juego(gid, estado, mins_a_inicio, pick="Home ML", visitante="Away", home="Home"):
    inicio = datetime.now(TZ) + timedelta(minutes=mins_a_inicio)
    return {
        "id": gid,
        "estado": estado,
        "visitante": visitante,
        "home": home,
        "pick": pick,
        "inicio_juego": inicio.isoformat(),
        "hora_inicio_txt": inicio.strftime("%H:%M"),
    }


def test_vigilancia_ok_si_ya_congelado():
    j = _juego("1", "PROGRAMADO", 40)
    mem = {"dias": [{"fecha": "2099-01-01", "predicciones": [{"game_id": "1", "pick": "Home ML"}]}]}
    # fecha_str real != 2099 → dia_por_fecha no encuentra; forzar via predicciones del dia operativo
    # Usamos dia con fecha de hoy simulando que ya está en memoria del día actual
    from servidor_mlb import fecha_str

    mem = {
        "dias": [
            {
                "fecha": fecha_str(),
                "predicciones": [{"game_id": "1", "pick": "Home ML"}],
            }
        ]
    }
    out = vigilancia_t60([j], mem, {"minutos_antes_juego": 60, "minutos_gracia_bloqueo": 30})
    assert out["ok"] is True
    assert out["total_riesgo"] == 0
    assert out["congelados_activos"] >= 1


def test_vigilancia_alerta_sin_congelar_cerca():
    from servidor_mlb import fecha_str

    j = _juego("2", "PROGRAMADO", 45, pick="Yankees ML", visitante="Bos", home="NYY")
    mem = {"dias": [{"fecha": fecha_str(), "predicciones": []}]}
    out = vigilancia_t60(
        [j],
        mem,
        {"minutos_antes_juego": 60, "minutos_gracia_bloqueo": 30},
    )
    assert out["ok"] is False
    assert out["nivel"] == "alerta"
    assert out["total_riesgo"] == 1
    assert "Yankees" in out["mensaje"] or "Bos" in out["mensaje"] or "NYY" in out["mensaje"]


def test_vigilancia_ignora_finalizados():
    from servidor_mlb import fecha_str

    j = _juego("3", "FINALIZADO", -180)
    mem = {"dias": [{"fecha": fecha_str(), "predicciones": []}]}
    out = vigilancia_t60([j], mem, {"minutos_antes_juego": 60})
    assert out["ok"] is True
    assert out["total_riesgo"] == 0
