"""Tests: importar pasado (4) + experiencias negativas (5)."""

from ia_importar import importar_dump_aprendizaje, importar_experiencias_lista
from ia_lecciones import (
    TIPO_OPORTUNIDAD,
    TIPO_SIN_CUOTA,
    TIPO_VETO_OK,
    escanear_experiencias_negativas,
    registrar_experiencia_negativa,
    registrar_experiencias_tras_liquidar,
)


def test_oportunidad_perdida_pasar_acierto():
    memoria = {"lecciones": []}
    pred = {
        "estado": "liquidado",
        "resultado": "acierto",
        "game_id": "g-op-1",
        "pick": "Yankees ML",
        "lineas_fuente": "oddspapi",
        "ia_veto": {"decision": "PASAR", "motivo": "scratch"},
        "valida_stats": True,
    }
    creadas = registrar_experiencia_negativa(memoria, pred, cuando="2026-08-10")
    assert len(creadas) == 1
    assert creadas[0]["tipo"] == TIPO_OPORTUNIDAD
    assert creadas[0]["patron"] == "oportunidad_perdida"
    # idempotente
    assert registrar_experiencia_negativa(memoria, pred) == []


def test_veto_acertado_pasar_fallo():
    memoria = {"lecciones": []}
    pred = {
        "estado": "liquidado",
        "resultado": "fallo",
        "game_id": "g-va-1",
        "pick": "Mets ML",
        "lineas_fuente": "oddspapi",
        "motivo_apuesta": "Modelo 60% · IA PASAR: scratch",
        "valida_stats": True,
    }
    # Tras liquidar: fallo postmortem + veto_acertado (+ quizás sin cuota si fuente modelo)
    r = registrar_experiencias_tras_liquidar(
        memoria, pred, cfg={"usar_ia_veto": False}, cuando="2026-08-10"
    )
    tipos = {x["tipo"] for x in ([r["fallo"]] if r["fallo"] else []) + r["negativas"]}
    assert TIPO_VETO_OK in tipos


def test_mala_practica_sin_mercado():
    memoria = {"lecciones": []}
    pred = {
        "estado": "liquidado",
        "resultado": "fallo",
        "game_id": "g-sc-1",
        "pick": "Dodgers ML",
        "lineas_fuente": "modelo",
        "motivo_apuesta": "Sin mercado · solo stats",
        "valida_stats": True,
    }
    creadas = registrar_experiencia_negativa(memoria, pred)
    assert any(c["tipo"] == TIPO_SIN_CUOTA for c in creadas)


def test_import_dump_marca_retroactivo_no_wr():
    destino = {"capital": 100, "dias": [], "lecciones": []}
    dump = {
        "capital": 100,
        "dias": [
            {
                "fecha": "2026-07-20",
                "predicciones": [
                    {
                        "game_id": "old-1",
                        "pick": "Cubs ML",
                        "estado": "liquidado",
                        "resultado": "fallo",
                        "lineas_fuente": "modelo",
                        "probPick": 60,
                    }
                ],
            }
        ],
    }
    stats = importar_dump_aprendizaje(destino, dump)
    assert stats["ok"] and stats["preds_nuevas"] == 1
    pred = destino["dias"][0]["predicciones"][0]
    assert pred["retroactivo"] is True
    assert pred["aprendizaje_solo"] is True
    assert pred["valida_stats"] is False


def test_import_lista_y_escaneo():
    memoria = {"capital": 100, "dias": [], "lecciones": []}
    r = importar_experiencias_lista(
        memoria,
        [
            {
                "fecha": "2026-07-15",
                "pick": "Sox ML",
                "visitante": "Sox",
                "home": "Yanks",
                "resultado": "acierto",
                "ia_veto": {"decision": "PASAR"},
                "lineas_fuente": "oddspapi",
            }
        ],
    )
    assert r["importadas"] == 1
    n = escanear_experiencias_negativas(memoria)
    assert n >= 1
    assert any(x.get("patron") == "oportunidad_perdida" for x in memoria["lecciones"])
