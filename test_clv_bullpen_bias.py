"""Tests CLV, bias solo dinero, pl_split divergencia."""

from __future__ import annotations

import clv_mlb as clv
import inteligencia_mlb as intel


def test_clv_pct_positivo_con_mejor_cuota():
    # Entrada 2.10 vs cierre pinnacle 2.00/1.85 → fair home ~1.93, CLV > 0
    v = clv.clv_pct(2.10, 2.00, 1.85, "Home Team ML", "Away", "Home Team")
    assert v is not None
    assert v > 0


def test_cuotas_pinnacle_desde_libros():
    j = {
        "lineas_libros": [
            {"casa": "draftkings", "away": 2.05, "home": 1.75},
            {"casa": "pinnacle", "away": 2.08, "home": 1.78},
        ]
    }
    a, h = clv.cuotas_pinnacle(j)
    assert a == 2.08
    assert h == 1.78


def test_actualizar_clv_registro_entrada_y_cierre():
    reg = {
        "pick": "Yankees ML",
        "visitante": "Red Sox",
        "home": "Yankees",
        "odds": 1.95,
    }
    juego = {
        "pick": "Yankees ML",
        "visitante": "Red Sox",
        "home": "Yankees",
        "lineas_libros": [{"casa": "pinnacle", "away": 2.10, "home": 1.95}],
    }
    assert clv.actualizar_clv_registro(reg, juego, fase="entrada") is True
    assert reg.get("clv_odds_entrada") == 1.95
    juego["lineas_libros"] = [{"casa": "pinnacle", "away": 2.15, "home": 1.88}]
    assert clv.actualizar_clv_registro(reg, juego, fase="cierre") is True
    assert reg.get("clv_pct") is not None


def test_resumen_clv_memoria():
    mem = {
        "dias": [
            {
                "predicciones": [
                    {"clv_pct": 2.5, "clv_entrada_pct": 1.0},
                    {"clv_pct": -1.0},
                ],
                "apuestas": [{"clv_pct": 3.0, "estado": "ganada"}],
            }
        ]
    }
    r = clv.resumen_clv_memoria(mem)
    assert r["muestras_cierre"] == 3
    assert r["clv_promedio"] is not None


def test_calcular_bias_solo_dinero_no_papel():
    from servidor_mlb import calcular_bias_aprendizaje

    mem_papel = {
        "dias": [
            {
                "predicciones": [
                    {"estado": "liquidado", "resultado": "acierto", "valida_stats": True}
                    for _ in range(20)
                ],
                "apuestas": [],
            }
        ]
    }
    assert calcular_bias_aprendizaje(mem_papel) == 0.0

    mem_din = {
        "dias": [
            {
                "predicciones": [],
                "apuestas": [
                    {"estado": "ganada"},
                    {"estado": "ganada"},
                    {"estado": "ganada"},
                    {"estado": "ganada"},
                    {"estado": "ganada"},
                    {"estado": "perdida"},
                ],
            }
        ]
    }
    assert calcular_bias_aprendizaje(mem_din) == 0.5


def test_pl_split_excluye_preds_con_dinero():
    from servidor_mlb import resumen_predicciones_y_dinero

    mem = {
        "stake_por_juego": 5.0,
        "dias": [
            {
                "predicciones": [
                    {
                        "game_id": "1",
                        "estado": "liquidado",
                        "resultado": "acierto",
                        "valida_stats": True,
                        "profit": 4.0,
                        "stake_virtual": 5.0,
                        "con_dinero": True,
                    },
                    {
                        "game_id": "2",
                        "estado": "liquidado",
                        "resultado": "fallo",
                        "valida_stats": True,
                        "profit": -5.0,
                        "stake_virtual": 5.0,
                    },
                ],
                "apuestas": [
                    {"game_id": "1", "estado": "ganada", "profit": 2.0, "stake": 3.0},
                ],
            }
        ],
    }
    s = resumen_predicciones_y_dinero(mem)
    assert s["predicciones"]["total"] == 1
    assert s["dinero"]["total"] == 1
    assert "roi_pct" in s["predicciones"]
    assert "divergencia" in s


def test_bullpen_index_en_analisis(monkeypatch):
    box = {
        "teams": {
            "home": {
                "team": {"id": 147},
                "players": {
                    "ID1": {
                        "person": {"fullName": "Reliever A"},
                        "stats": {
                            "pitching": {
                                "gamesStarted": 0,
                                "numberOfPitches": 45,
                                "inningsPitched": "1.0",
                                "era": "5.20",
                            }
                        },
                    }
                },
            }
        }
    }

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def fake_get(url, *args, **kwargs):
        if "schedule" in url:
            return FakeResp(
                {
                    "dates": [
                        {
                            "games": [
                                {
                                    "gamePk": 999,
                                    "gameDate": "2026-08-30T23:00:00Z",
                                    "status": {"abstractGameState": "Final"},
                                }
                            ]
                        }
                    ]
                }
            )
        return FakeResp(box)

    monkeypatch.setattr(intel._session, "get", fake_get)
    intel._bullpen_cache.clear()
    out = intel.analizar_bullpen_dia(147, season=2026, nombre="Yankees")
    assert out["ok"] is True
    assert out["bullpen_index"] >= 5
    assert out["pitches_l3d"] == 45
