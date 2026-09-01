"""Tests: aprendizaje V2 de la mente (contadores + ajuste de confianza)."""

from mente_aprendizaje import (
    aplicar_aprendizaje_a_conclusion,
    actualizar_stats_tras_liquidar,
    asegurar_mente_stats,
    recomputar_stats_desde_historial,
)


def _pred(gid, decision, resultado, **extra):
    p = {
        "game_id": gid,
        "estado": "liquidado",
        "resultado": resultado,
        "pick": "Home ML",
        "edge": 7,
        "probPick": 60,
        "lineas_fuente": "draftkings",
        "ia_mente": {
            "decision": decision,
            "autoriza_dinero": decision == "APOSTAR",
            "alertas": extra.pop("alertas", ["humanos"]),
        },
    }
    p.update(extra)
    return p


def test_pasar_miss_y_ok_actualizan_stats():
    mem = {}
    actualizar_stats_tras_liquidar(mem, _pred("a1", "PASAR", "acierto"))
    actualizar_stats_tras_liquidar(mem, _pred("a2", "PASAR", "fallo"))
    actualizar_stats_tras_liquidar(mem, _pred("a3", "PASAR", "acierto"))
    stats = asegurar_mente_stats(mem)
    pa = stats["decisiones"]["PASAR"]
    assert pa["oportunidad_perdida"] == 2
    assert pa["evito_fallo"] == 1
    assert stats["patrones"]["humanos"]["pasar_miss"] == 2
    assert stats["patrones"]["humanos"]["pasar_ok"] == 1


def test_apostar_fail_penaliza_y_baja_confianza():
    mem = {}
    # 4 fallos de APOSTAR con señal humanos → penalización negativa
    for i in range(4):
        actualizar_stats_tras_liquidar(
            mem, _pred(f"b{i}", "APOSTAR", "fallo", alertas=["humanos"])
        )
    stats = asegurar_mente_stats(mem)
    assert stats["penalizacion"].get("humanos", 0) <= -1

    conc = {
        "ok": True,
        "decision": "APOSTAR",
        "confianza": 4,
        "stake_pct": 3,
        "razones": ["edge ok"],
        "fuente": "heuristica",
    }
    out = aplicar_aprendizaje_a_conclusion(conc, mem, ["humanos"])
    assert out["confianza"] < 4
    assert out["penalizacion_aprendizaje"] < 0


def test_pasar_soft_se_suaviza_a_esperar_si_mucho_miss():
    mem = {}
    for i in range(5):
        # 4 miss + 1 ok → miss rate 0.8
        actualizar_stats_tras_liquidar(
            mem,
            _pred(f"c{i}", "PASAR", "acierto" if i < 4 else "fallo", alertas=["edge_bajo"]),
        )
    conc = {
        "ok": True,
        "decision": "PASAR",
        "confianza": 3,
        "stake_pct": 0,
        "razones": ["edge bajo"],
        "fuente": "heuristica",
    }
    out = aplicar_aprendizaje_a_conclusion(conc, mem, ["edge_bajo"])
    assert out["decision"] == "ESPERAR"


def test_recomputar_idempotente():
    mem = {
        "dias": [
            {
                "predicciones": [
                    _pred("r1", "PASAR", "fallo"),
                    _pred("r2", "APOSTAR", "acierto", alertas=["limpio"]),
                ]
            }
        ]
    }
    n1 = recomputar_stats_desde_historial(mem)
    n2 = recomputar_stats_desde_historial(mem)
    assert n1 == 2
    assert n2 == 2
    assert mem["mente_stats"]["decisiones"]["PASAR"]["evito_fallo"] == 1
    assert mem["mente_stats"]["decisiones"]["APOSTAR"]["aciertos"] == 1
