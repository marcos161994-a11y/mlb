"""Importa memoria_auditoria.json a la base sin perder historial.

Uso (una vez, con DATABASE_URL ya puesta):

    python migrar_memoria_db.py

O contra un SQLite local:

    python migrar_memoria_db.py --sqlite /tmp/memoria.sqlite

No borra el JSON. Si la base ya tiene días que el JSON no tiene, se quedan.
Si el JSON tiene días que la base perdió, se reponen. Si no hay nada nuevo,
no reescribe.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from memoria_fusion import (
    contar_historial,
    fechas_con_historial,
    fusionar_memoria,
)
from memoria_store import MemoriaStore, SqliteStore, abrir


def _ids(memoria: dict, campo: str) -> dict[str, set[tuple]]:
    out: dict[str, set[tuple]] = {}
    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        fecha = str(dia.get("fecha") or "")
        if not fecha:
            continue
        marcas: set[tuple] = set()
        for item in dia.get(campo) or []:
            if not isinstance(item, dict) or item.get("game_id") is None:
                continue
            marcas.add(
                (
                    str(item.get("game_id")),
                    str(item.get("estado") or ""),
                    str(item.get("resultado") or ""),
                    "" if item.get("profit") is None else str(item.get("profit")),
                )
            )
        out[fecha] = marcas
    return out


def _claves_lecciones(memoria: dict) -> set[str]:
    claves: set[str] = set()
    for lec in memoria.get("lecciones") or []:
        if not isinstance(lec, dict):
            continue
        clave = str(lec.get("id") or lec.get("patron") or lec.get("game_id") or "")
        if clave:
            claves.add(clave)
    return claves


def firma_historial(memoria: dict | None) -> tuple:
    memoria = memoria if isinstance(memoria, dict) else {}
    preds = tuple(
        sorted(
            (fecha, marca)
            for fecha, marcas in _ids(memoria, "predicciones").items()
            for marca in marcas
        )
    )
    apuestas = tuple(
        sorted(
            (fecha, marca)
            for fecha, marcas in _ids(memoria, "apuestas").items()
            for marca in marcas
        )
    )
    return (
        tuple(sorted(fechas_con_historial(memoria))),
        preds,
        apuestas,
        tuple(sorted(_claves_lecciones(memoria))),
    )


def problemas_perdida(antes: dict, despues: dict) -> list[str]:
    """Lista vacía si `despues` conserva fechas, jugadas y lecciones de `antes`."""
    msgs: list[str] = []
    perdidas = fechas_con_historial(antes) - fechas_con_historial(despues)
    if perdidas:
        msgs.append("fechas perdidas: " + ", ".join(sorted(perdidas)))
    for campo in ("predicciones", "apuestas"):
        ids_antes = _ids(antes, campo)
        ids_despues = _ids(despues, campo)
        for fecha, marcas in ids_antes.items():
            faltan = marcas - ids_despues.get(fecha, set())
            if faltan:
                muestra = ", ".join(sorted(m[0] for m in list(faltan)[:6]))
                msgs.append(f"{campo} {fecha} perdidas ({muestra})")
    lecciones = _claves_lecciones(antes) - _claves_lecciones(despues)
    if lecciones:
        msgs.append(f"lecciones perdidas: {len(lecciones)}")
    claves = set(antes) - set(despues)
    if claves and not (despues.get("dias") or despues.get("lecciones")):
        msgs.append("claves perdidas: " + ", ".join(sorted(claves)))
    return msgs


def combinar(actual: dict | None, incoming: dict) -> dict:
    """Une sin pisar. Si la base está vacía, el JSON entra tal cual."""
    if not isinstance(incoming, dict):
        raise ValueError("El JSON de memoria no es un objeto")
    if not isinstance(actual, dict) or not (actual.get("dias") or actual.get("lecciones")):
        return copy.deepcopy(incoming)
    if not (incoming.get("dias") or incoming.get("lecciones")):
        return copy.deepcopy(actual)
    return fusionar_memoria(actual, incoming)


def integrar_documento(store: MemoriaStore, documento: dict) -> dict[str, Any]:
    actual = store.cargar()
    fuentes = [documento]
    if isinstance(actual, dict):
        fuentes.append(actual)
    merged = combinar(actual, documento)
    problemas: list[str] = []
    for fuente in fuentes:
        problemas.extend(problemas_perdida(fuente, merged))
    # Claves de primer nivel del JSON no pueden desaparecer en un alta nueva.
    if not isinstance(actual, dict) or not actual.get("dias"):
        faltan = set(documento) - set(merged)
        if faltan:
            problemas.append("claves del JSON no copiadas: " + ", ".join(sorted(faltan)))
    if problemas:
        return {
            "ok": False,
            "escrito": False,
            "problemas": problemas,
            "capital": (actual or {}).get("capital") if isinstance(actual, dict) else None,
        }
    if isinstance(actual, dict) and firma_historial(actual) == firma_historial(merged):
        ap, pr = contar_historial(actual)
        return {
            "ok": True,
            "escrito": False,
            "problemas": [],
            "motivo": "la base ya tiene este historial",
            "capital": actual.get("capital"),
            "dia_actual": actual.get("dia_actual"),
            "fechas": len(fechas_con_historial(actual)),
            "apuestas": ap,
            "preds": pr,
            "lecciones": len(actual.get("lecciones") or []),
        }
    store.guardar(merged)
    ap, pr = contar_historial(merged)
    return {
        "ok": True,
        "escrito": True,
        "problemas": [],
        "capital": merged.get("capital"),
        "dia_actual": merged.get("dia_actual"),
        "fechas": len(fechas_con_historial(merged)),
        "apuestas": ap,
        "preds": pr,
        "lecciones": len(merged.get("lecciones") or []),
    }


def _leer_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "dias" not in data:
        raise ValueError(f"{path} no parece memoria_auditoria.json")
    return data


def sembrar_desde_archivos(
    *,
    data_dir: Path,
    json_repo: Path,
    json_legacy: Path | None = None,
) -> list[dict[str, Any]]:
    """Arranque: trae el JSON del repo (y un archivo viejo en DATA_DIR) si aportan días."""
    store = abrir(data_dir)
    informes: list[dict[str, Any]] = []
    vistos: set[Path] = set()
    for path in (json_repo, json_legacy):
        if path is None:
            continue
        try:
            resuelto = path.resolve()
        except OSError:
            resuelto = path
        if resuelto in vistos or not path.exists():
            continue
        vistos.add(resuelto)
        if store.semilla_ya_aplicada(path):
            print(f"[MIGRAR] {path.name}: semilla ya aplicada")
            informes.append({"ok": True, "escrito": False, "archivo": path.name, "motivo": "semilla ya aplicada"})
            continue
        try:
            documento = _leer_json(path)
        except Exception as exc:
            print(f"[MIGRAR] {path.name} ilegible: {exc}")
            continue
        informe = integrar_documento(store, documento)
        informe["archivo"] = path.name
        informes.append(informe)
        if informe.get("ok"):
            try:
                store.marcar_semilla(path)
            except Exception as exc:
                print(f"[MIGRAR] no se pudo marcar la semilla: {exc}")
        if informe.get("problemas"):
            print(f"[MIGRAR] NO se escribió {path.name}: {informe['problemas']}")
        elif informe.get("escrito"):
            print(
                f"[MIGRAR] {path.name} → {store.backend} "
                f"fechas={informe.get('fechas')} preds={informe.get('preds')} "
                f"apuestas={informe.get('apuestas')}"
            )
        else:
            print(f"[MIGRAR] {path.name}: {informe.get('motivo')}")
    return informes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Importa memoria_auditoria.json a Postgres o SQLite sin perder historial."
    )
    parser.add_argument(
        "--json",
        default="memoria_auditoria.json",
        help="Ruta al JSON (por defecto memoria_auditoria.json)",
    )
    parser.add_argument(
        "--sqlite",
        default="",
        help="Si se indica, escribe este SQLite e ignora DATABASE_URL",
    )
    args = parser.parse_args(argv)
    origen = Path(args.json)
    if not origen.is_file():
        print(f"No encuentro el JSON: {origen}", file=sys.stderr)
        return 1
    if args.sqlite:
        store = SqliteStore(Path(args.sqlite))
    elif os.environ.get("DATABASE_URL", "").strip():
        store = abrir(Path("."))
    else:
        print(
            "Falta DATABASE_URL. Exporta la URL de Neon/Supabase/Render "
            "o pasa --sqlite para una prueba local.",
            file=sys.stderr,
        )
        return 1
    try:
        documento = _leer_json(origen)
    except Exception as exc:
        print(f"JSON ilegible: {exc}", file=sys.stderr)
        return 1
    antes = origen.read_bytes()
    informe = integrar_documento(store, documento)
    if origen.read_bytes() != antes:
        print("ERROR: el script modificó el JSON. No debería.", file=sys.stderr)
        return 1
    if not informe.get("ok"):
        print("ERROR: la importación perdería historial:", file=sys.stderr)
        for msg in informe.get("problemas") or []:
            print(f"  - {msg}", file=sys.stderr)
        return 1
    print(
        f"OK backend={store.backend} destino={store.destino} "
        f"escrito={informe.get('escrito')} fechas={informe.get('fechas')} "
        f"preds={informe.get('preds')} apuestas={informe.get('apuestas')} "
        f"lecciones={informe.get('lecciones')} capital={informe.get('capital')}"
    )
    if informe.get("motivo"):
        print(informe["motivo"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
