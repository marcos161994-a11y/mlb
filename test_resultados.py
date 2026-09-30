"""Cálculo de la curva, el ROI y los cortes del panel de resultados."""

from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import servidor_mlb as srv
from resultados_mlb import calcular_resultados, clasificar_fuente_cuota


@asynccontextmanager
async def _sin_motor(_app):
    yield


def _memoria() -> dict:
    return {
        "capital_inicial": 100,
        "stake_por_juego": 3,
        "dias": [
            {
                "fecha": "2026-06-02",
                "dia": 2,
                "apuestas": [
                    {
                        "estado": "perdida",
                        "profit": -3,
                        "stake": 3,
                        "tipo_pick": "favorito_alto",
                        "lineas_fuente": "modelo",
                    },
                    {
                        "estado": "ganada",
                        "profit": 6,
                        "stake": 3,
                        "tipo_pick": "f5_especial",
                    },
                ],
                "predicciones": [],
            },
            {
                "fecha": "2026-06-01",
                "dia": 1,
                "apuestas": [
                    {
                        "estado": "ganada",
                        "profit": 4.5,
                        "stake": 3,
                        "tipo_pick": "scratch",
                        "lineas_fuente": "draftkings",
                    },
                    {
                        "estado": "perdida",
                        "profit": -3,
                        "stake": 3,
                        "tipo_pick": "underdog",
                        "fuente_momio": "estimado",
                        "lineas_fuente": "draftkings",
                    },
                    {
                        "estado": "pendiente",
                        "stake": 3,
                        "tipo_pick": "limpio",
                        "lineas_fuente": "espn",
                    },
                    {
                        "estado": "ganada",
                        "profit": 1,
                        "stake": 3,
                        "tipo_pick": "limpio",
                        "fuente_momio": "  ",
                        "lineas_fuente": "BetMGM",
                    },
                ],
                "predicciones": [
                    {
                        "estado": "liquidado",
                        "resultado": "acierto",
                        "profit": 3,
                        "stake_virtual": 3,
                        "ia_mente": {"decision": "apostar"},
                    },
                    {
                        "estado": "liquidado",
                        "resultado": "fallo",
                        "profit": -3,
                        "stake_virtual": 3,
                        "ia_mente": {"decision": "PASAR"},
                    },
                    {
                        "estado": "liquidado",
                        "resultado": "acierto",
                        "profit": 9,
                        "stake_virtual": 3,
                        "invalida_tarde": True,
                        "ia_mente": {"decision": "APOSTAR"},
                    },
                    {
                        "estado": "liquidado",
                        "resultado": "fallo",
                        "profit": -3,
                        "stake_virtual": 3,
                        "predicho_en": "2026-06-01T23:00:00+00:00",
                        "inicio_juego": "2026-06-01T20:00:00+00:00",
                        "ia_mente": {"decision": "PASAR"},
                    },
                    {
                        "estado": "liquidado",
                        "resultado": "acierto",
                        "odds": 2.5,
                        "stake_virtual": 3,
                        "ia_veto": {"decision": "ESPERAR"},
                    },
                    {
                        "estado": "pendiente",
                        "ia_mente": {"decision": "PASAR"},
                    },
                ],
            },
        ],
    }


