import json
import os
from datetime import datetime

def sincronizar_dia():
    # Ruta relativa al archivo de memoria
    ruta_json = os.path.join(os.path.dirname(__file__), "memoria_auditoria.json")
    
    if not os.path.exists(ruta_json):
        print(f"[ERROR] No se encontró el archivo: {ruta_json}")
        return

    try:
        with open(ruta_json, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not isinstance(data, dict) or "dias" not in data:
            print("[AVISO] Estructura de memoria inválida. El servidor la reparará al iniciar.")
            return

        if not data.get("dias") or len(data["dias"]) == 0:
            print("[INFO] No hay días registrados para sincronizar.")
            return

        # Fecha de inicio: Día 1 (2026-06-03)
        fecha_inicio = datetime.strptime(data["dias"][0]["fecha"], "%Y-%m-%d")
        
        # Fecha actual del sistema
        hoy = datetime.now()
        
        # Cálculo del día actual (Diferencia de días + 1)
        nuevo_dia = (hoy - fecha_inicio).days + 1
        
        if nuevo_dia > 0 and data["dia_actual"] != nuevo_dia:
            print(f"[INFO] Sincronizando experimento... Nuevo día actual: {nuevo_dia}")
            data["dia_actual"] = nuevo_dia
            with open(ruta_json, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[ERROR] Falló la sincronización autónoma: {e}")

if __name__ == "__main__":
    sincronizar_dia()