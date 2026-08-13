"""Tests de la mente (conclusión estructurada, sin Groq)."""

from mente_mlb import (
    aplicar_stake_mente,
    construir_briefing,
    mente_conclusion,
)


CFG = {
    "usar_mente": True,
    "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    "stake_por_juego": 5,
    "estrategia": {"min_stake_pct": 1, "max_stake_pct": 5},
}


def test_pasar_sin_mercado():
    juego = {
        "id": "t1",
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 64,
        "edge": 8,
        "odds": 1.7,
        "lineas_fuente": "modelo",
    }
    c = mente_conclusion(juego, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert c["autoriza_dinero"] is False
    assert any("mercado" in r.lower() or "cuota" in r.lower() for r in c["razones"])


def test_pasar_scratch_del_pick():
    juego = {
        "id": "t2",
        "visitante": "Yankees",
        "home": "Red Sox",
        "pick": "Yankees ML",
        "probPick": 60,
        "edge": 7,
        "odds": 1.85,
        "lineas_fuente": "oddspapi",
        "scratch_lineup": {
            "ok": True,
            "riesgo": True,
            "scratch_away": True,
            "alerta": "SP scratched",
        },
    }
    c = mente_conclusion(juego, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert c["autoriza_dinero"] is False


def test_apostar_heuristico_con_edge():
    juego = {
        "id": "t3",
        "visitante": "Away",
        "home": "Home",
        "pick": "Home ML",
        "probPick": 61,
        "edge": 9.0,
        "odds": 1.95,
        "lineas_fuente": "oddspapi",
        "pitcherAway": "X",
        "pitcherHome": "Y",
    }
    c = mente_conclusion(juego, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "APOSTAR"
    assert c["autoriza_dinero"] is True
    assert c["confianza"] >= 3
    assert c["stake_pct"] > 0
    stake = aplicar_stake_mente(100.0, c, CFG)
    assert 1.0 <= stake <= 5.0


def test_conf_baja_no_autoriza_en_estricto():
    cfg = {
        "usar_mente": True,
        "mente": {"modo": "estricto", "min_confianza": 5, "requiere_mercado": True},
    }
    juego = {
        "id": "t4",
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 56,
        "edge": 6.2,
        "odds": 2.05,
        "lineas_fuente": "oddspapi",
    }
    c = mente_conclusion(juego, cfg, {}, forzar=True, solo_local=True)
    # Heurística da conf 3 → no alcanza 5
    if c["decision"] == "APOSTAR":
        assert c["autoriza_dinero"] is False
        assert c.get("dinero_bloqueado_por") == "confianza"


def test_briefing_junta_pilares():
    b = construir_briefing(
        {
            "pick": "X ML",
            "probPick": 58,
            "edge": 5,
            "lineas_fuente": "modelo",
            "clima": {"ok": True, "run_env": 0.2, "motivo": "calor"},
            "factores_humanos": {"ok": True, "riesgo": True, "resumen": "B2B visita"},
        }
    )
    assert b["ok"]
    assert "sin_mercado" in b["alertas"]
    assert "humanos" in b["alertas"]


def test_briefing_congelado_se_reusa():
    from mente_mlb import generar_briefing_juego, mente_conclusion

    juego = {
        "id": "br1",
        "visitante": "A",
        "home": "B",
        "pick": "B ML",
        "probPick": 61,
        "edge": 9,
        "odds": 1.9,
        "lineas_fuente": "oddspapi",
        "pitcherAway": "X",
        "pitcherHome": "Y",
    }
    cfg = {
        "usar_mente": True,
        "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    }
    br = generar_briefing_juego(juego, {}, fase="t60")
    assert br["fase"] == "t60"
    assert "lecciones_txt" not in br  # compacto, sin texto largo
    assert juego["ia_briefing"]["resumen"]
    c = mente_conclusion(juego, cfg, {}, forzar=True, solo_local=True)
    assert c.get("briefing_fase") == "t60"
    # Conclusion no expone el resumen del briefing al cliente
    assert c.get("briefing") in (None, "")

