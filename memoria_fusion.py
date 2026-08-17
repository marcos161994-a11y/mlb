"""Fusión de memoria_auditoria.json (stdlib only).

Une historial de un backup (repo) con el disco/vivo sin borrar días.
Usado por el servidor y por el workflow de backup en GitHub Actions.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any


def contar_historial(memoria: dict) -> tuple[int, int]:
    """(apuestas liquidadas, predicciones con resultado)."""
    apuestas = 0
    preds = 0
    for dia in memoria.get("dias") or []:
        for a in dia.get("apuestas") or []:
            if a.get("estado") in ("ganada", "perdida"):
                apuestas += 1
        for p in dia.get("predicciones") or []:
            if p.get("resultado") in ("acierto", "fallo"):
                preds += 1
    return apuestas, preds


def fechas_con_historial(memoria: dict) -> set[str]:
    out: set[str] = set()
    for dia in memoria.get("dias") or []:
        fecha = str(dia.get("fecha") or "")
        if not fecha:
            continue
        if (dia.get("predicciones") or []) or (dia.get("apuestas") or []):
            out.add(fecha)
    return out


def backup_tiene_dias_que_el_disco_perdio(bundled: dict, disk: dict) -> bool:
    """True si el JSON del repo tiene fechas con picks que el disco ya no tiene."""
    lost = fechas_con_historial(bundled) - fechas_con_historial(disk)
    return bool(lost)


def _indice_por_game_id(items: list) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        gid = str(item.get("game_id") or "")
        if gid:
            out[gid] = item
    return out


def _mejor_pred(cur: dict | None, nuevo: dict) -> dict:
    if cur is None:
        return copy.deepcopy(nuevo)
    # Preferir liquidado sobre pendiente
    if cur.get("estado") == "pendiente" and nuevo.get("estado") == "liquidado":
        return copy.deepcopy(nuevo)
    if cur.get("estado") == "liquidado" and nuevo.get("estado") != "liquidado":
        return cur
    # Preferir el que ya tiene resultado
    if not cur.get("resultado") and nuevo.get("resultado"):
        return copy.deepcopy(nuevo)
    return cur


def _mejor_apuesta(cur: dict | None, nuevo: dict) -> dict:
    if cur is None:
        return copy.deepcopy(nuevo)
    if cur.get("estado") == "pendiente" and nuevo.get("estado") in ("ganada", "perdida"):
        return copy.deepcopy(nuevo)
    return cur


def _fusionar_lecciones(base: list, extra: list) -> list:
    by: dict[str, dict] = {}
    for lec in list(base or []) + list(extra or []):
        if not isinstance(lec, dict):
            continue
        key = str(
            lec.get("id")
            or lec.get("patron")
            or lec.get("game_id")
            or json.dumps(lec, sort_keys=True, ensure_ascii=False)[:120]
        )
        by[key] = copy.deepcopy(lec)
    return list(by.values())


def fusionar_memoria(base: dict, extra: dict) -> dict:
    """Une historial base con días más nuevos de extra (p.ej. picks de hoy tras wipe)."""
    out = copy.deepcopy(base if isinstance(base, dict) else {})
    extra = extra if isinstance(extra, dict) else {}
    by_fecha: dict[str, dict] = {
        d["fecha"]: d for d in out.get("dias") or [] if isinstance(d, dict) and d.get("fecha")
    }
    for dia in extra.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        fecha = dia.get("fecha")
        if not fecha:
            continue
        if fecha not in by_fecha:
            by_fecha[fecha] = copy.deepcopy(dia)
            continue
        dest = by_fecha[fecha]
        preds = _indice_por_game_id(dest.get("predicciones") or [])
        for p in dia.get("predicciones") or []:
            if not isinstance(p, dict):
                continue
            gid = str(p.get("game_id") or "")
            if not gid:
                continue
            preds[gid] = _mejor_pred(preds.get(gid), p)
        dest["predicciones"] = list(preds.values())
        aps = _indice_por_game_id(dest.get("apuestas") or [])
        for a in dia.get("apuestas") or []:
            if not isinstance(a, dict):
                continue
            gid = str(a.get("game_id") or "")
            if not gid:
                continue
            aps[gid] = _mejor_apuesta(aps.get(gid), a)
        dest["apuestas"] = list(aps.values())
        if dia.get("bloqueado_en") and not dest.get("bloqueado_en"):
            dest["bloqueado_en"] = dia["bloqueado_en"]

    dias = sorted(by_fecha.values(), key=lambda d: str(d.get("fecha") or ""))
    for i, d in enumerate(dias, 1):
        d["dia"] = i
    out["dias"] = dias

    cap = float(out.get("capital_inicial") or extra.get("capital_inicial") or 100)
    for d in dias:
        for a in d.get("apuestas") or []:
            if a.get("estado") in ("ganada", "perdida") and a.get("profit") is not None:
                cap += float(a["profit"])
    out["capital"] = round(cap, 2)
    out["dia_actual"] = max(
        int(out.get("dia_actual") or 1),
        int(extra.get("dia_actual") or 1),
        len(dias) or 1,
    )
    out["lecciones"] = _fusionar_lecciones(out.get("lecciones") or [], extra.get("lecciones") or [])
    for k in (
        "telegram",
        "mente_stats",
        "ml_meta",
        "calib_meta",
        "ultimo_bloqueo",
        "experiencias_negativas_backfill_hecho",
        "lecciones_backfill_hecho",
    ):
        if extra.get(k) is not None:
            out[k] = copy.deepcopy(extra[k])
    if extra.get("experimento_activo") is not None:
        out["experimento_activo"] = extra["experimento_activo"]
    return out


def memoria_parece_reinicio(memoria: dict) -> bool:
    """True si parece un wipe/reinicio (día 1, banca inicial, sin historial dinero)."""
    dias = memoria.get("dias") or []
    capital = float(memoria.get("capital") or 0)
    inicial = float(memoria.get("capital_inicial") or 100)
    apuestas, preds = contar_historial(memoria)
    return (
        int(memoria.get("dia_actual") or 1) <= 1
        and abs(capital - inicial) < 0.01
        and apuestas == 0
        and len(dias) <= 2
        and preds <= 10
    )


def fusionar_archivos(path_a: Path, path_b: Path, path_out: Path) -> dict[str, Any]:
    a = json.loads(path_a.read_text(encoding="utf-8"))
    b = json.loads(path_b.read_text(encoding="utf-8"))
    merged = fusionar_memoria(a, b)
    path_out.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return merged


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print("uso: memoria_fusion.py backup.json live.json [salida.json]", file=sys.stderr)
        return 2
    src_a, src_b = Path(args[0]), Path(args[1])
    dest = Path(args[2]) if len(args) > 2 else src_a
    merged = fusionar_archivos(src_a, src_b, dest)
    ap, pr = contar_historial(merged)
    fechas = sorted(fechas_con_historial(merged))
    print(
        f"OK fusion dia={merged.get('dia_actual')} capital={merged.get('capital')} "
        f"apuestas={ap} preds={pr} fechas={','.join(fechas)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
