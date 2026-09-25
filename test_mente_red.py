"""Red neuronal de la mente: nodos, aristas y payload del panel."""

from __future__ import annotations

from pathlib import Path

from mente_red import construir_mente_red


def _mem():
    return {
        "ml_meta": {"muestras": 400, "mensaje": "ok"},
        "lecciones": [{"id": "a"}, {"id": "b"}],
        "dias": [
            {
                "fecha": "2026-09-20",
                "predicciones": [
                    {"resultado": "acierto", "probPick": 72},
                    {"resultado": "fallo", "probPick": 52},
                    {"resultado": "acierto", "probPick": 66},
                ],
            }
        ],
    }


def test_red_tiene_capas_y_loop():
    red = construir_mente_red(
        {"usar_mente": True, "usar_ia_veto": True, "inteligencia": {"mc_sims": 80}},
        _mem(),
        lecciones={"total": 12},
        mente_stats={"decisiones": {"APOSTAR": {"aciertos": 4, "fallos": 1}, "PASAR": {"evito_fallo": 2}}},
    )
    assert red["ok"] is True
    ids = {n["id"] for n in red["nodos"]}
    for nid in (
        "mlb", "espn", "clima", "lesion", "scratch", "humanos", "l10",
        "stats", "ml", "elo", "calib",
        "consenso", "bullpen", "park", "tipo", "mc", "totales",
        "brief", "reglas", "groq", "aprende", "ops",
        "papel", "alta", "dinero", "liq", "lecs",
    ):
        assert nid in ids, nid
    edges = {(e["from"], e["to"]) for e in red["aristas"]}
    assert ("liq", "lecs") in edges
    assert ("lecs", "aprende") in edges
    assert ("aprende", "papel") in edges
    assert red["wr_todos"]["n"] == 3
    assert red["wr_alta"]["aciertos"] == 2
    assert red["lecciones"] == 12


def test_nodo_apagado_si_flag_off():
    red = construir_mente_red({"usar_elo": False, "usar_clima": False}, _mem())
    by = {n["id"]: n for n in red["nodos"]}
    assert by["elo"]["on"] is False
    assert by["clima"]["on"] is False


def test_panel_ya_no_tiene_la_red():
    html = Path("QuantumMLB.html").read_text(encoding="utf-8")
    assert 'id="mente-red-svg"' not in html
    assert "function pintarMenteRed" not in html
    assert "mente-red-box" not in html


def test_carpeta_local_tiene_la_red():
    html = Path("mente/index.html").read_text(encoding="utf-8")
    assert 'id="mente-red-svg"' in html
    assert "/api/mente-red" in html
    assert "function pintar" in html
    assert "Mente.url" in html
    atajo = Path("mente/Mente.url").read_text(encoding="utf-8")
    assert "mlb-1-en7i.onrender.com/mente" in atajo
    assert Path("mente/Abrir-mente.bat").exists()


def test_servidor_expone_ruta_mente():
    src = Path("servidor_mlb.py").read_text(encoding="utf-8")
    assert '@app.get("/mente")' in src
    assert "def panel_mente" in src
    assert "def mente_acceso_directo" in src
