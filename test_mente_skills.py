"""Biblioteca de habilidades: humedad observada, reflexión y reporte."""

from pathlib import Path

from mente_mlb import mente_conclusion
from mente_skills import aplicar_skills, reflexionar_fallo, reporte_auto_evolucion
from skills.humedad_pelota import auditar_humedad, clasificar_fallo


CFG = {
    "usar_mente": True,
    "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    "estrategia": {"favorito_inflado": {"activo": False}},
}


def test_humedad_alta_no_bloquea_la_apuesta():
    juego = {"clima": {"ok": True, "humedad": 78}, "edge": 6}
    assert aplicar_skills(juego, {}) == (False, "")


def test_fallo_con_humedad_registra_la_limitacion():
    pred = {"resultado": "fallo", "game_id": "1", "clima": {"ok": True, "humedad": 80}}
    assert "física climática" in (clasificar_fallo(pred) or "")
    mem: dict = {}
    reflexionar_fallo(mem, pred)
    assert mem["skills_limitaciones"][0]["texto"].startswith("Limitación detectada")
    seco = {"resultado": "fallo", "clima": {"ok": True, "humedad": 40}}
    assert clasificar_fallo(seco) is None


def test_auditoria_activa_el_skill_si_el_grupo_viene_en_rojo():
    preds = []
    for i in range(16):
        preds.append({"resultado": "fallo", "valida_stats": True, "profit": -3, "clima": {"humedad": 80}})
    for i in range(8):
        preds.append({"resultado": "acierto", "valida_stats": True, "profit": 2, "clima": {"humedad": 80}})
    for i in range(12):
        preds.append({"resultado": "acierto", "valida_stats": True, "profit": 2, "clima": {"humedad": 55}})
    audit = auditar_humedad(preds)
    assert audit["grupos"]["alta"]["n"] == 24
    assert audit["grupos"]["alta"]["wr"] < 45
    assert audit["activar"] is True


def test_historial_real_tiene_el_grupo_humedo_en_rojo():
    """Muestra documentada (10 de 26 en humedad alta), sin abrir el JSON vivo."""
    preds = []
    for _ in range(10):
        preds.append(
            {
                "resultado": "acierto",
                "valida_stats": True,
                "profit": 1.8,
                "clima": {"ok": True, "humedad": 78},
            }
        )
    for _ in range(16):
        preds.append(
            {
                "resultado": "fallo",
                "valida_stats": True,
                "profit": -3.0,
                "clima": {"ok": True, "humedad": 80},
            }
        )
    audit = auditar_humedad(preds)
    alta = audit["grupos"]["alta"]
    assert alta["n"] >= 20
    assert alta["wr"] < 45
    assert alta["profit"] < 0
    assert audit["activar"] is True


def test_mente_no_pasa_solo_por_humedad_alta():
    juego = {
        "id": "hum",
        "visitante": "Away",
        "home": "Home",
        "pick": "Home ML",
        "probPick": 60,
        "edge": 7,
        "odds": 1.9,
        "lineas_fuente": "draftkings",
        "clima": {"ok": True, "humedad": 76, "run_env": 0.2},
    }
    c = mente_conclusion(juego, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] != "PASAR"
    assert not any("humedad" in r.lower() for r in c["razones"])


def test_reporte_tiene_las_tres_lineas():
    rep = reporte_auto_evolucion()
    assert rep["habilidad"] == "Humedad alta"
    assert rep["activa"] is False
    assert "física climática" in rep["motivo"]
    assert "ya no bloquea" in rep["prueba"]
    html = Path("diagrama/resumen.html").read_text(encoding="utf-8")
    assert "Reporte de Auto-Evolución" in html
    src = Path("servidor_mlb.py").read_text(encoding="utf-8")
    assert "reflexionar_fallo" in src
    assert "/api/mente-skills" in src
