#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exportar_a_sqlite.py — Quantium / Quantum MLB → base local SQLite.

Lee el archivo memoria_auditoria.json del repo MLB (solo lectura, nunca lo
modifica) y lo vuelca en una base SQLite local con el esquema común para
todos los modelos (MLB, NBA, caballos):

    events       -> un juego / carrera
    predictions  -> cada pick del modelo (papel) o apuesta real
    results      -> liquidación de cada pick (ganó/perdió, profit)
    lessons      -> "lecciones" que la mente aprendió
    meta         -> datos sueltos (capital, versión del modelo, etc.)

Es idempotente: se puede correr las veces que quieras; usa UPSERT
(INSERT ... ON CONFLICT DO UPDATE), así que nunca duplica filas.

Solo usa la librería estándar de Python 3 (json, sqlite3, argparse, pathlib).

Uso (Windows):
    py -3 exportar_a_sqlite.py --json C:\\ruta\\mlb\\memoria_auditoria.json
    py -3 exportar_a_sqlite.py --json memoria_auditoria.json --db C:\\datos\\apuestas.db
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# En Windows la consola puede no ser UTF-8; evitamos que un acento rompa el print.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AQUI = Path(__file__).resolve().parent
SPORT = "mlb"
LIGA = "MLB"
MODELO_PICK = "quantum_mlb_ensemble"   # modelo_mlb + Elo + RF/XGB + calibración
MERCADO = "moneyline"                   # hoy todos los picks del repo son "... ML"