def test_curva_roi_tipos_racha_y_drawdown():
    memoria = _memoria()
    antes = copy.deepcopy(memoria)
    out = calcular_resultados(memoria)
    assert memoria == antes

    assert [p["fecha"] for p in out["curva"]] == [None, "2026-06-01", "2026-06-02"]
    assert out["curva"][0]["capital"] == 100
    assert out["curva"][1]["capital"] == 102.5
    assert out["curva"][1]["profit_dia"] == 2.5
    assert out["curva"][1]["n"] == 3
    assert out["curva"][2]["capital"] == 105.5
    assert out["curva"][2]["profit_dia"] == 3.0

    res = out["resumen"]
    assert res["n"] == 5
    assert res["ganadas"] == 3
    assert res["perdidas"] == 2
    assert res["pendientes"] == 1
    assert res["record"] == "3-2"
    assert res["win_rate"] == 60.0
    assert res["profit"] == 5.5
    assert res["stake"] == 15
    assert res["roi_pct"] == 36.7
    assert res["capital"] == 105.5
    assert res["roi_banca_pct"] == 5.5
    assert out["capital_inicial"] == 100

    assert out["racha"] == {"tipo": "ganada", "n": 1, "texto": "1 ganada"}
    # 100 → 104.5 → 101.5 → 102.5 → 99.5 → 105.5. Valle 99.5 desde 104.5.
    assert out["drawdown"]["max_usd"] == 5.0
    assert out["drawdown"]["max_pct"] == 4.8
    assert out["drawdown"]["pico"] == 104.5
    assert out["drawdown"]["valle"] == 99.5

    tipos = {t["tipo"]: t for t in out["por_tipo"]}
    assert list(tipos) == ["scratch", "underdog", "favorito_alto", "limpio", "f5_especial"]
    assert tipos["scratch"]["roi_pct"] == 150.0
    assert tipos["scratch"]["record"] == "1-0"
    assert tipos["underdog"]["roi_pct"] == -100.0
    assert tipos["underdog"]["record"] == "0-1"
    assert tipos["favorito_alto"]["etiqueta"] == "Favorito"
    assert tipos["favorito_alto"]["record"] == "0-1"
    assert tipos["f5_especial"]["etiqueta"] == "F5 especial"
    assert tipos["limpio"]["record"] == "1-0"
    assert tipos["limpio"]["n"] == 1


def test_fuente_real_estimada_y_ausente():
    out = calcular_resultados(_memoria())
    fuente = out["por_fuente"]
    # scratch (draftkings) + limpio (BetMGM, fuente_momio vacío)
    assert fuente["real"]["n"] == 2
    assert fuente["real"]["profit"] == 5.5
    assert fuente["real"]["record"] == "2-0"
    # underdog estimado + favorito con lineas_fuente modelo
    assert fuente["estimado"]["n"] == 2
    assert fuente["estimado"]["profit"] == -6
    assert fuente["estimado"]["roi_pct"] == -100.0
    assert fuente["sin_dato"]["n"] == 1
    assert fuente["sin_dato"]["etiqueta"] == "Sin dato"

    assert clasificar_fuente_cuota({"fuente_momio": "estimado", "lineas_fuente": "pinnacle"}) == "estimado"
    assert clasificar_fuente_cuota({"fuente_momio": " Pinnacle "}) == "real"
    assert clasificar_fuente_cuota({"odds_source": "modelo"}) == "estimado"
    assert clasificar_fuente_cuota({"lineas_fuente": "ESPN"}) == "real"
    assert clasificar_fuente_cuota({"lineas_fuente": "modelo"}) == "estimado"
    assert clasificar_fuente_cuota({"stake": 3}) == "sin_dato"
    assert clasificar_fuente_cuota({"fuente_momio": None, "lineas_fuente": "draftkings"}) == "real"
    assert clasificar_fuente_cuota(None) == "sin_dato"


def test_mente_apost_ar_pasar_y_omite_tardias():
    out = calcular_resultados(_memoria())
    mente = out["mente"]
    assert mente["disponible"] is True
    por = {v["decision"]: v for v in mente["veredictos"]}
    assert list(por) == ["APOSTAR", "PASAR", "ESPERAR"]
    assert por["APOSTAR"]["record"] == "1-0"
    assert por["APOSTAR"]["roi_pct"] == 100.0
    assert por["PASAR"]["record"] == "0-1"
    assert por["PASAR"]["roi_pct"] == -100.0
    assert por["ESPERAR"]["profit"] == 4.5
    assert por["ESPERAR"]["roi_pct"] == 150.0


