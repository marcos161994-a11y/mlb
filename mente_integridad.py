"""Auditorías de integridad para la mente de errores (stdlib only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ACCION_REGISTRAR = "registrar"
ACCION_NOTIFICAR = "notificar"
ACCION_RESTAURAR_HISTORIAL = "restaurar_historial"

PANEL_VER_MIN = "2026-08-31-mercado"


def _fechas_con_preds(memoria: dict) -> set[str]:
    out: set[str] = set()
    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        fecha = str(dia.get("fecha") or "")
        if fecha and (dia.get("predicciones") or dia.get("apuestas")):
            out.add(fecha)
    return out


def auditar_integridad_memoria(memoria: dict | None) -> list[dict[str, Any]]:
    """Duplicados, game_id mixto int/str, resumen incoherente."""
    if not isinstance(memoria, dict):
        return []
    hallazgos: list[dict[str, Any]] = []
    dup_total = 0
    mixto_total = 0
    resumen_null = 0

    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        fecha = str(dia.get("fecha") or "?")
        preds = [p for p in (dia.get("predicciones") or []) if isinstance(p, dict)]
        vistos: dict[str, list[Any]] = {}
        for p in preds:
            gid = p.get("game_id")
            if gid is None:
                continue
            key = str(gid)
            vistos.setdefault(key, []).append(gid)
        for key, ids in vistos.items():
            if len(ids) > 1:
                dup_total += len(ids) - 1
            tipos = {type(x).__name__ for x in ids}
            if len(tipos) > 1:
                mixto_total += 1
        if dia.get("resumen") is None and preds:
            resumen_null += 1

    if dup_total:
        hallazgos.append(
            {
                "codigo": "memoria_pred_duplicada",
                "severidad": "alta",
                "mensaje": (
                    f"Predicciones duplicadas por game_id ({dup_total} extra). "
                    "Revisar guardar_prediccion (str game_id)."
                )[:180],
                "acciones": [ACCION_REGISTRAR, ACCION_NOTIFICAR],
                "meta": {"duplicados": dup_total},
            }
        )
    if mixto_total:
        hallazgos.append(
            {
                "codigo": "memoria_game_id_mixto",
                "severidad": "media",
                "mensaje": (
                    f"game_id int/str mezclados en {mixto_total} día(s). "
                    "Puede fallar con_dinero y stats."
                )[:180],
                "acciones": [ACCION_REGISTRAR],
                "meta": {"dias_mixto": mixto_total},
            }
        )
    if resumen_null >= 3:
        hallazgos.append(
            {
                "codigo": "memoria_resumen_faltante",
                "severidad": "baja",
                "mensaje": f"{resumen_null} día(s) con preds pero sin resumen cacheado",
                "acciones": [ACCION_REGISTRAR],
            }
        )

    cap = float(memoria.get("capital") or 0)
    cap_ini = float(memoria.get("capital_inicial") or 100)
    if cap < 0 or cap > cap_ini * 5:
        hallazgos.append(
            {
                "codigo": "memoria_capital_raro",
                "severidad": "media",
                "mensaje": f"Capital {cap:.2f} fuera de rango vs inicial {cap_ini:.2f}",
                "acciones": [ACCION_REGISTRAR],
            }
        )
    return hallazgos


def auditar_backup_local(
    memoria_path: Path,
    backup_path: Path,
) -> list[dict[str, Any]]:
    """Compara memoria principal vs memoria_auditoria_backup.json en disco."""
    hallazgos: list[dict[str, Any]] = []
    if not backup_path.exists():
        return hallazgos
    try:
        main = json.loads(memoria_path.read_text(encoding="utf-8")) if memoria_path.exists() else {}
        backup = json.loads(backup_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [
            {
                "codigo": "backup_ilegible",
                "severidad": "alta",
                "mensaje": f"No se pudo leer memoria/backup: {e}"[:160],
                "acciones": [ACCION_REGISTRAR, ACCION_NOTIFICAR],
            }
        ]
    if not isinstance(main, dict):
        main = {}
    if not isinstance(backup, dict):
        return hallazgos

    f_main = _fechas_con_preds(main)
    f_back = _fechas_con_preds(backup)
    perdidas = sorted(f_back - f_main)
    if perdidas:
        hallazgos.append(
            {
                "codigo": "backup_tiene_dias_extra",
                "severidad": "alta",
                "mensaje": (
                    f"Backup local tiene {len(perdidas)} día(s) que el disco principal perdió: "
                    f"{', '.join(perdidas[:4])}{'…' if len(perdidas) > 4 else ''}"
                )[:180],
                "acciones": [ACCION_RESTAURAR_HISTORIAL, ACCION_REGISTRAR, ACCION_NOTIFICAR],
                "meta": {"fechas_perdidas": perdidas[:10]},
            }
        )
    return hallazgos


def verificar_panel_html(html_path: Path | None = None) -> dict[str, Any]:
    """Comprueba QuantumMLB.html servido (JS crítico para predicciones)."""
    path = html_path or (Path(__file__).resolve().parent / "QuantumMLB.html")
    out: dict[str, Any] = {
        "ok": True,
        "path": str(path.name),
        "checks": {},
        "hallazgos": [],
    }
    if not path.exists():
        out["ok"] = False
        out["hallazgos"].append(
            {
                "codigo": "panel_html_faltante",
                "severidad": "alta",
                "mensaje": "QuantumMLB.html no encontrado en el servidor",
            }
        )
        return out

    try:
        html = path.read_text(encoding="utf-8")
    except OSError as e:
        out["ok"] = False
        out["hallazgos"].append(
            {
                "codigo": "panel_html_ilegible",
                "severidad": "alta",
                "mensaje": str(e)[:160],
            }
        )
        return out

    ver = None
    for marker in ("const PANEL_VER = '", 'const PANEL_VER = "'):
        i = html.find(marker)
        if i >= 0:
            start = i + len(marker)
            end = html.find("'", start) if marker.endswith("'") else html.find('"', start)
            if end > start:
                ver = html[start:end]
                break
    out["checks"]["panel_ver"] = ver
    if not ver:
        out["ok"] = False
        out["hallazgos"].append(
            {
                "codigo": "panel_sin_version",
                "severidad": "media",
                "mensaje": "PANEL_VER no encontrado en QuantumMLB.html",
            }
        )

    fn_start = html.find("function pintarPredicciones")
    if fn_start < 0:
        out["ok"] = False
        out["hallazgos"].append(
            {
                "codigo": "panel_sin_pintar_predicciones",
                "severidad": "alta",
                "mensaje": "Función pintarPredicciones ausente en el panel",
            }
        )
    else:
        body = html[fn_start : fn_start + 9000]
        idx_decl = body.find("const diasOrden")
        idx_use = body.find("diasOrden.map")
        orden_ok = idx_decl >= 0 and idx_use >= 0 and idx_decl < idx_use
        out["checks"]["dias_orden_ok"] = orden_ok
        if not orden_ok:
            out["ok"] = False
            out["hallazgos"].append(
                {
                    "codigo": "panel_js_dias_orden",
                    "severidad": "alta",
                    "mensaje": (
                        "Bug JS: diasOrden usado antes de declararse · "
                        "predicciones en papel quedan vacías"
                    ),
                    "acciones": [ACCION_REGISTRAR, ACCION_NOTIFICAR],
                }
            )

    if "function reportarErrorPanel" not in html:
        out["checks"]["reporte_cliente"] = False
    else:
        out["checks"]["reporte_cliente"] = True

    if ver and ver < PANEL_VER_MIN:
        out["ok"] = False
        out["hallazgos"].append(
            {
                "codigo": "panel_ver_vieja",
                "severidad": "alta",
                "mensaje": f"Panel desactualizado ({ver} < {PANEL_VER_MIN})",
                "acciones": [ACCION_REGISTRAR, ACCION_NOTIFICAR],
            }
        )
    return out


def hallazgos_errores_cliente(recientes: list[dict[str, Any]], minutos: int = 120) -> list[dict[str, Any]]:
    """Convierte errores reportados por el navegador en hallazgos."""
    from datetime import datetime, timedelta

    if not recientes:
        return []
    corte = datetime.now() - timedelta(minutes=max(5, minutos))
    out: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for err in reversed(recientes):
        if not isinstance(err, dict):
            continue
        if str(err.get("codigo") or "").startswith("test_"):
            continue
        cod = str(err.get("codigo") or "panel_js")
        if cod in vistos:
            continue
        raw = err.get("hora")
        try:
            if raw and datetime.fromisoformat(str(raw).replace("Z", "")) < corte:
                continue
        except ValueError:
            pass
        vistos.add(cod)
        out.append(
            {
                "codigo": f"cliente_{cod}",
                "severidad": "alta" if cod.startswith("panel_") else "media",
                "mensaje": (
                    f"Panel ({err.get('panel_ver') or '?'}): "
                    f"{(err.get('mensaje') or '')[:120]}"
                )[:180],
                "acciones": [ACCION_REGISTRAR, ACCION_NOTIFICAR],
                "meta": {
                    "origen": err.get("origen"),
                    "panel_ver": err.get("panel_ver"),
                    "url": (err.get("url") or "")[:80],
                },
                "cooldown_min": 60,
            }
        )
        if len(out) >= 3:
            break
    return out
