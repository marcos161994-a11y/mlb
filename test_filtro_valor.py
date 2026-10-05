"""Filtro de valor, sombra de la mente y calibración con holdout."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from filtro_valor import (
    aplicar_decision_tipo,
    backtest_filtro_tipo,
    backtest_historico,
    cuota_decimal_real,
    evaluar_valor,
    fuente_casa_real,
)

CFG = {
    "estrategia": {
        "min_prob_modelo": 58.0,
        "min_edge_pct": 6.0,
        "min_cuota_underdog": 2.0,
        "filtro_valor": {
            "activo": True,
            "margen_min_pct": 1.0,
            "margen_underdog_pct": 2.0,
            "margen_scratch_pct": 1.0,
            "penalizar_scratch": False,
        },
    },
    "usar_calibracion": True,
}


def test_config_trae_filtro_sombra_y_pesos_en_cero():
    cfg = json.loads(Path("config_experimento.json").read_text(encoding="utf-8"))
    fv = (cfg.get("estrategia") or {}).get("filtro_valor") or {}
    assert fv.get("activo") is True
    assert fv.get("sombra") is True
    assert float(fv.get("margen_min_pct")) == 1.0
    assert float(fv.get("margen_underdog_pct")) == 2.0
    assert float(fv.get("margen_underdog_pct")) > float(fv.get("margen_min_pct"))
    assert float(fv.get("margen_scratch_pct")) == float(fv.get("margen_min_pct"))
    assert fv.get("penalizar_scratch") is False
    ft = (cfg.get("estrategia") or {}).get("filtro_tipo") or {}
    assert ft.get("activo") is True
    assert ft.get("apostar_scratch") is True
    assert ft.get("cortar_underdog") is True
    mente = cfg.get("mente") or {}
    assert mente.get("shadow") is True
    assert str(mente.get("modo")) == "shadow"
    pesos = cfg.get("pesos_ensemble") or {}
    assert float(pesos.get("rf")) == 0.0
    assert float(pesos.get("xgb")) == 0.0
    assert float(pesos.get("estadistico")) > 0


def test_estimado_no_es_cuota_real():
    assert fuente_casa_real({"lineas_fuente": "draftkings", "fuente_momio": "estimado"}) is False
    assert cuota_decimal_real({"odds": 1.9, "lineas_fuente": "modelo"}) is None
    assert cuota_decimal_real({"odds": 1.91, "lineas_fuente": "draftkings"}) == 1.91
    # El campo explícito del otro agente manda sobre `odds`.
    assert (
        cuota_decimal_real(
            {
                "odds": 1.50,
                "cuota_real_decimal": 2.10,
                "lineas_fuente": "draftkings",
                "fuente_momio": "pinnacle",
            }
        )
        == 2.10
    )
    # Momio americano no se lee como decimal.
    assert (
        cuota_decimal_real(
            {
                "odds": 1.80,
                "precio_congelado": 150,
                "lineas_fuente": "fanduel",
            }
        )
        == 1.80
    )


def test_underdog_exige_mas_edge_y_scratch_no():
    favorito = {
        "probPick": 62.0,
        "odds": 1.70,
        "lineas_fuente": "draftkings",
        "tipo_pick": "limpio",
    }
    # implícita 58.8, edge 3.2 >= 1.0
    assert evaluar_valor(favorito, CFG)["veredicto"] == "apostaria"
    assert evaluar_valor(favorito, CFG)["apostar"] is True

    # Cuota 1.76 → implícita 56.8. Prob 58 deja 1.2 pts: pasa el margen 1.0 y no el 2.0 del underdog.
    mismo_precio = {
        "probPick": 58.0,
        "odds": 1.76,
        "lineas_fuente": "draftkings",
        "tipo_pick": "limpio",
    }
    assert evaluar_valor(mismo_precio, CFG)["apostar"] is True
    under = {**mismo_precio, "tipo_pick": "underdog"}
    ev_under = evaluar_valor(under, CFG)
    assert ev_under["apostar"] is False
    assert ev_under["veredicto"] == "pasaria"
    assert ev_under["tipo"] == "underdog"
    assert ev_under["margen"] == 2.0

    scratch = {
        "probPick": 58.0,
        "odds": 2.20,
        "lineas_fuente": "draftkings",
        "tipo_pick": "scratch",
        "scratch_lineup": {"riesgo": True},
    }
    ev_scratch = evaluar_valor(scratch, CFG)
    # implícita 45.5, edge 12.5, margen scratch 1.0 (no el de underdog)
    assert ev_scratch["apostar"] is True
    assert ev_scratch["margen"] == 1.0


def test_sin_filtro_sigue_el_min_edge_viejo():
    cfg = {"estrategia": {"min_edge_pct": 6.0, "min_prob_modelo": 58.0}}
    # implícita de 1.80 = 55.6, edge 3.4 < 6
    reg = {"probPick": 59.0, "odds": 1.80, "lineas_fuente": "draftkings", "tipo_pick": "limpio"}
    ev = evaluar_valor(reg, cfg)
    assert ev["filtro_activo"] is False
    assert ev["apostar"] is False
    assert ev["margen"] == 6.0


def test_apostable_por_valor_respeta_filtro(monkeypatch):
    import servidor_mlb as srv

    reg = {"probPick": 60.0, "tipo_pick": "limpio", "lineas_fuente": "modelo"}
    apostable, edge = srv._apostable_por_valor(reg, CFG, 1.75, "draftkings")
    # implícita 57.1, edge ~2.9 >= 1.0
    assert apostable is True
    assert edge >= 1.0
    assert reg["filtro_valor"]["tipo"] == "limpio"
    assert reg["filtro_valor"]["veredicto"] == "apostaria"


def test_sombra_no_aprueba_ni_bloquea():
    import servidor_mlb as srv

    cfg = json.loads(json.dumps(CFG))
    cfg["estrategia"]["filtro_valor"]["sombra"] = True
    # implícita 57.1, edge ~2.9: el valor apostaría, min_edge 6 no.
    reg = {"probPick": 60.0, "tipo_pick": "limpio", "lineas_fuente": "modelo"}
    apostable, _edge = srv._apostable_por_valor(reg, cfg, 1.75, "draftkings")
    assert apostable is False
    assert reg["filtro_valor"]["sombra"] is True
    assert reg["filtro_valor"]["apostar"] is True
    assert reg["filtro_valor"]["veredicto"] == "apostaria"

    # implícita 55.6, edge 8.4 >= min_edge 6, pero el margen de valor es 20.
    cfg["estrategia"]["filtro_valor"]["margen_min_pct"] = 20.0
    reg2 = {"probPick": 64.0, "tipo_pick": "limpio", "lineas_fuente": "modelo"}
    apostable2, _edge2 = srv._apostable_por_valor(reg2, cfg, 1.80, "draftkings")
    assert apostable2 is True
    assert reg2["filtro_valor"]["veredicto"] == "pasaria"
    assert reg2["filtro_valor"]["apostar"] is False


def test_tipo_apuesta_scratch_corta_underdog_y_deja_el_resto():
    cfg = {
        "estrategia": {
            "filtro_tipo": {
                "activo": True,
                "apostar_scratch": True,
                "cortar_underdog": True,
            }
        }
    }
    under = {
        "tipo_pick": "underdog",
        "apostable": True,
        "probPick": 60,
        "odds": 2.2,
        "lineas_fuente": "draftkings",
        "motivo_apuesta": "Valor +6%",
    }
    ev = aplicar_decision_tipo(under, cfg)
    assert under["apostable"] is False
    assert under["motivo_apuesta"] == "underdog cortado"
    assert ev["motivo"] == "underdog cortado"
    assert under["filtro_tipo"]["decision"] == "cortar"

    # 55% vs cuota 2.00 (implícita 50): edge +5, bajo el 58% del filtro normal.
    # El scratch igual entra porque el edge es positivo.
    scratch = {
        "tipo_pick": "scratch",
        "apostable": False,
        "probPick": 55.0,
        "odds": 2.00,
        "edge": 5.0,
        "lineas_fuente": "draftkings",
        "scratch_lineup": {"riesgo": True},
        "motivo_apuesta": "Sin valor (mínimo +6% edge)",
    }
    aplicar_decision_tipo(scratch, cfg)
    assert scratch["apostable"] is True
    assert scratch["filtro_tipo"]["decision"] == "apostar"
    assert "se apuesta" in (scratch["motivo_apuesta"] or "")

    sin_cuota = {
        "tipo_pick": "scratch",
        "apostable": False,
        "probPick": 60.0,
        "odds": 1.90,
        "lineas_fuente": "modelo",
        "fuente_momio": "estimado",
        "scratch_lineup": {"riesgo": True},
    }
    aplicar_decision_tipo(sin_cuota, cfg)
    assert sin_cuota["apostable"] is False
    assert sin_cuota["filtro_tipo"]["decision"] == "sin_cuota"

    lesionado = {
        **scratch,
        "apostable": False,
        "pick": "A ML",
        "visitante": "A",
        "home": "B",
        "lesiones": {"starter_riesgo": True, "starter_away_lesionado": True},
    }
    aplicar_decision_tipo(lesionado, cfg)
    assert lesionado["apostable"] is False
    assert lesionado["filtro_tipo"]["decision"] == "veto_lesion"

    limpio = {
        "tipo_pick": "limpio",
        "apostable": True,
        "odds": 1.70,
        "probPick": 62,
        "lineas_fuente": "draftkings",
        "motivo_apuesta": "Valor +8%",
    }
    aplicar_decision_tipo(limpio, cfg)
    assert limpio["apostable"] is True
    assert limpio["motivo_apuesta"] == "Valor +8%"
    assert limpio["filtro_tipo"]["decision"] == "igual"

    favorito = {
        "tipo_pick": "favorito_alto",
        "apostable": False,
        "odds": 1.40,
        "probPick": 70,
        "lineas_fuente": "draftkings",
        "motivo_apuesta": "Sin valor (mínimo +6% edge)",
    }
    aplicar_decision_tipo(favorito, cfg)
    assert favorito["apostable"] is False
    assert favorito["motivo_apuesta"] == "Sin valor (mínimo +6% edge)"
    assert favorito["filtro_tipo"]["decision"] == "igual"


def test_scratch_sin_edge_positivo_no_apuesta():
    """El scratch no pisa un precio sin valor. Limpio y favorito no cambian."""
    from lineas_betmgm import american_a_decimal

    cfg = {
        "estrategia": {
            "filtro_tipo": {
                "activo": True,
                "apostar_scratch": True,
                "cortar_underdog": True,
            }
        }
    }

    def _scratch(prob: float, odds: float, *, american: int | None = None) -> dict:
        reg = {
            "tipo_pick": "scratch",
            "apostable": True,
            "pick": "Home ML",
            "probPick": prob,
            "odds": odds,
            "lineas_fuente": "draftkings",
            "fuente_momio": "draftkings",
            "scratch_lineup": {"riesgo": True},
            "motivo_apuesta": "Scratch con cuota real: se apuesta",
        }
        if american is not None:
            reg["odds_american"] = american
        aplicar_decision_tipo(reg, cfg)
        return reg

    # Edge positivo bajo el mínimo de 58% / +6: el scratch sí apuesta.
    con_valor = _scratch(54.0, 2.10)
    assert con_valor["apostable"] is True
    assert con_valor["filtro_tipo"]["decision"] == "apostar"
    assert con_valor["filtro_tipo"]["edge"] > 0

    # Edge cero: no se apuesta.
    cero = _scratch(50.0, 2.0)
    assert cero["apostable"] is False
    assert cero["filtro_tipo"]["decision"] == "sin_valor"
    assert "sin valor a este precio" in (cero["motivo_apuesta"] or "")
    assert "no se apuesta" in (cero["motivo_apuesta"] or "")

    # Casos reales en los que el scratch apostó con edge negativo.
    # 30-sep PHI@ATL: 50.5% y edge ~0 (aquí cuota pareja 1.98, implícita 50.5).
    atl = _scratch(50.5, 1.98)
    assert atl["apostable"] is False
    assert atl["filtro_tipo"]["edge"] <= 0

    # 3-oct CWS@CLE: CLE 51.1% a −149.
    cle = _scratch(51.1, american_a_decimal(-149), american=-149)
    assert cle["apostable"] is False
    assert cle["filtro_tipo"]["edge"] < 0
    assert "sin valor a este precio" in (cle["motivo_apuesta"] or "")

    # 3-oct SD@MIL: MIL 54.4% a −218.
    mil_3 = _scratch(54.4, american_a_decimal(-218), american=-218)
    assert mil_3["apostable"] is False
    assert mil_3["filtro_tipo"]["edge"] < 0

    # 4-oct SD@MIL: MIL 53.6% a −136 (implícita ~57.6).
    mil_4 = _scratch(53.6, american_a_decimal(-136), american=-136)
    assert mil_4["apostable"] is False
    assert mil_4["filtro_tipo"]["edge"] < 0
    assert "implícita" in (mil_4["motivo_apuesta"] or "")

    # Un limpio con el mismo precio negativo no lo reescribe el filtro de tipo.
    limpio = {
        "tipo_pick": "limpio",
        "apostable": False,
        "probPick": 51.1,
        "odds": american_a_decimal(-149),
        "lineas_fuente": "draftkings",
        "motivo_apuesta": "Sin valor (mínimo +6% edge)",
    }
    aplicar_decision_tipo(limpio, cfg)
    assert limpio["apostable"] is False
    assert limpio["motivo_apuesta"] == "Sin valor (mínimo +6% edge)"
    assert limpio["filtro_tipo"]["decision"] == "igual"


@pytest.fixture
def _calibrador_aislado():
    import calibracion as cal

    prev = (
        cal._calibrador,
        dict(cal._calibradores_tipo),
        dict(cal._calibradores_segmento),
        dict(cal._meta),
    )
    cal._calibrador = None
    cal._calibradores_tipo = {}
    cal._calibradores_segmento = {}
    cal._meta = {}
    yield cal
    (
        cal._calibrador,
        cal._calibradores_tipo,
        cal._calibradores_segmento,
        cal._meta,
    ) = prev


def _memoria_sesgada(n: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    prob = rng.uniform(62.0, 84.0, n)
    # El modelo está inflado: la frecuencia real está más cerca de 55%.
    p_true = np.clip(0.50 + 0.20 * ((prob / 100.0) - 0.50), 0.35, 0.70)
    y = rng.binomial(1, p_true)
    preds = []
    for i in range(n):
        preds.append(
            {
                "game_id": str(i),
                "probPick": round(float(prob[i]), 1),
                "estado": "liquidado",
                "resultado": "acierto" if int(y[i]) == 1 else "fallo",
                "lineas_fuente": "draftkings",
                "odds": 1.70,
                "tipo_pick": "limpio",
            }
        )
    return {"dias": [{"fecha": "2026-08-01", "predicciones": preds, "apuestas": []}]}


def test_muestra_corta_elige_platt(_calibrador_aislado):
    cal = _calibrador_aislado
    meta = cal.entrenar_calibrador(_memoria_sesgada(45, 2), min_muestras=30)
    assert meta["ok"] is True
    assert meta["metodo"] == "platt"
    assert "Platt" in (meta.get("holdout") or {}).get("motivo", "")
    assert cal._path().exists()
    assert cal._path().with_name("calibracion_reporte.json").exists()


def test_holdout_baja_el_brier_cuando_hay_historia(_calibrador_aislado):
    cal = _calibrador_aislado
    meta = cal.entrenar_calibrador(_memoria_sesgada(220, 3), min_muestras=30)
    hold = meta["holdout"]
    assert meta["ok"] is True
    assert hold["brier_antes"] is not None
    assert hold["brier_despues"] <= hold["brier_antes"] + 0.005
    assert hold["bins_antes"] and hold["bins_despues"]
    assert meta["metodo"] in ("isotonic", "platt")
    cruda = 78.0
    ajustada = cal.calibrar_probabilidad(cruda, CFG)
    assert ajustada < cruda


def test_backtest_sintetico_recorta_apuestas_sin_edge(_calibrador_aislado):
    rng = np.random.default_rng(4)
    n = 160
    preds = []
    for i in range(n):
        fecha = f"2026-07-{(i % 28) + 1:02d}"
        # 72% declarado, cuota corta: el edge crudo existe, el real no.
        gana = bool(rng.random() < 0.52)
        preds.append(
            {
                "game_id": str(i),
                "probPick": 72.0,
                "odds": 1.55,
                "lineas_fuente": "draftkings",
                "tipo_pick": "favorito_alto",
                "estado": "liquidado",
                "resultado": "acierto" if gana else "fallo",
                "stake_virtual": 5.0,
                "_fecha": fecha,
            }
        )
    # backtest_historico lee la memoria, no las claves _fecha sueltas.
    por_fecha: dict[str, list] = {}
    for pred in preds:
        fila = dict(pred)
        fecha = fila.pop("_fecha")
        por_fecha.setdefault(fecha, []).append(fila)
    memoria = {
        "dias": [
            {"fecha": fecha, "predicciones": filas, "apuestas": []}
            for fecha, filas in sorted(por_fecha.items())
        ]
    }
    out = backtest_historico(memoria, CFG)
    assert out["ok"] is True
    assert out["etiqueta"] == "backtest"
    assert out["antes"]["n"] > out["despues"]["n"]


def test_backtest_filtro_tipo_quita_underdog(_calibrador_aislado):
    n = 160
    preds = []
    for i in range(n):
        fecha = f"2026-06-{(i % 28) + 1:02d}"
        tipo = "underdog" if i % 2 == 0 else ("scratch" if i % 4 == 1 else "limpio")
        gana = tipo != "underdog"
        preds.append(
            {
                "game_id": str(i),
                "probPick": 60.0,
                "odds": 2.10 if tipo == "underdog" else 1.80,
                "lineas_fuente": "draftkings",
                "tipo_pick": tipo,
                "estado": "liquidado",
                "resultado": "acierto" if gana else "fallo",
                "stake_virtual": 5.0,
                "_fecha": fecha,
            }
        )
    por_fecha: dict[str, list] = {}
    for pred in preds:
        fila = dict(pred)
        fecha = fila.pop("_fecha")
        por_fecha.setdefault(fecha, []).append(fila)
    memoria = {
        "dias": [
            {"fecha": fecha, "predicciones": filas, "apuestas": []}
            for fecha, filas in sorted(por_fecha.items())
        ]
    }
    cfg = json.loads(json.dumps(CFG))
    cfg["estrategia"]["filtro_valor"]["sombra"] = True
    cfg["estrategia"]["filtro_tipo"] = {
        "activo": True,
        "apostar_scratch": True,
        "cortar_underdog": True,
    }
    out = backtest_filtro_tipo(memoria, cfg)
    assert out["ok"] is True
    assert out["etiqueta"] == "backtest"
    assert out["holdout"]["despues"]["n"] < out["holdout"]["antes"]["n"]
    assert out["todos"]["despues"]["n"] < out["todos"]["antes"]["n"]
    assert out["por_tipo_todos"]["underdog"]["n"] > 0
    assert out["sombra_valor_holdout"]["n"] == out["n_holdout"]
    # El libro de después no incluye underdogs: solo scratch y limpio.
    assert out["todos"]["despues"]["n"] == (
        out["por_tipo_todos"]["scratch"]["n"] + out["por_tipo_todos"]["limpio"]["n"]
    )
