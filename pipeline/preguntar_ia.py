#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preguntar_ia.py — Pregúntale a tu IA local (Ollama) sobre tus apuestas.

Lee las vistas de apuestas.db (creadas por exportar_a_sqlite.py), arma un
contexto corto en español y lo manda a Ollama (http://localhost:11434).
Solo librería estándar: sqlite3, json, urllib, argparse.

Uso (Windows):
    py -3 preguntar_ia.py "¿Qué tipo de pick me está dando mejor ROI?"
    py -3 preguntar_ia.py --solo-contexto "x"      (ver el contexto sin llamar a Ollama)
    py -3 preguntar_ia.py --modelo llama3.1:8b --dias 14 "Resume la última semana"
"""

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AQUI = Path(__file__).resolve().parent
URL_OLLAMA = "http://localhost:11434/api/generate"
MODELO = "qwen2.5:7b"


def tabla(con, sql, params=()):
    """Ejecuta una consulta y la devuelve como texto tipo tabla (compacto)."""
    try:
        cur = con.execute(sql, params)
    except sqlite3.Error as e:
        return f"(no disponible: {e})"
    cols = [d[0] for d in cur.description]
    filas = cur.fetchall()
    if not filas:
        return "(sin datos)"
    lineas = [" | ".join(cols)]
    for f in filas:
        lineas.append(" | ".join("" if v is None else str(v)[:80] for v in f))
    return "\n".join(lineas)


def construir_contexto(ruta_db: Path, dias: int) -> str:
    con = sqlite3.connect(str(ruta_db))
    partes = []

    # Estado del experimento (capital, día) desde meta
    meta = dict(con.execute(
        "SELECT sport || '.' || key, value FROM meta WHERE key IN "
        "('modo','capital','capital_inicial','dia_actual','dias_totales')").fetchall())
    if meta:
        partes.append("ESTADO DEL EXPERIMENTO:\n" + ", ".join(f"{k}={v}" for k, v in meta.items()))

    partes.append(f"RESUMEN DIARIO (últimos {dias} días):\n" + tabla(con,
        "SELECT sport, fecha, picks_papel, aciertos_papel, fallos_papel, pct_acierto_papel, "
        "profit_papel, apuestas_reales, ganadas_reales, profit_real, pendientes "
        "FROM v_resumen_diario ORDER BY fecha DESC LIMIT ?", (dias,)))

    partes.append("RENDIMIENTO POR MERCADO Y TIPO DE PICK (hit rate y ROI):\n" + tabla(con,
        "SELECT sport, market, kind, pick_type, picks, ganados, perdidos, hit_rate_pct, "
        "roi_pct, profit FROM v_rendimiento_por_mercado ORDER BY sport, kind, picks DESC"))

    partes.append("RENDIMIENTO SEGÚN LA DECISIÓN DE LA MENTE:\n" + tabla(con,
        "SELECT * FROM v_rendimiento_por_decision"))

    partes.append("CALIBRACIÓN (prob. del modelo vs % real):\n" + tabla(con,
        "SELECT * FROM v_calibracion"))

    partes.append("PICKS PENDIENTES:\n" + tabla(con,
        "SELECT sport, kind, fecha, inicio, visitante, local, pick, prob_pct, cuota, "
        "edge_pct, decision FROM v_picks_pendientes LIMIT 20"))

    partes.append("ÚLTIMAS LECCIONES:\n" + tabla(con,
        "SELECT sport, fecha, tipo, pick, leccion FROM v_lecciones_recientes LIMIT 10"))
    con.close()
    return "\n\n".join(partes)


def preguntar_ollama(pregunta, contexto, modelo, url, timeout):
    prompt = (
        "Eres el analista de apuestas de Marcos. Responde en español, claro y breve. "
        "Usa SOLO los datos de abajo; si no alcanzan, dilo. 'papel' = predicción sin "
        "dinero, 'real' = apuesta con dinero. ROI y hit rate están en %.\n\n"
        f"=== DATOS ===\n{contexto}\n=== FIN DATOS ===\n\nPregunta: {pregunta}\nRespuesta:"
    )
    cuerpo = json.dumps({"model": modelo, "prompt": prompt, "stream": False}).encode("utf-8")
    req = urllib.request.Request(url, data=cuerpo, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("response", "").strip() or "(Ollama respondió vacío)"


def main():
    ap = argparse.ArgumentParser(description="Pregunta a Ollama sobre apuestas.db")
    ap.add_argument("pregunta", nargs="+", help="Tu pregunta (entre comillas)")
    ap.add_argument("--db", default=str(AQUI / "apuestas.db"))
    ap.add_argument("--modelo", default=MODELO)
    ap.add_argument("--url", default=URL_OLLAMA)
    ap.add_argument("--dias", type=int, default=10, help="Días del resumen diario a incluir")
    ap.add_argument("--timeout", type=int, default=300, help="Segundos máximos de espera")
    ap.add_argument("--solo-contexto", action="store_true", help="Imprime el contexto y sale")
    a = ap.parse_args()

    ruta_db = Path(a.db)
    if not ruta_db.exists():
        print(f"ERROR: no encuentro {ruta_db}. Corre primero exportar_a_sqlite.py")
        sys.exit(1)

    contexto = construir_contexto(ruta_db, a.dias)
    if a.solo_contexto:
        print(contexto)
        return

    pregunta = " ".join(a.pregunta)
    try:
        print(preguntar_ollama(pregunta, contexto, a.modelo, a.url, a.timeout))
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", "replace")[:300]
        print(f"Ollama respondió error HTTP {e.code}: {detalle}")
        if e.code == 404:
            print(f"¿Descargaste el modelo? Prueba:  ollama pull {a.modelo}")
        sys.exit(2)
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        print(f"No pude conectar con Ollama en {a.url} ({e}).")
        print("Abre Ollama (o corre 'ollama serve') y verifica con:  ollama list")
        sys.exit(2)


if __name__ == "__main__":
    main()
