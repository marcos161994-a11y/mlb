"""
Importar pasado para aprendizaje (plan 4).

Fusiona backups / listas de experiencias como paper RETROACTIVO:
cuenta para ML + lecciones, NO para win rate del panel.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any


def _asegurar_dia(memoria: dict, fecha: str) -> dict:
    for d in memoria.get("dias") or []:
        if isinstance(d, dict) and d.get("fecha") == fecha:
            return d
    dia = {
        "fecha": fecha,
        "dia": len(memoria.get("dias") or []) + 1,
        "predicciones": [],
        "apuestas": [],
        "resumen": {},
    }
    memoria.setdefault("dias", []).append(dia)
    return dia


def _marcar_aprendizaje(pred: dict) -> dict:
    p = copy.deepcopy(pred)
    p["retroactivo"] = True
    p["aprendizaje_solo"] = True
    p["valida_stats"] = False
    motivo = str(p.get("motivo_apuesta") or "")
    if "APRENDIZAJE" not in motivo.upper():
        p["motivo_apuesta"] = (motivo + " · import aprendizaje").strip(" ·")
    return p


def importar_dump_aprendizaje(
    memoria: dict,
    dump: dict,
    *,
    importar_apuestas: bool = False,
) -> dict[str, Any]:
    """
    Fusiona predicciones liquidadas de un dump/backup en la memoria actual.
    Marca cada pred importada como retroactivo (no cuenta en WR del panel).
    Opcionalmente copia lecciones del dump.
    """
    if not isinstance(dump, dict):
        return {"ok": False, "motivo": "dump invalido"}

    preds_in = 0
    preds_new = 0
    lec_in = 0

    by_fecha = {d.get("fecha"): d for d in (memoria.get("dias") or []) if d.get("fecha")}

    for dia_src in dump.get("dias") or []:
        if not isinstance(dia_src, dict):
            continue
        fecha = dia_src.get("fecha")
        if not fecha:
            continue
        dest = by_fecha.get(fecha)
        if dest is None:
            dest = _asegurar_dia(memoria, fecha)
            by_fecha[fecha] = dest

        existentes = {
            str(p.get("game_id") or ""): p for p in (dest.get("predicciones") or [])
        }
        for pred in dia_src.get("predicciones") or []:
            if not isinstance(pred, dict):
                continue
            if pred.get("estado") != "liquidado":
                continue
            if pred.get("resultado") not in ("acierto", "fallo"):
                continue
            preds_in += 1
            gid = str(pred.get("game_id") or "")
            if not gid or gid in existentes:
                continue
            marcado = _marcar_aprendizaje(pred)
            dest.setdefault("predicciones", []).append(marcado)
            existentes[gid] = marcado
            preds_new += 1

        if importar_apuestas and not dest.get("apuestas") and dia_src.get("apuestas"):
            # No altera capital: solo referencia histórica etiquetada
            aps = []
            for a in dia_src["apuestas"]:
                if not isinstance(a, dict):
                    continue
                aa = copy.deepcopy(a)
                aa["import_aprendizaje"] = True
                aps.append(aa)
            dest["apuestas"] = aps

    # Fusionar lecciones del dump (por id o game_id+tipo)
    from ia_lecciones import asegurar_lista_lecciones

    dest_lec = asegurar_lista_lecciones(memoria)
    keys = {
        (
            str(x.get("game_id") or ""),
            str(x.get("tipo") or x.get("patron") or ""),
        )
        for x in dest_lec
        if isinstance(x, dict)
    }
    for item in dump.get("lecciones") or []:
        if not isinstance(item, dict):
            continue
        k = (str(item.get("game_id") or ""), str(item.get("tipo") or item.get("patron") or ""))
        if k in keys and k[0]:
            continue
        copia = copy.deepcopy(item)
        copia["importada"] = True
        dest_lec.append(copia)
        keys.add(k)
        lec_in += 1

    # Reindexar días
    dias = sorted(memoria.get("dias") or [], key=lambda d: d.get("fecha") or "")
    for i, d in enumerate(dias, 1):
        d["dia"] = i
    memoria["dias"] = dias

    memoria["import_aprendizaje_meta"] = {
        "ultimo": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "preds_vistas": preds_in,
        "preds_nuevas": preds_new,
        "lecciones_importadas": lec_in,
    }
    return {
        "ok": True,
        "preds_vistas": preds_in,
        "preds_nuevas": preds_new,
        "lecciones_importadas": lec_in,
        "dias": len(dias),
    }


def importar_experiencias_lista(memoria: dict, items: list) -> dict[str, Any]:
    """
    Importa lista manual de experiencias:
    [{fecha, visitante, home, pick, resultado, probPick?, edge?, game_id?}, ...]
    """
    if not isinstance(items, list):
        return {"ok": False, "motivo": "lista invalida"}

    n = 0
    for raw in items:
        if not isinstance(raw, dict):
            continue
        fecha = str(raw.get("fecha") or "").strip()
        pick = str(raw.get("pick") or "").strip()
        resultado = str(raw.get("resultado") or "").strip().lower()
        if not fecha or not pick or resultado not in ("acierto", "fallo"):
            continue
        gid = str(raw.get("game_id") or f"imp-{fecha}-{pick}")[:80]
        dia = _asegurar_dia(memoria, fecha)
        if any(str(p.get("game_id")) == gid for p in (dia.get("predicciones") or [])):
            continue
        pred = _marcar_aprendizaje(
            {
                "game_id": gid,
                "visitante": raw.get("visitante"),
                "home": raw.get("home"),
                "pick": pick,
                "probPick": raw.get("probPick"),
                "edge": raw.get("edge"),
                "odds": raw.get("odds") or 2.0,
                "lineas_fuente": raw.get("lineas_fuente") or "import",
                "estado": "liquidado",
                "resultado": resultado,
                "marcador_final": raw.get("marcador_final"),
                "motivo_apuesta": raw.get("motivo") or "experiencia importada",
                "ml_features": raw.get("ml_features")
                if isinstance(raw.get("ml_features"), dict)
                else None,
                "ia_veto": raw.get("ia_veto")
                if isinstance(raw.get("ia_veto"), dict)
                else None,
                "profit": 0,
                "stake_virtual": float(raw.get("stake_virtual") or 0),
            }
        )
        dia.setdefault("predicciones", []).append(pred)
        n += 1

    return {"ok": True, "importadas": n}
