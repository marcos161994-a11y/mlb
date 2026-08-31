"""Integración: retry cuota modelo→casa + favorito inflado → sin dinero."""

from __future__ import annotations

import json

import servidor_mlb as srv


CFG = {
    "temporada_mlb": 2026,
    "timezone": "America/Puerto_Rico",
    "minutos_antes_juego": 60,
    "modo_solo_modelo": False,
    "usar_mente": True,
    "usar_scratch_lineup": False,
    "usar_lesiones": False,
    "usar_ia_veto": False,
    "stake_por_juego": 3.0,
    "estrategia": {
        "min_edge_pct": 6.0,
        "min_prob_modelo": 58.0,
        "max_apuestas_dia": 4,
        "requiere_betmgm": True,
        "favorito_inflado": {
            "activo": True,
            "umbral_prob": 62.0,
            "min_edge_pct": 15.0,
        },
    },
    "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True, "shadow": False},
    "lineas": {"minutos_retry_cuotas": [45, 30]},
}


def _setup_memoria(tmp_path, monkeypatch, *, gid: str, fecha: str | None = None) -> dict:
    monkeypatch.setattr(srv, "cargar_config", lambda: CFG)
    fecha = fecha or srv.fecha_str()
    memoria = {
        "capital": 97.63,
        "capital_inicial": 100.0,
        "dia_actual": 17,
        "dias_totales": 200,
        "experimento_activo": True,
        "stake_por_juego": 3.0,
        "modo": "simulacion",
        "dias": [
            {
                "dia": 17,
                "fecha": fecha,
                "predicciones": [
                    {
                        "game_id": gid,
                        "pick": "San Diego Padres ML",
                        "probPick": 62.3,
                        "lineas_fuente": "modelo",
                        "odds": 1.61,
                        "edge": 0,
                        "apostable": False,
                        "visitante": "San Diego Padres",
                        "home": "Cleveland Guardians",
                        "estado": "pendiente",
                        "motivo_apuesta": "Sin cuota real de mercado",
                    }
                ],
                "apuestas": [],
            }
        ],
    }
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "memoria_auditoria.json")
    monkeypatch.setattr(srv, "MEMORIA_BACKUP_PATH", tmp_path / "memoria_auditoria_backup.json")
    srv._invalidar_cache_memoria()
    srv.guardar_memoria(memoria)
    return memoria


def test_retry_cuota_favorito_inflado_sin_dinero(tmp_path, monkeypatch):
    gid = "824400"
    fecha = srv.fecha_str()
    _setup_memoria(tmp_path, monkeypatch, gid=gid, fecha=fecha)

    juego = {
        "id": gid,
        "estado": "PROGRAMADO",
        "visitante": "San Diego Padres",
        "home": "Cleveland Guardians",
        "lineas_fuente": "draftkings",
        "odds_away_decimal": 2.05,
        "odds_away_american": 105,
    }

    monkeypatch.setattr(srv, "cargar_config", lambda: CFG)
    monkeypatch.setattr(srv, "obtener_juegos_fecha", lambda *_a, **_k: [juego])
    monkeypatch.setattr(srv, "_mercado_requiere_cuotas", lambda _cfg: True)

    bloqueos: list[str] = []

    def fake_bloquear(game_id, forzar=False):
        bloqueos.append(str(game_id))
        return {"ok": False, "motivo": "no debería llamarse"}

    monkeypatch.setattr(srv, "bloquear_juego", fake_bloquear)

    res = srv.refrescar_cuotas_juego(gid)

    assert res["ok"] is True
    assert res["actualizado"] is True
    assert res["apostable"] is False
    assert bloqueos == []

    mem = srv.cargar_memoria()
    pred = mem["dias"][0]["predicciones"][0]
    assert pred["lineas_fuente"] == "draftkings"
    assert pred.get("cuota_retry") is True
    assert pred["apostable"] is False
    assert float(pred["edge"]) < 15.0
    assert len(mem["dias"][0]["apuestas"]) == 0


def test_bloquear_juego_tras_retry_favorito_inflado_sin_apuesta(tmp_path, monkeypatch):
    gid = "824400"
    fecha = srv.fecha_str()
    _setup_memoria(tmp_path, monkeypatch, gid=gid, fecha=fecha)

    juego = {
        "id": gid,
        "estado": "PROGRAMADO",
        "visitante": "San Diego Padres",
        "home": "Cleveland Guardians",
        "pick": "San Diego Padres ML",
        "probPick": 62.3,
        "lineas_fuente": "draftkings",
        "odds_away_decimal": 2.05,
        "odds_away_american": 105,
        "odds": 2.05,
        "edge": 13.5,
        "apostable": False,
        "motivo_apuesta": "Favorito inflado — solo papel",
    }

    monkeypatch.setattr(srv, "cargar_config", lambda: CFG)
    monkeypatch.setattr(srv, "obtener_juegos_fecha", lambda *_a, **_k: [juego])

    res = srv.bloquear_juego(gid)

    assert res["ok"] is False
    assert res.get("prediccion_guardada") is True
    mem = srv.cargar_memoria()
    assert len(mem["dias"][0]["apuestas"]) == 0


def test_actualizar_mercado_upgrade_modelo_a_draftkings():
    existente = {
        "pick": "San Diego Padres ML",
        "probPick": 62.3,
        "lineas_fuente": "modelo",
        "odds": 1.61,
        "edge": 0,
        "apostable": False,
        "visitante": "San Diego Padres",
        "home": "Cleveland Guardians",
    }
    juego = {
        "visitante": "San Diego Padres",
        "home": "Cleveland Guardians",
        "lineas_fuente": "draftkings",
        "odds_away_decimal": 2.05,
        "odds_away_american": 105,
    }
    ok = srv.actualizar_mercado_en_prediccion(existente, juego, CFG)
    assert ok is True
    assert existente["lineas_fuente"] == "draftkings"
    assert existente.get("cuota_retry") is True
    assert existente["apostable"] is False
    assert "favorito" in (existente.get("motivo_apuesta") or "").lower() or float(existente["edge"]) < 15.0
