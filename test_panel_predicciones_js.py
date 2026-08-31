"""Regresión: pintarPredicciones no usa diasOrden antes de declararlo."""

from __future__ import annotations

from pathlib import Path


def test_pintar_predicciones_dias_orden_declarado_antes():
    html = Path(__file__).resolve().parent.joinpath("QuantumMLB.html").read_text(
        encoding="utf-8"
    )
    start = html.find("function pintarPredicciones")
    assert start != -1
    body = html[start : start + 9000]
    idx_use = body.find("diasOrden.map")
    idx_decl = body.find("const diasOrden")
    assert idx_decl != -1 and idx_use != -1
    assert idx_decl < idx_use, "diasOrden debe declararse antes del template HTML"
