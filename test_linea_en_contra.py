"""Veto en vivo: línea en contra (movimiento cuota vs congelada)."""

from __future__ import annotations

import json
from pathlib import Path

import servidor_mlb as srv
from aprendizaje_mlb import bloqueado_linea_en_contra
from mente_mlb import mente_conclusion


CFG = {
    "timezone": "America/Puerto_Rico",
    "temporada_mlb": 2026,
    "usar_mente": True,
    "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    "stake_por_juego": 3,
    "estrategia": {
        "min_edge_pct": 6.0,
        "min_prob_modelo": 58.0,
        "favorito_inflado": {"activo": True, "umbral_prob": 62.0, "min_edge_pct": 15.0},
        "linea_en_contra": {
            "activo": True,
            "umbral_pct": 5.0,
            "min_edge_excepcion_pct": 18.0,
        },
    },
}


def test_bloqueado_linea_en_contra_basico():
    reg = {"linea_movimiento_pct": -7.5, "edge": 8.0}
    ok, msg = bloqueado_linea_en_contra(reg, CFG)
    assert ok is True
    assert "línea en contra" in msg.lower() or "Línea en contra" in msg


def test_permite_edge_excepcional():
    reg = {"linea_movimiento_pct": -8.0, "edge": 19.0}
    ok, _ = bloqueado_linea_en_contra(reg, CFG)
    assert ok is False


def test_permite_movimiento_a_favor():
    reg = {"linea_movimiento_pct": 3.0, "edge": 10.0}
    ok, _ = bloqueado_linea_en_contra(reg, CFG)
    assert ok is False


def test_actualizar_mercado_bloquea_linea_en_contra():
    existente = {
        "pick": "Yankees ML",
        "probPick": 62.0,
        "lineas_fuente": "draftkings",
        "odds": 2.05,
        "odds_congelada": 2.05,
        "edge": 12.0,
        "apostable": True,
        "visitante": "Red Sox",
        "home": "Yankees",
    }
    juego = {
        "visitante": "Red Sox",
        "home": "Yankees",
        "lineas_fuente": "draftkings",
        "odds_home_decimal": 1.85,
        "odds_home_american": -118,
    }
    ok = srv.actualizar_mercado_en_prediccion(existente, juego, CFG)
    assert ok is True
    assert float(existente["linea_movimiento_pct"]) <= -5
    assert existente["apostable"] is False
    assert "línea en contra" in (existente.get("motivo_apuesta") or "").lower()


def test_mente_pasa_linea_en_contra():
    j = {
        "id": "x1",
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 60.0,
        "edge": 9.0,
        "odds": 1.85,
        "lineas_fuente": "draftkings",
        "linea_movimiento_pct": -8.0,
    }
    c = mente_conclusion(j, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert c["autoriza_dinero"] is False
    assert any("línea" in r.lower() for r in c.get("razones") or [])


def test_retry_sin_dinero_linea_en_contra(tmp_path, monkeypatch):
    gid = "9001"
    fecha = srv.fecha_str()
    memoria = {
        "capital": 100.0,
        "capital_inicial": 100.0,
        "dia_actual": 1,
        "dias_totales": 200,
        "experimento_activo": True,
        "stake_por_juego": 3.0,
        "modo": "simulacion",
        "dias": [
            {
                "dia": 1,
                "fecha": fecha,
                "predicciones": [
                    {
                        "game_id": gid,
                        "pick": "Yankees ML",
                        "probPick": 62.0,
                        "lineas_fuente": "draftkings",
                        "odds": 2.05,
                        "odds_congelada": 2.05,
                        "edge": 12.0,
                        "apostable": True,
                        "visitante": "Red Sox",
                        "home": "Yankees",
                        "estado": "pendiente",
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

    juego = {
        "id": gid,
        "estado": "PROGRAMADO",
        "visitante": "Red Sox",
        "home": "Yankees",
        "lineas_fuente": "draftkings",
        "odds_home_decimal": 1.85,
        "odds_home_american": -118,
    }
    monkeypatch.setattr(srv, "cargar_config", lambda: CFG)
    monkeypatch.setattr(srv, "obtener_juegos_fecha", lambda *_a, **_k: [juego])
    monkeypatch.setattr(srv, "_mercado_requiere_cuotas", lambda _cfg: True)
    bloqueos = []
    monkeypatch.setattr(srv, "bloquear_juego", lambda g, forzar=False: bloqueos.append(g) or {"ok": False})

    res = srv.refrescar_cuotas_juego(gid)
    assert res["actualizado"] is True
    assert res["apostable"] is False
    assert bloqueos == []
    pred = srv.cargar_memoria()["dias"][0]["predicciones"][0]
    assert pred["apostable"] is False


def test_config_linea_en_contra_presente():
    cfg = json.loads(Path("config_experimento.json").read_text(encoding="utf-8"))
    le = (cfg.get("estrategia") or {}).get("linea_en_contra") or {}
    assert le.get("activo") is True
    assert float(le.get("umbral_pct") or 0) == 5.0
