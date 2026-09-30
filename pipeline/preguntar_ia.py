#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preguntar_ia.py — Pregúntale a tu IA local (Ollama) sobre tus apuestas.

Lee las vistas de apuestas.db (creadas por exportar_a_sqlite.py), arma un
contexto corto en español y lo manda a Ollama (http://localhost:11434).
Solo librería estándar: sqlite3, json, urllib, argparse.

Las filas de rendimiento muestran la n (tamaño de muestra) junto al hit rate
y al ROI. Si n es menor que el umbral, se marcan como muestra chica para que
el modelo no saque conclusiones de dos apuestas sueltas.

Uso (Windows):
    py -3 preguntar_ia.py "¿Qué tipo de pick me está dando mejor ROI?"
    py -3 preguntar_ia.py --solo-contexto "x"      (ver el contexto sin llamar a Ollama)
    py -3 preguntar_ia.py --modelo llama3.1:8b --dias 14 "Resume la última semana"
    py -3 preguntar_ia.py --min-muestra 30 "¿Qué tipo tiene mejor ROI?"
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
# Por debajo de esta n la fila no sirve para concluir (ROI de 2 apuestas, etc.).
MIN_MUESTRA = 30
MARCA_MUESTRA_CHICA = "MUESTRA CHICA (no concluyente)"
TITULO_REAL = "APUESTAS REALES (dinero)"
TITULO_PAPEL = "PICKS EN PAPEL (simulación)"


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


def consultar(con, sql, params=()):
    """Devuelve (lista de dicts, error). error es None si la consulta salió bien."""
    try:
        cur = con.execute(sql, params)
    except sqlite3.Error as e:
        return [], str(e)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, fila)) for fila in cur.fetchall()], None


def _n_de(fila):
    """n de la fila: cantidad de picks (columna picks de la vista)."""
    n = fila.get("picks")
    try:
        return int(n) if n is not None else 0
    except (TypeError, ValueError):
        return 0


def _con_n(valor, n):
    """Pega la n al lado del hit rate o del ROI para que no se citen solos."""
    if valor is None or valor == "":
        return f"(sin dato) (n={n})"
    return f"{valor}% (n={n})"


def _nota(n, min_muestra):
    if n < min_muestra:
        return MARCA_MUESTRA_CHICA
    return ""


def _texto_tabla(filas_texto):
    if not filas_texto:
        return "(sin datos)"
    return "\n".join(" | ".join(str(c) for c in fila) for fila in filas_texto)


def formatear_rendimiento_mercado(filas, min_muestra):
    """Tabla de mercado/tipo con n junto al hit rate y al ROI."""
    if not filas:
        return "(sin datos)"
    lineas = [[
        "sport", "market", "pick_type", "n", "ganados", "perdidos",
        "hit_rate_pct", "roi_pct", "profit", "nota",
    ]]
    for f in filas:
        n = _n_de(f)
        lineas.append([
            f.get("sport") or "",
            f.get("market") or "",
            f.get("pick_type") or "",
            n,
            "" if f.get("ganados") is None else f.get("ganados"),
            "" if f.get("perdidos") is None else f.get("perdidos"),
            _con_n(f.get("hit_rate_pct"), n),
            _con_n(f.get("roi_pct"), n),
            "" if f.get("profit") is None else f.get("profit"),
            _nota(n, min_muestra),
        ])
    return _texto_tabla(lineas)


def formatear_rendimiento_decision(filas, min_muestra):
    """Tabla por decisión de la mente, con la misma marca de muestra chica."""
    if not filas:
        return "(sin datos)"
    lineas = [[
        "sport", "decision", "n", "ganados", "perdidos",
        "hit_rate_pct", "roi_pct", "profit", "nota",
    ]]
    for f in filas:
        n = _n_de(f)
        lineas.append([
            f.get("sport") or "",
            f.get("decision") or "",
            n,
            "" if f.get("ganados") is None else f.get("ganados"),
            "" if f.get("perdidos") is None else f.get("perdidos"),
            _con_n(f.get("hit_rate_pct"), n),
            _con_n(f.get("roi_pct"), n),
            "" if f.get("profit") is None else f.get("profit"),
            _nota(n, min_muestra),
        ])
    return _texto_tabla(lineas)


def mejor_tipo_suficiente(filas, min_muestra):
    """Elige en Python el pick_type con mejor ROI entre filas de n suficiente.

    No mira las filas chicas: un ROI de 108% con n=2 no puede ganar.
    """
    candidatas = []
    for f in filas:
        n = _n_de(f)
        roi = f.get("roi_pct")
        if n < min_muestra or roi is None:
            continue
        try:
            roi_num = float(roi)
        except (TypeError, ValueError):
            continue
        candidatas.append((roi_num, n, f))
    if not candidatas:
        return (
            "Mejor tipo con muestra suficiente: no hay "
            f"(ninguna fila con n>={min_muestra} y ROI)"
        )
    # Mayor ROI; a igualdad, la muestra más grande.
    candidatas.sort(key=lambda t: (-t[0], -t[1], str(t[2].get("pick_type") or "")))
    roi_num, n, f = candidatas[0]
    tipo = f.get("pick_type") or "(sin tipo)"
    kind = f.get("kind") or ""
    market = f.get("market") or ""
    roi_txt = f.get("roi_pct")
    return (
        f"Mejor tipo con muestra suficiente: {tipo} "
        f"({kind}, {market}, n={n}, ROI {roi_txt}%)"
    )