# ---------------------------------------------------------------------------
# Esquema común (mismo para MLB, NBA y caballos)
# ---------------------------------------------------------------------------
ESQUEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,      -- 'mlb:823653' (deporte:id_fuente)
    sport           TEXT NOT NULL,         -- mlb | nba | horse
    source_id       TEXT,                  -- id original (gamePk de MLB)
    league_or_track TEXT,                  -- 'MLB', 'NBA', 'Hipódromo Camarero'
    event_date      TEXT,                  -- fecha del día del experimento (YYYY-MM-DD)
    event_time      TEXT,                  -- inicio ISO con zona horaria
    home_or_race    TEXT,                  -- local (o nombre/número de carrera)
    away_or_null    TEXT,                  -- visitante (NULL en caballos)
    home_extra      TEXT,                  -- MLB: pitcher abridor local
    away_extra      TEXT,                  -- MLB: pitcher abridor visitante
    status          TEXT,                  -- scheduled | final
    final_result    TEXT,                  -- marcador final en texto
    away_score      INTEGER,
    home_score      INTEGER,
    winner          TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id   TEXT PRIMARY KEY,      -- 'mlb:pred:823653' o 'mlb:bet:823653'
    event_id        TEXT NOT NULL REFERENCES events(event_id),
    sport           TEXT NOT NULL,
    kind            TEXT NOT NULL,         -- 'papel' (predicción) | 'real' (apuesta con dinero)
    model           TEXT,                  -- nombre del modelo que generó el pick
    model_version   TEXT,
    decision_model  TEXT,                  -- quién decidió apostar/pasar (mente, heurística...)
    created_at      TEXT,                  -- cuándo se congeló el pick
    event_date      TEXT,
    market          TEXT,                  -- moneyline | win | place | show | exacta ...
    selection       TEXT,                  -- 'Minnesota Twins ML'
    pick_type       TEXT,                  -- MLB: underdog | favorito_alto | limpio | scratch
    model_prob      REAL,                  -- probabilidad del modelo (0-1)
    odds_decimal    REAL,
    odds_american   INTEGER,
    odds_source     TEXT,                  -- draftkings | modelo | ...
    odds_is_market  INTEGER,               -- 1 si la cuota vino de una casa real, 0 si la generó el modelo
    bookmaker       TEXT,
    implied_prob    REAL,                  -- 1 / odds_decimal
    edge_pct        REAL,                  -- ventaja en puntos % según el modelo
    decision        TEXT,                  -- bet | pass | wait | NULL
    decision_raw    TEXT,                  -- APOSTAR | PASAR | ESPERAR (original)
    apostable       INTEGER,
    stake           REAL,                  -- $ arriesgado (virtual en papel)
    stake_is_real   INTEGER,               -- 1 si hubo dinero (con_dinero)
    confidence      INTEGER,               -- 1-5 según la mente
    rationale_text  TEXT,                  -- motivo en texto (útil para la IA)
    features_json   TEXT,                  -- features ML en JSON
    extra_json      TEXT,                  -- el registro original completo (nada se pierde)
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS results (
    prediction_id   TEXT PRIMARY KEY REFERENCES predictions(prediction_id),
    settled_at      TEXT,
    outcome         TEXT,                  -- win | loss | push
    profit          REAL,
    closing_odds    REAL,                  -- cuota congelada/cierre si existe
    final_score     TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lessons (
    lesson_id       TEXT PRIMARY KEY,
    sport           TEXT NOT NULL,
    lesson_date     TEXT,
    event_id        TEXT,                  -- sin FK: algunas lecciones son de juegos sin predicción
    tipo            TEXT,                  -- acierto_refuerzo | fallo_postmortem | ...
    patron          TEXT,
    selection       TEXT,
    model_prob      REAL,
    edge_pct        REAL,
    final_score     TEXT,
    text            TEXT,                  -- la lección en frases
    motivo          TEXT,
    confianza       REAL,
    fuente          TEXT,
    modelo          TEXT,
    created_at      TEXT,
    extra_json      TEXT,
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    sport TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (sport, key)
);

CREATE INDEX IF NOT EXISTS ix_pred_event ON predictions(event_id);
CREATE INDEX IF NOT EXISTS ix_pred_date  ON predictions(sport, event_date);
CREATE INDEX IF NOT EXISTS ix_less_date  ON lessons(sport, lesson_date);
"""

# ---------------------------------------------------------------------------
# Vistas pensadas para que una IA local (Ollama) lea resúmenes cortos
# ---------------------------------------------------------------------------
VISTAS = """
DROP VIEW IF EXISTS v_picks;
CREATE VIEW v_picks AS
SELECT p.*, e.event_time, e.home_or_race, e.away_or_null, e.status AS event_status,
       r.outcome, r.profit, r.settled_at, r.final_score
FROM predictions p
JOIN events e ON e.event_id = p.event_id
LEFT JOIN results r ON r.prediction_id = p.prediction_id;

-- Resumen por día y deporte: papel vs dinero real
DROP VIEW IF EXISTS v_resumen_diario;
CREATE VIEW v_resumen_diario AS
SELECT sport, event_date AS fecha,
       SUM(kind='papel')                                           AS picks_papel,
       SUM(kind='papel' AND outcome='win')                         AS aciertos_papel,
       SUM(kind='papel' AND outcome='loss')                        AS fallos_papel,
       ROUND(100.0*SUM(kind='papel' AND outcome='win')
             / NULLIF(SUM(kind='papel' AND outcome IN ('win','loss')),0),1) AS pct_acierto_papel,
       ROUND(SUM(CASE WHEN kind='papel' THEN profit END),2)        AS profit_papel,
       SUM(kind='real')                                            AS apuestas_reales,
       SUM(kind='real' AND outcome='win')                          AS ganadas_reales,
       ROUND(SUM(CASE WHEN kind='real' THEN stake END),2)          AS stake_real,
       ROUND(SUM(CASE WHEN kind='real' THEN profit END),2)         AS profit_real,
       SUM(outcome IS NULL)                                        AS pendientes
FROM v_picks
GROUP BY sport, event_date;

-- Rendimiento por mercado / tipo de pick / papel-real: hit rate y ROI
DROP VIEW IF EXISTS v_rendimiento_por_mercado;
CREATE VIEW v_rendimiento_por_mercado AS
SELECT sport, market, kind, COALESCE(pick_type,'(sin tipo)') AS pick_type,
       COUNT(*)                                     AS picks,
       SUM(outcome='win')                           AS ganados,
       SUM(outcome='loss')                          AS perdidos,
       ROUND(100.0*SUM(outcome='win')/NULLIF(SUM(outcome IN ('win','loss')),0),1) AS hit_rate_pct,
       ROUND(AVG(model_prob)*100,1)                 AS prob_modelo_prom_pct,
       ROUND(AVG(odds_decimal),3)                   AS cuota_prom,
       ROUND(SUM(CASE WHEN outcome IS NOT NULL THEN stake END),2) AS apostado,
       ROUND(SUM(profit),2)                         AS profit,
       ROUND(100.0*SUM(profit)/NULLIF(SUM(CASE WHEN outcome IS NOT NULL THEN stake END),0),1) AS roi_pct
FROM v_picks
GROUP BY sport, market, kind, COALESCE(pick_type,'(sin tipo)');

-- Rendimiento según lo que decidió la mente (APOSTAR / PASAR / ESPERAR)
DROP VIEW IF EXISTS v_rendimiento_por_decision;
CREATE VIEW v_rendimiento_por_decision AS
SELECT sport, kind, COALESCE(decision,'(sin decisión)') AS decision,
       COUNT(*) AS picks,
       SUM(outcome='win') AS ganados, SUM(outcome='loss') AS perdidos,
       ROUND(100.0*SUM(outcome='win')/NULLIF(SUM(outcome IN ('win','loss')),0),1) AS hit_rate_pct,
       ROUND(SUM(profit),2) AS profit,
       ROUND(100.0*SUM(profit)/NULLIF(SUM(CASE WHEN outcome IS NOT NULL THEN stake END),0),1) AS roi_pct
FROM v_picks GROUP BY sport, kind, COALESCE(decision,'(sin decisión)');

-- Calibración: ¿el 60% del modelo gana ~60%?
DROP VIEW IF EXISTS v_calibracion;
CREATE VIEW v_calibracion AS
SELECT sport, CAST(model_prob*10 AS INTEGER)*10 AS prob_desde_pct,
       COUNT(*) AS picks,
       ROUND(AVG(model_prob)*100,1) AS prob_modelo_prom_pct,
       ROUND(100.0*SUM(outcome='win')/NULLIF(SUM(outcome IN ('win','loss')),0),1) AS real_pct
FROM v_picks WHERE model_prob IS NOT NULL AND outcome IS NOT NULL
GROUP BY sport, CAST(model_prob*10 AS INTEGER);

-- Picks sin liquidar (lo que la IA debería vigilar)
DROP VIEW IF EXISTS v_picks_pendientes;
CREATE VIEW v_picks_pendientes AS
SELECT sport, kind, event_date AS fecha, event_time AS inicio,
       away_or_null AS visitante, home_or_race AS local, selection AS pick,
       ROUND(model_prob*100,1) AS prob_pct, odds_decimal AS cuota, edge_pct,
       decision, stake, rationale_text AS motivo
FROM v_picks WHERE outcome IS NULL
ORDER BY event_time;

-- Últimas lecciones con texto
DROP VIEW IF EXISTS v_lecciones_recientes;
CREATE VIEW v_lecciones_recientes AS
SELECT sport, lesson_date AS fecha, tipo, patron, selection AS pick, text AS leccion
FROM lessons WHERE text IS NOT NULL AND text <> ''
ORDER BY lesson_date DESC, created_at DESC;
"""

# ---------------------------------------------------------------------------
# Utilidades de conversión
# ---------------------------------------------------------------------------
def num(v):
    """Convierte a float o None (tolera strings y basura)."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def entero(v):
    f = num(v)
    return int(round(f)) if f is not None else None


def booleano(v):
    return None if v is None else (1 if v else 0)


def jdump(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


# "Texas Rangers 4 - Minnesota Twins 6" -> ("Texas Rangers", 4, "Minnesota Twins", 6)
MARCADOR_RE = re.compile(r"^\s*(.+?)\s+(\d+)\s*-\s*(.+?)\s+(\d+)\s*$")


def parse_marcador(texto):
    if not texto or not isinstance(texto, str):
        return None
    m = MARCADOR_RE.match(texto)
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3), int(m.group(4))


DECISION_MAP = {"APOSTAR": "bet", "PASAR": "pass", "ESPERAR": "wait"}


def outcome_de(reg):
    """Normaliza el resultado: predicciones usan 'resultado' (acierto/fallo),
    apuestas usan 'estado' (ganada/perdida)."""
    r = (reg.get("resultado") or "").lower()
    e = (reg.get("estado") or "").lower()
    if r == "acierto" or e == "ganada":
        return "win"
    if r == "fallo" or e == "perdida":
        return "loss"
    if r in ("push", "empate", "nulo") or e in ("push", "empate", "nula", "anulada"):
        return "push"
    return None


# Campos del JSON que sí van a columnas (el resto queda en extra_json)
CAMPOS_MAPEADOS = {
    "game_id", "visitante", "home", "pick", "odds", "odds_american", "edge", "probPick",
    "apostable", "lineas_fuente", "casa", "motivo_apuesta", "pitcherAway", "pitcherHome",
    "inicio_juego", "estado", "resultado", "profit", "stake", "stake_virtual", "con_dinero",
    "predicho_en", "bloqueado_en", "ia_mente", "ml_features", "tipo_pick", "marcador_final",
    "liquidado_en", "odds_congelada",
}
CAMPOS_LECCION = {
    "id", "patron", "game_id", "fecha", "tipo", "pick", "probPick", "edge",
    "marcador_final", "leccion", "motivo", "confianza", "fuente", "modelo", "creada_en",
    "visitante", "home",
}

# ---------------------------------------------------------------------------
# UPSERT genérico
# ---------------------------------------------------------------------------
def upsert(cur, tabla, pk, fila):
    """INSERT ... ON CONFLICT(pk) DO UPDATE: idempotente, sin duplicados."""
    cols = list(fila.keys())
    marcas = ",".join("?" for _ in cols)
    pks = [pk] if isinstance(pk, str) else list(pk)
    sets = ",".join(f"{c}=excluded.{c}" for c in cols if c not in pks)
    sql = (f"INSERT INTO {tabla} ({','.join(cols)}) VALUES ({marcas}) "
           f"ON CONFLICT({','.join(pks)}) DO UPDATE SET {sets}, updated_at=datetime('now')")
    cur.execute(sql, [fila[c] for c in cols])


def upsert_evento(cur, fecha, reg):
    gid = str(reg.get("game_id"))
    eid = f"{SPORT}:{gid}"
    marc = reg.get("marcador_final")
    p = parse_marcador(marc)
    away_score = home_score = winner = None
    if p:
        away_score, home_score = p[1], p[3]
        if away_score != home_score:
            winner = p[0] if away_score > home_score else p[2]
    fila = {
        "event_id": eid, "sport": SPORT, "source_id": gid, "league_or_track": LIGA,
        "event_date": fecha, "event_time": reg.get("inicio_juego"),
        "home_or_race": reg.get("home"), "away_or_null": reg.get("visitante"),
        "home_extra": reg.get("pitcherHome"), "away_extra": reg.get("pitcherAway"),
        "status": "final" if marc else "scheduled", "final_result": marc,
        "away_score": away_score, "home_score": home_score, "winner": winner,
    }
    # No pisar datos buenos con vacíos: si ya existe, solo actualizamos lo que venga lleno.
    existe = cur.execute("SELECT 1 FROM events WHERE event_id=?", (eid,)).fetchone()
    if existe:
        llenos = {k: v for k, v in fila.items() if v is not None}
        upsert(cur, "events", "event_id", llenos)
    else:
        upsert(cur, "events", "event_id", fila)
    return eid


def fila_prediccion(fecha, reg, kind, eid, version):
    mente = reg.get("ia_mente") if isinstance(reg.get("ia_mente"), dict) else {}
    odds = num(reg.get("odds"))
    prob = num(reg.get("probPick"))
    fuente = reg.get("lineas_fuente")
    dec_raw = mente.get("decision")
    if kind == "real":
        stake = num(reg.get("stake"))
        decision, stake_real = "bet", 1
        creado = reg.get("bloqueado_en")
    else:
        stake = num(reg.get("stake_virtual"))
        decision = DECISION_MAP.get(dec_raw)
        stake_real = booleano(reg.get("con_dinero"))
        creado = reg.get("predicho_en")
    return {
        "prediction_id": f"{SPORT}:{'bet' if kind == 'real' else 'pred'}:{reg.get('game_id')}",
        "event_id": eid, "sport": SPORT, "kind": kind,
        "model": MODELO_PICK, "model_version": version,
        "decision_model": mente.get("fuente"),
        "created_at": creado, "event_date": fecha,
        "market": MERCADO if str(reg.get("pick", "")).strip().endswith(" ML") else "otro",
        "selection": reg.get("pick"), "pick_type": reg.get("tipo_pick"),
        "model_prob": prob / 100.0 if prob is not None else None,
        "odds_decimal": odds, "odds_american": entero(reg.get("odds_american")),
        "odds_source": fuente,
        "odds_is_market": 0 if fuente == "modelo" else (1 if fuente else None),
        "bookmaker": reg.get("casa") or fuente,
        "implied_prob": round(1.0 / odds, 4) if odds and odds > 1 else None,
        "edge_pct": num(reg.get("edge")),
        "decision": decision, "decision_raw": dec_raw if kind == "papel" else "APOSTAR",
        "apostable": booleano(reg.get("apostable")),
        "stake": stake, "stake_is_real": stake_real,
        "confidence": entero(mente.get("confianza")),
        "rationale_text": reg.get("motivo_apuesta"),
        "features_json": jdump(reg.get("ml_features")),
        "extra_json": jdump(reg),
    }


def fila_resultado(pid, reg):
    out = outcome_de(reg)
    if out is None:
        return None
    return {
        "prediction_id": pid, "settled_at": reg.get("liquidado_en"), "outcome": out,
        "profit": num(reg.get("profit")), "closing_odds": num(reg.get("odds_congelada")),
        "final_score": reg.get("marcador_final"),
    }


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------
def exportar(ruta_json: Path, ruta_db: Path) -> dict:
    with ruta_json.open("r", encoding="utf-8") as fh:
        mem = json.load(fh)

    ml = mem.get("ml_meta") or {}
    version = f"ml_schema{ml.get('schema', '?')}"
    if ml.get("ultimo_entreno"):
        version += f"_entreno{str(ml['ultimo_entreno'])[:10]}"

    con = sqlite3.connect(str(ruta_db))
    con.executescript(ESQUEMA)
    cur = con.cursor()
    sin_mapear = {}          # campo -> nº de veces (quedó solo en extra_json)
    avisos = []
    stats = {"predicciones": 0, "apuestas": 0, "resultados": 0, "lecciones": 0}

    for dia in mem.get("dias") or []:
        fecha = dia.get("fecha")
        for kind, lista in (("papel", dia.get("predicciones")), ("real", dia.get("apuestas"))):
            for reg in lista or []:
                if not isinstance(reg, dict) or not reg.get("game_id"):
                    avisos.append(f"{fecha}: registro sin game_id ignorado")
                    continue
                for k in reg:
                    if k not in CAMPOS_MAPEADOS:
                        sin_mapear[k] = sin_mapear.get(k, 0) + 1
                eid = upsert_evento(cur, fecha, reg)
                fila = fila_prediccion(fecha, reg, kind, eid, version)
                upsert(cur, "predictions", "prediction_id", fila)
                stats["predicciones" if kind == "papel" else "apuestas"] += 1
                res = fila_resultado(fila["prediction_id"], reg)
                if res:
                    upsert(cur, "results", "prediction_id", res)
                    stats["resultados"] += 1
                else:
                    # Si antes estaba liquidado y ahora no, quitamos el resultado viejo.
                    cur.execute("DELETE FROM results WHERE prediction_id=?", (fila["prediction_id"],))

    for lec in mem.get("lecciones") or []:
        if not isinstance(lec, dict) or not lec.get("id"):
            continue
        prob = num(lec.get("probPick"))
        extra = {k: v for k, v in lec.items() if k not in CAMPOS_LECCION}
        upsert(cur, "lessons", "lesson_id", {
            "lesson_id": lec["id"], "sport": SPORT, "lesson_date": lec.get("fecha"),
            "event_id": f"{SPORT}:{lec['game_id']}" if lec.get("game_id") else None,
            "tipo": lec.get("tipo"), "patron": lec.get("patron"), "selection": lec.get("pick"),
            "model_prob": prob / 100.0 if prob is not None else None,
            "edge_pct": num(lec.get("edge")), "final_score": lec.get("marcador_final"),
            "text": lec.get("leccion"), "motivo": lec.get("motivo"),
            "confianza": num(lec.get("confianza")), "fuente": lec.get("fuente"),
            "modelo": lec.get("modelo"), "created_at": lec.get("creada_en"),
            "extra_json": jdump(extra) if extra else None,
        })
        stats["lecciones"] += 1

    # Datos sueltos del experimento (capital, día, metadatos del modelo)
    for k in ("modo", "capital", "capital_inicial", "dia_actual", "dias_totales",
              "stake_por_juego", "experimento_activo", "ml_meta", "calib_meta", "mente_stats"):
        if k in mem:
            v = mem[k]
            upsert(cur, "meta", ("sport", "key"), {
                "sport": SPORT, "key": k,
                "value": v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)})
    upsert(cur, "meta", ("sport", "key"), {"sport": SPORT, "key": "fuente_json", "value": str(ruta_json)})

    con.executescript(VISTAS)
    con.commit()
    con.close()
    return {"stats": stats, "sin_mapear": sin_mapear, "avisos": avisos}


