"""Exportación a SQLite y la respuesta de ROI."""

from __future__ import annotations

import json

from consulta.exportar_a_sqlite import exportar, main as exportar_main
from consulta.preguntar_ia import main as preguntar_main
from consulta.preguntar_ia import responder, _abrir


def _memoria() -> dict:
    def pred(tipo, resultado, profit, stake, decision, prob):
        return {
            "game_id": f"{tipo}-{resultado}-{prob}",
            "tipo_pick": tipo,
            "probPick": prob,
            "odds": 1.8,
            "resultado": resultado,
            "estado": "liquidado",
            "profit": profit,
            "stake_virtual": stake,
            "ia_mente": {"decision": decision} if decision else None,
        }

    scratches = [pred("scratch", "acierto", 4.0, 5.0, "PASAR", 60)] * 40
    scratches += [pred("scratch", "fallo", -5.0, 5.0, "PASAR", 55)] * 10
    favoritos = [pred("favorito_alto", "acierto", 1.0, 5.0, "PASAR", 72)] * 20
    favoritos += [pred("favorito_alto", "fallo", -5.0, 5.0, "APOSTAR", 72)] * 20
    return {
        "capital": 90.0,
        "capital_inicial": 100.0,
        "dia_actual": 2,
        "dias_totales": 10,
        "stake_por_juego": 5.0,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-08-01",
                "predicciones": scratches + favoritos,
                "apuestas": [
                    {"estado": "ganada", "profit": 5.0, "stake": 5.0, "pick": "A"},
                    {"estado": "perdida", "profit": -5.0, "stake": 5.0, "pick": "B"},
                ],
            }
        ],
    }


def test_exportar_y_responder_marca_scratch(tmp_path, capsys):
    db = tmp_path / "memoria.sqlite"
    resumen = exportar(_memoria(), db)
    assert resumen["predicciones"] == 90
    assert resumen["apuestas"] == 2
    conn = _abrir(db)
    try:
        texto = responder(conn, "¿Qué tipo de pick me da mejor ROI y por qué?")
    finally:
        conn.close()
    assert texto.startswith("El tipo con mejor ROI es scratch:")
    assert "filtrando al revés" in texto
    assert "APOSTAR:" in texto
    assert "PASAR:" in texto
    assert "70%" in texto
    assert "Apuestas reales: 2" in texto


def test_cli_desde_la_carpeta(tmp_path, monkeypatch, capsys):
    origen = tmp_path / "memoria_auditoria.json"
    origen.write_text(json.dumps(_memoria()), encoding="utf-8")
    db = tmp_path / "memoria.sqlite"
    monkeypatch.chdir(tmp_path)
    assert exportar_main(["--json", str(origen), "--db", str(db)]) == 0
    salida = capsys.readouterr().out
    assert "90 predicciones" in salida
    assert preguntar_main(["¿Qué tipo de pick me da mejor ROI y por qué?", "--db", str(db)]) == 0
    respuesta = capsys.readouterr().out
    assert "scratch" in respuesta
    assert preguntar_main(["¿y la banca?", "--db", str(tmp_path / "no.sqlite")]) == 1