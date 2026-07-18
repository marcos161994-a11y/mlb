"""
Entrena el Random Forest con apuestas y predicciones liquidadas.
Tambi?n se ejecuta autom?ticamente tras cada liquidaci?n en servidor_mlb.py.
"""

import json
from pathlib import Path

from ml_predictor import auto_entrenar_ml, cargar_datos_entrenamiento_desde_memoria

MEMORIA_PATH = Path(__file__).resolve().parent / "memoria_auditoria.json"


def main():
    print("=" * 50)
    print("  ENTRENAMIENTO DEL MODELO DE MACHINE LEARNING")
    print("=" * 50)
    memoria = json.loads(MEMORIA_PATH.read_text(encoding="utf-8"))
    datos = cargar_datos_entrenamiento_desde_memoria(memoria)
    print(f"[ENTRENAMIENTO] Muestras disponibles: {len(datos)}")
    meta = auto_entrenar_ml(memoria, min_muestras=5)
    if meta.get("ok"):
        MEMORIA_PATH.write_text(json.dumps(memoria, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[ENTRENAMIENTO] {meta['mensaje']}")
    else:
        print(f"[ENTRENAMIENTO] {meta.get('mensaje', 'Sin cambios')}")


if __name__ == "__main__":
    main()