def main():
    ap = argparse.ArgumentParser(description="Exporta memoria_auditoria.json (Quantum MLB) a SQLite.")
    ap.add_argument("--json", default=str(AQUI / "memoria_auditoria.json"),
                    help="Ruta a memoria_auditoria.json (default: junto al script)")
    ap.add_argument("--db", default=str(AQUI / "apuestas.db"),
                    help="Ruta de la base SQLite (default: apuestas.db junto al script)")
    ap.add_argument("--silencioso", action="store_true", help="No imprimir detalle de campos")
    a = ap.parse_args()

    ruta_json, ruta_db = Path(a.json), Path(a.db)
    if not ruta_json.exists():
        print(f"ERROR: no encuentro {ruta_json}")
        sys.exit(1)
    ruta_db.parent.mkdir(parents=True, exist_ok=True)

    r = exportar(ruta_json, ruta_db)
    print(f"OK -> {ruta_db}")
    print("Procesados:", r["stats"])
    con = sqlite3.connect(str(ruta_db))
    for t in ("events", "predictions", "results", "lessons", "meta"):
        print(f"  {t:12s} {con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]} filas")
    con.close()
    if not a.silencioso and r["sin_mapear"]:
        print("Campos guardados solo en extra_json (sin columna propia):")
        for k, n in sorted(r["sin_mapear"].items(), key=lambda x: -x[1]):
            print(f"  {k}: {n}")
    for av in r["avisos"][:20]:
        print("AVISO:", av)


if __name__ == "__main__":
    main()
