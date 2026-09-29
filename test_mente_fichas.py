"""Fichas: una situación solo bloquea dinero si viene peor que el resto."""

from mente_fichas import auditar_fichas, debe_pasar_por_ficha
from mente_mlb import mente_conclusion


def _pred(resultado: str, dias: float, *, profit: float, clv: float, viento: float = 4) -> dict:
    return {
        "resultado": resultado,
        "valida_stats": True,
        "profit": profit,
        "clv_pct": clv,
        "visitante": "Away",
        "home": "Home",
        "pick": "Home ML",
        "edge": 6,
        "factores_humanos": {
            "home": {
                "dias_descanso": dias,
                "back_to_back": dias <= 0,
                "cambio_zona": 3 if dias <= 0 else 0,
                "fatiga_viaje": 0.1,
            },
            "away": {"dias_descanso": 3, "fatiga_viaje": 0.1},
            "serie": {"getaway": False},
        },
        "ml_features": {"fatiga_bullpen": 0.2},
        "clima": {"viento_mph": viento, "humedad": 40},
    }


def _memoria_descanso_en_rojo() -> dict:
    preds = []
    for _ in range(6):
        preds.append(_pred("acierto", 0, profit=2, clv=-1))
    for _ in range(14):
        preds.append(_pred("fallo", 0, profit=-3, clv=-2))
    for _ in range(16):
        preds.append(_pred("acierto", 2, profit=2, clv=1))
    for _ in range(6):
        preds.append(_pred("fallo", 2, profit=-3, clv=1))
    return {"dias": [{"predicciones": preds}]}


def test_descanso_corto_se_activa_si_viene_peor():
    audit = auditar_fichas(_memoria_descanso_en_rojo())
    ficha = next(f for f in audit["fichas"] if f["id"] == "descanso_corto")
    assert ficha["n"] == 20
    assert ficha["wr"] == 30.0
    assert ficha["profit"] < 0
    assert ficha["clv_en_contra"] == 20
    assert ficha["activa"] is True


def test_muestra_chica_o_grupo_sano_no_se_activa():
    pocos = {"dias": [{"predicciones": [_pred("fallo", 0, profit=-3, clv=-1) for _ in range(19)]}]}
    assert auditar_fichas(pocos)["activas"] == []
    sanos = []
    for _ in range(14):
        sanos.append(_pred("acierto", 0, profit=3, clv=1))
    for _ in range(8):
        sanos.append(_pred("fallo", 0, profit=-3, clv=1))
    for _ in range(20):
        sanos.append(_pred("acierto", 2, profit=2, clv=1))
    audit = auditar_fichas({"dias": [{"predicciones": sanos}]})
    assert next(f for f in audit["fichas"] if f["id"] == "descanso_corto")["activa"] is False


def test_margen_grande_no_pasa_aunque_la_ficha_este_en_rojo():
    mem = _memoria_descanso_en_rojo()
    juego = {
        "visitante": "Away",
        "home": "Home",
        "pick": "Home ML",
        "edge": 14,
        "factores_humanos": {"home": {"dias_descanso": 0, "back_to_back": True, "cambio_zona": 3}},
    }
    assert debe_pasar_por_ficha(juego, mem)[0] is False
    juego["edge"] = 6
    ok, msg = debe_pasar_por_ficha(juego, mem)
    assert ok is True
    assert "Descanso corto" in msg


def test_mente_pasa_cuando_la_ficha_esta_en_rojo():
    juego = {
        "id": "b2b",
        "visitante": "Away",
        "home": "Home",
        "pick": "Home ML",
        "probPick": 60,
        "edge": 7,
        "odds": 1.9,
        "lineas_fuente": "draftkings",
        "clima": {"ok": True, "humedad": 40, "viento_mph": 3},
        "factores_humanos": {
            "home": {"dias_descanso": 0, "back_to_back": True, "cambio_zona": 3, "fatiga_viaje": 0.1},
        },
    }
    cfg = {
        "usar_mente": True,
        "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
        "estrategia": {"favorito_inflado": {"activo": False}},
    }
    c = mente_conclusion(juego, cfg, _memoria_descanso_en_rojo(), forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert c["lecciones_usadas"] == ["ficha"]
