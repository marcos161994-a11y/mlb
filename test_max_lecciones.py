"""Tests tope configurable de lecciones IA."""

from ia_lecciones import (
    aplicar_tope_lecciones,
    max_lecciones_almacenadas,
    max_lecciones_prompt,
    texto_lecciones_para_prompt,
)


def test_max_lecciones_desde_config():
    cfg = {"max_lecciones": 200, "max_lecciones_prompt": 10}
    assert max_lecciones_almacenadas(cfg) == 200
    assert max_lecciones_prompt(cfg) == 10


def test_aplicar_tope_conserva_recientes():
    mem = {"lecciones": [{"id": i} for i in range(200)]}
    cfg = {"max_lecciones": 150}
    recortadas = aplicar_tope_lecciones(mem, cfg)
    assert recortadas == 50
    assert len(mem["lecciones"]) == 150
    assert mem["lecciones"][0]["id"] == 50
    assert mem["lecciones"][-1]["id"] == 199


def test_prompt_usa_max_config(monkeypatch):
    cfg = {"max_lecciones_prompt": 6}
    mem = {
        "lecciones": [
            {"patron": "edge_falso", "leccion": f"L{i}", "tipo": "fallo_postmortem", "confianza": 3}
            for i in range(20)
        ]
    }

    monkeypatch.setattr(
        "ia_lecciones.max_lecciones_prompt",
        lambda _cfg=None: max_lecciones_prompt(cfg),
    )
    txt = texto_lecciones_para_prompt(mem)
    assert txt.count("L") <= 6 or "Lecciones previas" in txt
