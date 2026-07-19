"""Sincroniza el experimento MLB con la fecha real (America/Puerto_Rico)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Puerto_Rico")


def sincronizar_dia() -> None:
    ruta_json = os.path.join(os.path.dirname(__file__), "memoria_auditoria.json")

    if not os.path.exists(ruta_json):
        print(f"[ERROR] No se encontró el archivo: {ruta_json}")
        return

    try:
        with open(ruta_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict) or "dias" not in data or not data["dias"]:
            print("[INFO] No hay días registrados para sincronizar.")
            return

        fecha_inicio = datetime.strptime(data["dias"][0]["fecha"], "%Y-%m-%d").date()
        hoy = datetime.now(TZ).date()
        dias_totales = int(data.get("dias_totales") or 200)
        nuevo_dia = min(max(1, (hoy - fecha_inicio).days + 1), dias_totales)
        fecha_objetivo = fecha_inicio + timedelta(days=nuevo_dia - 1)

        por_fecha = {d.get("fecha"): d for d in data["dias"]}
        creados = 0
        for n in range(1, nuevo_dia + 1):
            f = (fecha_inicio + timedelta(days=n - 1)).strftime("%Y-%m-%d")
            if f not in por_fecha:
                data["dias"].append(
                    {
                        "dia": n,
                        "fecha": f,
                        "bloqueado_en": None,
                        "apuestas": [],
                        "predicciones": [],
                        "resumen": {},
                    }
                )
                por_fecha[f] = data["dias"][-1]
                creados += 1

        data["dias"].sort(key=lambda x: x.get("fecha") or "")
        # Normalizar índices de día según fecha
        for d in data["dias"]:
            try:
                f = datetime.strptime(d["fecha"], "%Y-%m-%d").date()
                d["dia"] = (f - fecha_inicio).days + 1
            except Exception:
                pass

        anterior = data.get("dia_actual")
        data["dia_actual"] = nuevo_dia

        with open(ruta_json, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(
            f"[OK] Experimento sincronizado: día {anterior} → {nuevo_dia} "
            f"({fecha_objetivo}). Días nuevos creados: {creados}."
        )
    except Exception as e:
        print(f"[ERROR] Falló la sincronización autónoma: {e}")


if __name__ == "__main__":
    sincronizar_dia()