def _filtrar_kind(filas, kind):
    return [f for f in filas if f.get("kind") == kind]


def _bloque_kind(titulo, mercado, decision, min_muestra):
    return "\n\n".join([
        titulo,
        "RENDIMIENTO POR MERCADO Y TIPO DE PICK (hit rate y ROI):\n"
        + formatear_rendimiento_mercado(mercado, min_muestra),
        "RENDIMIENTO SEGÚN LA DECISIÓN DE LA MENTE:\n"
        + formatear_rendimiento_decision(decision, min_muestra),
    ])


def construir_contexto(ruta_db: Path, dias: int, min_muestra: int = MIN_MUESTRA) -> str:
    con = sqlite3.connect(str(ruta_db))
    try:
        partes = []

        mercado, err_mercado = consultar(con,
            "SELECT sport, market, kind, pick_type, picks, ganados, perdidos, "
            "hit_rate_pct, roi_pct, profit FROM v_rendimiento_por_mercado "
            "ORDER BY picks DESC, pick_type")
        decision, err_decision = consultar(con,
            "SELECT sport, kind, decision, picks, ganados, perdidos, "
            "hit_rate_pct, roi_pct, profit FROM v_rendimiento_por_decision "
            "ORDER BY picks DESC, decision")

        # Línea ya calculada: el modelo no tiene que comparar ROI de muestras distintas.
        if err_mercado:
            partes.append(
                "Mejor tipo con muestra suficiente: (no disponible: "
                f"{err_mercado})")
        else:
            partes.append(mejor_tipo_suficiente(mercado, min_muestra))
        partes.append(
            f"Umbral de muestra: n>={min_muestra}. "
            f"Si n < {min_muestra}, la fila dice {MARCA_MUESTRA_CHICA} "
            "y no es concluyente. Prefiere la n más grande.")

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

        real_m = [] if err_mercado else _filtrar_kind(mercado, "real")
        papel_m = [] if err_mercado else _filtrar_kind(mercado, "papel")
        real_d = [] if err_decision else _filtrar_kind(decision, "real")
        papel_d = [] if err_decision else _filtrar_kind(decision, "papel")

        if err_mercado or err_decision:
            aviso = err_mercado or err_decision
            partes.append(f"{TITULO_REAL}\n(no disponible: {aviso})")
            partes.append(f"{TITULO_PAPEL}\n(no disponible: {aviso})")
        else:
            partes.append(_bloque_kind(TITULO_REAL, real_m, real_d, min_muestra))
            partes.append(_bloque_kind(TITULO_PAPEL, papel_m, papel_d, min_muestra))

        partes.append("CALIBRACIÓN (prob. del modelo vs % real):\n" + tabla(con,
            "SELECT * FROM v_calibracion"))

        partes.append("PICKS PENDIENTES:\n" + tabla(con,
            "SELECT sport, kind, fecha, inicio, visitante, local, pick, prob_pct, cuota, "
            "edge_pct, decision FROM v_picks_pendientes LIMIT 20"))

        partes.append("ÚLTIMAS LECCIONES:\n" + tabla(con,
            "SELECT sport, fecha, tipo, pick, leccion FROM v_lecciones_recientes LIMIT 10"))
        return "\n\n".join(partes)
    finally:
        con.close()


def instrucciones(min_muestra: int) -> str:
    """Texto fijo para que el modelo no concluya con muestras diminutas."""
    return (
        "Eres el analista de apuestas de MLB de Marcos. Responde en español, claro y breve. "
        "Responde solo con los datos de abajo y solo sobre apuestas de MLB. "
        "Si los datos no alcanzan para responder, dilo con claridad y no inventes cifras. "
        "Cada vez que uses un número (ROI, hit rate, profit u otro), cita la n de esa fila. "
        f"Nunca concluyas a partir de filas marcadas {MARCA_MUESTRA_CHICA}: "
        f"son las de n < {min_muestra} y no son concluyentes. "
        "Prefiere las muestras más grandes. "
        f"No mezcles secciones: {TITULO_REAL} es dinero de verdad; "
        f"{TITULO_PAPEL} es predicción sin dinero. "
        "La línea 'Mejor tipo con muestra suficiente' ya está calculada solo con filas "
        f"de n>={min_muestra}; no la reemplaces por una fila de muestra chica. "
        "ROI y hit rate están en %."
    )


def preguntar_ollama(pregunta, contexto, modelo, url, timeout, min_muestra=MIN_MUESTRA):
    prompt = (
        f"{instrucciones(min_muestra)}\n\n"
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
    ap.add_argument("--min-muestra", type=int, default=MIN_MUESTRA,
                    help="Mínimo de picks (n) para tratar una fila como concluyente (default: 30)")
    a = ap.parse_args()

    ruta_db = Path(a.db)
    if not ruta_db.exists():
        print(f"ERROR: no encuentro {ruta_db}. Corre primero exportar_a_sqlite.py")
        sys.exit(1)

    contexto = construir_contexto(ruta_db, a.dias, a.min_muestra)
    if a.solo_contexto:
        print(contexto)
        return

    pregunta = " ".join(a.pregunta)
    try:
        print(preguntar_ollama(pregunta, contexto, a.modelo, a.url, a.timeout, a.min_muestra))
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
