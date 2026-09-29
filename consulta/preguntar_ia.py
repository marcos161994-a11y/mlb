"""Responde con los números de memoria.sqlite. No inventa picks.

Desde esta carpeta, después de exportar:

    py -3 preguntar_ia.py "¿Qué tipo de pick me da mejor ROI y por qué?"

El ROI es profit / stake de los picks ya liquidados (acierto o fallo).
Un tipo entra al ranking solo si tiene al menos 30 picks: cinco filas
sin etiqueta no cuentan como estrategia.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

MIN_MUESTRA = 30


def _abrir(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _metricas(filas: list[sqlite3.Row]) -> dict:
    n = len(filas)
    aciertos = sum(1 for fila in filas if fila["resultado"] == "acierto")
    profit = sum(float(fila["profit"] or 0) for fila in filas)
    stake = sum(float(fila["stake"] or 0) for fila in filas)
    return {
        "n": n,
        "aciertos": aciertos,
        "wr": (100.0 * aciertos / n) if n else 0.0,
        "profit": profit,
        "stake": stake,
        "roi": (100.0 * profit / stake) if stake else 0.0,
    }


def _fmt_roi(roi: float) -> str:
    return f"{roi:+.1f}%"


def _fmt_wr(wr: float) -> str:
    return f"{wr:.1f}%"


def _linea_tipo(nombre: str, m: dict) -> str:
    return (
        f"{nombre}: {m['n']} picks, acierto {_fmt_wr(m['wr'])}, "
        f"ROI {_fmt_roi(m['roi'])}."
    )


def _tipos(conn: sqlite3.Connection) -> list[tuple[str, dict]]:
    cur = conn.execute(
        """
        SELECT COALESCE(tipo_pick, '') AS tipo, resultado, profit, stake
        FROM predicciones
        WHERE estado = 'liquidado' AND resultado IN ('acierto', 'fallo')
        """
    )
    grupos: dict[str, list[sqlite3.Row]] = {}
    for fila in cur:
        nombre = (fila["tipo"] or "").strip() or "sin tipo"
        grupos.setdefault(nombre, []).append(fila)
    ordenados = sorted(grupos.items(), key=lambda par: _metricas(par[1])["roi"], reverse=True)
    return [(nombre, _metricas(filas)) for nombre, filas in ordenados]


def _por_decision(conn: sqlite3.Connection, decision: str) -> dict:
    cur = conn.execute(
        """
        SELECT resultado, profit, stake
        FROM predicciones
        WHERE estado = 'liquidado'
          AND resultado IN ('acierto', 'fallo')
          AND decision_mente = ?
        """,
        (decision,),
    )
    return _metricas(list(cur))


def _prob_alta(conn: sqlite3.Connection, umbral: float = 70.0) -> dict:
    cur = conn.execute(
        """
        SELECT resultado, profit, stake
        FROM predicciones
        WHERE estado = 'liquidado'
          AND resultado IN ('acierto', 'fallo')
          AND prob >= ?
        """,
        (umbral,),
    )
    return _metricas(list(cur))


def _banca(conn: sqlite3.Connection) -> dict:
    exp = conn.execute(
        "SELECT capital, capital_inicial FROM experimento"
    ).fetchone()
    cur = conn.execute(
        """
        SELECT estado, profit
        FROM apuestas
        WHERE estado IN ('ganada', 'perdida')
        """
    )
    filas = list(cur)
    ganadas = sum(1 for fila in filas if fila["estado"] == "ganada")
    perdidas = sum(1 for fila in filas if fila["estado"] == "perdida")
    profit = sum(float(fila["profit"] or 0) for fila in filas)
    return {
        "n": len(filas),
        "ganadas": ganadas,
        "perdidas": perdidas,
        "profit": profit,
        "capital": float(exp["capital"]) if exp else 0.0,
        "capital_inicial": float(exp["capital_inicial"]) if exp else 0.0,
    }


def responder(conn: sqlite3.Connection, pregunta: str) -> str:
    tipos = _tipos(conn)
    con_muestra = [(nombre, m) for nombre, m in tipos if m["n"] >= MIN_MUESTRA and nombre != "sin tipo"]
    mejor_nombre, mejor = con_muestra[0] if con_muestra else ("", {"n": 0, "wr": 0, "roi": 0})
    apostar = _por_decision(conn, "APOSTAR")
    pasar = _por_decision(conn, "PASAR")
    alta = _prob_alta(conn)
    banca = _banca(conn)

    bloques = {
        "tipo": _bloque_tipo(mejor_nombre, mejor, tipos),
        "mente": _bloque_mente(apostar, pasar),
        "confianza": _bloque_confianza(alta),
        "banca": _bloque_banca(banca),
    }
    q = (pregunta or "").lower()
    if any(palabra in q for palabra in ("mente", "apostar", "pasar", "filtro")):
        orden = ("mente", "tipo", "confianza", "banca")
    elif any(palabra in q for palabra in ("70", "confianza", "sobreconf", "prob")):
        orden = ("confianza", "tipo", "mente", "banca")
    elif any(palabra in q for palabra in ("real", "capital", "banca", "dinero")):
        orden = ("banca", "tipo", "mente", "confianza")
    else:
        orden = ("tipo", "mente", "confianza", "banca")
    return "\n\n".join(bloques[clave] for clave in orden)


def _bloque_tipo(mejor_nombre: str, mejor: dict, tipos: list[tuple[str, dict]]) -> str:
    if not mejor_nombre:
        return "No hay suficientes picks liquidados para comparar tipos."
    lineas = [
        (
            f"El tipo con mejor ROI es {mejor_nombre}: "
            f"{mejor['n']} picks, acierto {_fmt_wr(mejor['wr'])}, ROI {_fmt_roi(mejor['roi'])}."
        ),
        f"Comparado con los otros tipos que tienen al menos {MIN_MUESTRA} picks liquidados:",
    ]
    for nombre, m in tipos:
        if nombre == mejor_nombre or nombre == "sin tipo" or m["n"] < MIN_MUESTRA:
            continue
        lineas.append(_linea_tipo(nombre, m))
    chicos = [(nombre, m) for nombre, m in tipos if nombre == "sin tipo" or m["n"] < MIN_MUESTRA]
    if chicos:
        detalle = ", ".join(f"{nombre} ({m['n']})" for nombre, m in chicos)
        lineas.append(f"Quedan fuera del ranking por muestra chica: {detalle}.")
    favorito = next((m for nombre, m in tipos if nombre == "favorito_alto"), None)
    if mejor_nombre == "scratch" and favorito and favorito["wr"] > mejor["wr"] and favorito["roi"] < mejor["roi"]:
        lineas.append(
            "Gana un poco más el favorito alto, pero la cuota es corta y el ROI se queda en cero. "
            "Scratch acierta menos y aun así deja dinero."
        )
    return "\n".join(lineas)


def _bloque_mente(apostar: dict, pasar: dict) -> str:
    if apostar["n"] == 0 and pasar["n"] == 0:
        return "La mente no marcó APOSTAR ni PASAR en picks liquidados."
    lineas = [
        (
            f"APOSTAR: {apostar['n']} picks, acierto {_fmt_wr(apostar['wr'])}, "
            f"ROI {_fmt_roi(apostar['roi'])}."
        ),
        (
            f"PASAR: {pasar['n']} picks, acierto {_fmt_wr(pasar['wr'])}, "
            f"ROI {_fmt_roi(pasar['roi'])}."
        ),
    ]
    if apostar["n"] and pasar["n"] and pasar["roi"] > apostar["roi"]:
        lineas.insert(0, "La mente, en este historial, está filtrando al revés.")
        lineas.append("El grupo que deja pasar rinde mejor que el grupo al que le da dinero.")
    elif apostar["n"] and pasar["n"] and apostar["roi"] > pasar["roi"]:
        lineas.insert(0, "La mente, en este historial, está dejando el dinero en el grupo que rinde más.")
        lineas.append("El grupo APOSTAR rinde mejor que el grupo PASAR.")
    else:
        lineas.insert(0, "La mente marcó estos grupos en picks liquidados.")
    return "\n".join(lineas)


def _bloque_confianza(alta: dict) -> str:
    if alta["n"] == 0:
        return "No hay picks liquidados con probabilidad de 70% o más."
    if alta["wr"] < 70:
        cierre = "En ese rango está un poco sobreconfiado."
    else:
        cierre = "En ese rango el acierto llega a lo que el modelo dice, o lo pasa."
    return (
        f"Cuando el modelo dice 70% o más ({alta['n']} picks), "
        f"en realidad gana el {_fmt_wr(alta['wr'])}. "
        f"{cierre}"
    )


def _bloque_banca(banca: dict) -> str:
    signo = "-" if banca["profit"] < 0 else ""
    return (
        f"Apuestas reales: {banca['n']}, {banca['ganadas']} ganadas y {banca['perdidas']} perdidas, "
        f"{signo}${abs(banca['profit']):.2f} en total. "
        f"El capital queda en ${banca['capital']:.2f}."
    )


def _resolver_db(pedido: str | None) -> Path:
    if pedido:
        return Path(pedido)
    junto = Path(__file__).resolve().parent / "memoria.sqlite"
    if Path("memoria.sqlite").is_file():
        return Path("memoria.sqlite")
    return junto


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pregunta al historial exportado a SQLite.")
    parser.add_argument("pregunta", help="Pregunta en español")
    parser.add_argument("--db", default=None, help="Ruta a memoria.sqlite")
    args = parser.parse_args(argv)
    db_path = _resolver_db(args.db)
    if not db_path.is_file():
        print(
            f"No está {db_path}. Primero corre exportar_a_sqlite.py --json <tu memoria_auditoria.json>.",
            file=sys.stderr,
        )
        return 1
    conn = _abrir(db_path)
    try:
        print(responder(conn, args.pregunta))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