def test_racha_de_perdidas_y_vacio():
    memoria = {
        "capital_inicial": 100,
        "dias": [
            {
                "fecha": "2026-07-01",
                "apuestas": [
                    {"estado": "ganada", "profit": 2, "stake": 3, "tipo_pick": "scratch"},
                    {"estado": "perdida", "profit": -3, "stake": 3},
                    {"estado": "perdida", "profit": -3, "stake": 3},
                    {"estado": "perdida", "profit": -3, "stake": 3},
                ],
            }
        ],
    }
    out = calcular_resultados(memoria)
    assert out["racha"] == {"tipo": "perdida", "n": 3, "texto": "3 perdidas"}
    assert out["resumen"]["roi_pct"] == round(100 * (2 - 9) / 12, 1)

    vacio = calcular_resultados({})
    assert vacio["capital_inicial"] == 100
    assert vacio["curva"] == [
        {"fecha": None, "dia": 0, "capital": 100.0, "profit_dia": 0.0, "n": 0}
    ]
    assert vacio["resumen"]["n"] == 0
    assert vacio["resumen"]["roi_pct"] is None
    assert vacio["resumen"]["capital"] == 100
    assert vacio["racha"]["texto"] == "Sin racha"
    assert vacio["drawdown"]["max_usd"] == 0
    assert vacio["por_tipo"] == []
    assert vacio["mente"]["disponible"] is False
    assert calcular_resultados(None)["ok"] is True


def test_roi_nulo_si_no_hay_stake_y_tipo_en_inteligencia():
    out = calcular_resultados(
        {
            "capital_inicial": 100,
            "dias": [
                {
                    "fecha": "2026-08-01",
                    "apuestas": [
                        {
                            "estado": "ganada",
                            "profit": 4,
                            "stake": 0,
                            "inteligencia": {"tipo_pick": "scratch"},
                            "lineas_fuente": "draftkings",
                        }
                    ],
                }
            ],
        }
    )
    assert out["resumen"]["profit"] == 4
    assert out["resumen"]["roi_pct"] is None
    assert out["resumen"]["capital"] == 104
    assert out["por_tipo"][0]["tipo"] == "scratch"
    assert out["por_tipo"][0]["roi_pct"] is None


def test_panel_enlaza_el_endpoint():
    html = Path(__file__).resolve().parent.joinpath("QuantumMLB.html").read_text(encoding="utf-8")
    assert 'id="resultados"' in html
    assert "/api/resultados" in html
    assert "Por tipo de pick" in html
    assert "Fuente de la cuota" in html
    assert "Veredicto de la mente" in html
    assert "Salida" in html


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(srv.app.router, "lifespan_context", _sin_motor)
    with TestClient(srv.app) as http:
        yield http


def test_endpoint_es_get_publico_y_no_escribe(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "tok-secreto")
    memoria = _memoria()
    srv.MEMORIA_PATH.write_text(json.dumps(memoria), encoding="utf-8")
    srv._invalidar_cache_memoria()
    antes = srv.MEMORIA_PATH.read_bytes()

    def _no_guardar(*_a, **_k):
        raise AssertionError("el endpoint no debe guardar memoria")

    monkeypatch.setattr(srv, "guardar_memoria", _no_guardar)
    response = client.get("/api/resultados")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["resumen"]["n"] == 5
    assert body["resumen"]["capital"] == 105.5
    assert "telegram" not in response.text
    assert "bot_token" not in response.text
    assert srv.MEMORIA_PATH.read_bytes() == antes

    post = client.post("/api/resultados")
    assert post.status_code == 405
    for route in srv.app.routes:
        if getattr(route, "path", "") == "/api/resultados":
            assert "GET" in (route.methods or set())
            assert "POST" not in (route.methods or set())
            dependant = route.dependant
            calls = [getattr(dep, "call", None) for dep in dependant.dependencies]
            assert srv.exigir_cron_secreto not in calls
