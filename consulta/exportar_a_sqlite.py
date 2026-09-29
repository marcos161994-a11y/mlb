"""Pasa memoria_auditoria.json a SQLite para consultarlo en la PC.

Desde esta carpeta:

    py -3 exportar_a_sqlite.py --json C:\\ruta\\a\\memoria_auditoria.json

Escribe memoria.sqlite al lado. No toca el JSON ni el servidor.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _stake_prediccion(pred: dict) -> float:
    bruto = pred.get("stake_virtual")
    if bruto is None:
        bruto = pred.get("stake")
    if bruto is None:
        return 5.0
    try:
        return float(bruto)
    except (TypeError, ValueError):
        return 5.0


def _flotante(valor, default: float | None = 0.0) -> float | None:
    if valor is None or valor == "":
        return default
    try:
        return float(valor)
    except (TypeError, ValueError):
        return default


def _decision_mente(pred: dict) -> str | None:
    mente = pred.get("ia_mente")
    if isinstance(mente, dict) and mente.get("decision"):
        return str(mente.get("decision"))
    return None


def exportar(memoria: dict, db_path: Path) -> dict:
    """Reescribe la base. Cada corrida parte del JSON, no acumula."""
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE experimento (
                capital REAL,
                capital_inicial REAL,
                dia_actual INTEGER,
                dias_totales INTEGER,
                stake_por_juego REAL
            );
            CREATE TABLE predicciones (
                fecha TEXT,
                dia INTEGER,
                game_id TEXT,
                visitante TEXT,
                home TEXT,
                pick TEXT,
                tipo_pick TEXT,
                prob REAL,
                odds REAL,
                resultado TEXT,
                estado TEXT,
                profit REAL,
                stake REAL,
                con_dinero INTEGER,
                decision_mente TEXT,
                valida_stats INTEGER
            );
            CREATE TABLE apuestas (
                fecha TEXT,
                dia INTEGER,
                game_id TEXT,
                pick TEXT,
                estado TEXT,
                resultado TEXT,
                profit REAL,
                stake REAL,
                odds REAL,
                prob REAL
            );
            CREATE INDEX idx_pred_tipo ON predicciones(tipo_pick);
            CREATE INDEX idx_pred_mente ON predicciones(decision_mente);
            CREATE INDEX idx_pred_estado ON predicciones(estado);
            """
        )
        conn.execute(
            """
            INSERT INTO experimento
                (capital, capital_inicial, dia_actual, dias_totales, stake_por_juego)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                _flotante(memoria.get("capital")),
                _flotante(memoria.get("capital_inicial")),
                int(memoria.get("dia_actual") or 0),
                int(memoria.get("dias_totales") or 0),
                _flotante(memoria.get("stake_por_juego"), 5.0),
            ),
        )
        preds: list[tuple] = []
        apuestas: list[tuple] = []
        for dia in memoria.get("dias") or []:
            if not isinstance(dia, dict):
                continue
            fecha = dia.get("fecha")
            numero = dia.get("dia")
            for pred in dia.get("predicciones") or []:
                if not isinstance(pred, dict):
                    continue
                preds.append(
                    (
                        fecha,
                        numero,
                        str(pred.get("game_id") or ""),
                        pred.get("visitante"),
                        pred.get("home"),
                        pred.get("pick"),
                        pred.get("tipo_pick") or None,
                        _flotante(pred.get("probPick"), default=None),
                        _flotante(pred.get("odds"), default=None),
                        pred.get("resultado"),
                        pred.get("estado"),
                        None if pred.get("profit") is None else _flotante(pred.get("profit")),
                        _stake_prediccion(pred),
                        1 if pred.get("con_dinero") else 0,
                        _decision_mente(pred),
                        None
                        if pred.get("valida_stats") is None
                        else (1 if pred.get("valida_stats") else 0),
                    )
                )
            for ap in dia.get("apuestas") or []:
                if not isinstance(ap, dict):
                    continue
                apuestas.append(
                    (
                        fecha,
                        numero,
                        str(ap.get("game_id") or ""),
                        ap.get("pick"),
                        ap.get("estado"),
                        ap.get("resultado"),
                        None if ap.get("profit") is None else _flotante(ap.get("profit")),
                        _flotante(ap.get("stake"), 0.0),
                        _flotante(ap.get("odds"), default=None),
                        _flotante(ap.get("probPick"), default=None),
                    )
                )
        conn.executemany(
            """
            INSERT INTO predicciones (
                fecha, dia, game_id, visitante, home, pick, tipo_pick, prob, odds,
                resultado, estado, profit, stake, con_dinero, decision_mente, valida_stats
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            preds,
        )
        conn.executemany(
            """
            INSERT INTO apuestas (
                fecha, dia, game_id, pick, estado, resultado, profit, stake, odds, prob
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            apuestas,
        )
        conn.commit()
    finally:
        conn.close()
    return {"predicciones": len(preds), "apuestas": len(apuestas), "db": str(db_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exporta memoria_auditoria.json a SQLite.")
    parser.add_argument("--json", required=True, help="Ruta al memoria_auditoria.json")
    parser.add_argument(
        "--db",
        default="memoria.sqlite",
        help="Base de salida (por defecto memoria.sqlite en esta carpeta)",
    )
    args = parser.parse_args(argv)
    origen = Path(args.json)
    if not origen.is_file():
        print(f"No encuentro el JSON: {origen}", file=sys.stderr)
        return 1
    try:
        memoria = json.loads(origen.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"JSON ilegible: {exc}", file=sys.stderr)
        return 1
    if not isinstance(memoria, dict) or "dias" not in memoria:
        print("Ese JSON no parece una memoria del experimento.", file=sys.stderr)
        return 1
    resumen = exportar(memoria, Path(args.db))
    print(
        f"Listo: {resumen['predicciones']} predicciones y "
        f"{resumen['apuestas']} apuestas en {resumen['db']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
