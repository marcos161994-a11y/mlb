"""Tests combo cuotas: multi-book, retry T-45/T-30, upgrade predicción."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import servidor_mlb as srv


TZ = ZoneInfo("America/Puerto_Rico")


def test_config_multi_book():
    cfg = json.loads(Path("config_experimento.json").read_text(encoding="utf-8"))
    books = (cfg.get("lineas") or {}).get("bookmakers") or ""
    assert "draftkings" in books
    assert "pinnacle" in books or "fanduel" in books
    retries = (cfg.get("lineas") or {}).get("minutos_retry_cuotas")
    assert 45 in retries and 30 in retries


def test_minutos_retry_cuotas_default():
    out = srv._minutos_retry_cuotas({"minutos_antes_juego": 60, "lineas": {}})
    assert out == [45, 30]


def test_minutos_retry_cuotas_respeta_t60():
    out = srv._minutos_retry_cuotas(
        {"minutos_antes_juego": 60, "lineas": {"minutos_retry_cuotas": [60, 45, 30, 10]}}
    )
    assert 60 not in out
    assert out == [45, 30, 10]


def test_actualizar_mercado_en_prediccion():
    existente = {
        "pick": "Yankees ML",
        "probPick": 58.0,
        "lineas_fuente": "modelo",
        "odds": 1.61,
        "edge": 0,
        "apostable": False,
        "visitante": "Boston Red Sox",
        "home": "New York Yankees",
    }
    juego = {
        "visitante": "Boston Red Sox",
        "home": "New York Yankees",
        "lineas_fuente": "draftkings",
        "odds_away_decimal": 2.10,
        "odds_home_decimal": 2.05,
        "odds_home_american": 105,
        "odds": 2.05,
    }
    cfg = {
        "estrategia": {"min_edge_pct": 6.0, "min_prob_modelo": 58.0},
        "modo_solo_modelo": False,
    }
    ok = srv.actualizar_mercado_en_prediccion(existente, juego, cfg)
    assert ok is True
    assert existente["lineas_fuente"] == "draftkings"
    assert existente["odds"] == 2.05
    assert existente["edge"] > 0
    assert existente["apostable"] is True


def test_actualizar_mercado_no_pisa_casa_existente():
    existente = {
        "pick": "Yankees ML",
        "probPick": 62.0,
        "lineas_fuente": "draftkings",
        "odds": 1.72,
        "edge": 4.0,
        "apostable": False,
    }
    juego = {
        "visitante": "Boston Red Sox",
        "home": "New York Yankees",
        "lineas_fuente": "fanduel",
        "odds_home_decimal": 1.65,
    }
    ok = srv.actualizar_mercado_en_prediccion(existente, juego, {})
    assert ok is False
    assert existente["lineas_fuente"] == "draftkings"


def test_lineas_para_panel_incluye_retries():
    srv._lineas_meta_cache = {"ok": True, "partidos": 5}
    out = srv._lineas_para_panel(
        {
            "lineas": {
                "bookmakers": "pinnacle,draftkings",
                "minutos_retry_cuotas": [45, 30],
            },
            "minutos_antes_juego": 60,
        }
    )
    assert out["bookmakers"] == "pinnacle,draftkings"
    assert out["minutos_retry_cuotas"] == [45, 30]


def test_programar_bloqueos_crea_retry_jobs(monkeypatch):
    inicio = datetime.now(TZ) + timedelta(hours=3)
    bloqueo = inicio - timedelta(minutes=60)
    juego = {
        "id": "777001",
        "estado": "PROGRAMADO",
        "visitante": "A",
        "home": "B",
        "inicio_juego": inicio.isoformat(),
        "hora_bloqueo": bloqueo.isoformat(),
        "hora_bloqueo_txt": bloqueo.strftime("%I:%M %p"),
        "hora_inicio_txt": inicio.strftime("%I:%M %p"),
    }
    monkeypatch.setattr(srv, "obtener_juegos_fecha", lambda *_a, **_k: [juego])
    monkeypatch.setattr(srv, "cargar_config", lambda: {
        "timezone": "America/Puerto_Rico",
        "minutos_antes_juego": 60,
        "lineas": {"minutos_retry_cuotas": [45, 30]},
    })
    monkeypatch.setattr(srv, "ahora_simulado", lambda: datetime.now(TZ))

    added = []

    class FakeScheduler:
        def get_jobs(self):
            return []

        def remove_job(self, _jid):
            pass

        def add_job(self, func, trigger, id, replace_existing=True):
            added.append(id)

    monkeypatch.setattr(srv, "scheduler", FakeScheduler())
    srv.programar_bloqueos_por_juego()
    assert "bloqueo_juego_777001" in added
    assert "cuotas_retry_777001_45" in added
    assert "cuotas_retry_777001_30" in added


def test_refrescar_cuotas_pendientes_upgrade_modelo(tmp_path, monkeypatch):
    gid = "888001"
    fecha = srv.fecha_str()
    memoria = {
        "capital": 100.0,
        "capital_inicial": 100.0,
        "stake_por_juego": 3.0,
        "dia_actual": 1,
        "dias_totales": 200,
        "dias": [
            {
                "dia": 1,
                "fecha": fecha,
                "predicciones": [
                    {
                        "game_id": gid,
                        "pick": "Team A ML",
                        "probPick": 60.0,
                        "odds": 1.67,
                        "lineas_fuente": "modelo",
                        "edge": 0,
                        "apostable": False,
                        "motivo_apuesta": "Modelo 60% sin cuota real",
                        "visitante": "Team A",
                        "home": "Team B",
                    }
                ],
                "apuestas": [],
            }
        ],
    }
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "memoria_auditoria.json")
    srv._invalidar_cache_memoria()
    srv.guardar_memoria(memoria)

    juego = {
        "id": gid,
        "estado": "PROGRAMADO",
        "visitante": "Team A",
        "home": "Team B",
        "lineas_fuente": "draftkings",
        "odds_away_decimal": 2.10,
        "odds_away_american": 110,
        "odds_home_decimal": 1.75,
        "odds_home_american": -133,
    }

    cfg = {
        "timezone": "America/Puerto_Rico",
        "temporada_mlb": 2026,
        "estrategia": {"min_edge_pct": 6, "min_prob_modelo": 58},
    }
    monkeypatch.setattr(srv, "cargar_config", lambda: cfg)
    monkeypatch.setattr(srv, "_mercado_requiere_cuotas", lambda _cfg: True)
    monkeypatch.setattr(srv, "obtener_juegos_fecha", lambda *_a, **_k: [juego])
    monkeypatch.setattr(srv, "bloquear_juego", lambda *_a, **_k: {"ok": False})

    out = srv.refrescar_cuotas_pendientes_hoy()
    assert out["actualizados"] == 1

    pred = srv.cargar_memoria()["dias"][0]["predicciones"][0]
    assert pred["lineas_fuente"] == "draftkings"
    assert pred.get("cuota_retry") is True
    assert float(pred["edge"]) > 0
