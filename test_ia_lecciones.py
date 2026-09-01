"""Tests: post-mortem / lecciones de fallos para el veto IA."""

from ia_lecciones import (
    _heuristica_leccion,
    backfill_lecciones_si_vacio,
    registrar_leccion_desde_fallo,
    resumen_lecciones,
    texto_lecciones_para_prompt,
)


def test_heuristica_sin_cuota_real():
    pred = {
        "resultado": "fallo",
        "lineas_fuente": "modelo",
        "motivo_apuesta": "Sin mercado · solo stats",
        "probPick": 62,
        "edge": 8,
    }
    h = _heuristica_leccion(pred)
    assert h["patron"] == "sin_cuota_real"
    assert "cuota" in h["leccion"].lower() or "mercado" in h["leccion"].lower()


def test_heuristica_scratch():
    pred = {
        "resultado": "fallo",
        "lineas_fuente": "draftkings",
        "scratch_lineup": {"riesgo": True, "alerta": "star out"},
        "probPick": 55,
        "edge": 7,
    }
    h = _heuristica_leccion(pred)
    assert h["patron"] == "scratch_lineup"


def test_registrar_idempotente_por_game_id():
    memoria = {"lecciones": []}
    pred = {
        "resultado": "fallo",
        "game_id": "g-100",
        "pick": "Dodgers ML",
        "visitante": "Giants",
        "home": "Dodgers",
        "lineas_fuente": "modelo",
        "probPick": 61,
        "edge": 5,
    }
    cfg = {"usar_ia_veto": False}  # solo heurística
    a = registrar_leccion_desde_fallo(memoria, pred, cfg, cuando="2026-08-10")
    b = registrar_leccion_desde_fallo(memoria, pred, cfg, cuando="2026-08-10")
    assert a is not None
    assert b is None
    assert len(memoria["lecciones"]) == 1
    assert memoria["lecciones"][0]["game_id"] == "g-100"


def test_ignora_acierto_y_tardios():
    memoria = {"lecciones": []}
    cfg = {"usar_ia_veto": False}
    assert (
        registrar_leccion_desde_fallo(
            memoria,
            {"resultado": "acierto", "game_id": "1"},
            cfg,
        )
        is None
    )
    assert (
        registrar_leccion_desde_fallo(
            memoria,
            {
                "resultado": "fallo",
                "game_id": "2",
                "invalida_tarde": True,
                "lineas_fuente": "modelo",
            },
            cfg,
        )
        is None
    )
    assert memoria["lecciones"] == []


def test_texto_prompt_incluye_lecciones():
    memoria = {
        "lecciones": [
            {
                "patron": "edge_falso",
                "leccion": "Exigir edge real",
                "pick": "A ML",
                "fecha": "2026-08-01",
            }
        ]
    }
    txt = texto_lecciones_para_prompt(memoria)
    assert "edge_falso" in txt
    assert "Exigir edge real" in txt


def test_backfill_si_vacio_y_flag():
    memoria = {
        "dias": [
            {
                "fecha": "2026-08-01",
                "predicciones": [
                    {
                        "resultado": "fallo",
                        "estado": "liquidado",
                        "game_id": "bf-1",
                        "pick": "Yankees ML",
                        "lineas_fuente": "modelo",
                        "probPick": 64,
                        "edge": 4,
                        "valida_stats": True,
                    },
                    {
                        "resultado": "acierto",
                        "estado": "liquidado",
                        "game_id": "bf-2",
                        "pick": "Mets ML",
                    },
                ],
            }
        ]
    }
    n = backfill_lecciones_si_vacio(memoria)
    assert n >= 1
    assert memoria["lecciones_backfill_hecho"] is True
    assert len(memoria["lecciones"]) >= 1
    # Segunda pasada: no duplica
    assert backfill_lecciones_si_vacio(memoria) == 0
    meta = resumen_lecciones(memoria)
    assert meta["total"] >= 1
    assert "sin_cuota_real" in meta["por_patron"] or "mala_practica_sin_mercado" in meta["por_patron"]
