"""
Entrena el Random Forest con apuestas y predicciones liquidadas.
Tambi?n se ejecuta autom?ticamente tras cada liquidaci?n en servidor_mlb.py.
"""

import json
import os
from pathlib import Path

from memoria_store import abrir
from ml_predictor import auto_entrenar_ml, cargar_datos_entrenamiento_desde_memoria

MEMORIA_PATH = Path(__file__).resolve().parent / "memoria_auditoria.json"


def _cargar() -> tuple[dict, object]:
    data_dir = Path(os.environ.get("DATA_DIR", str(MEMORIA_PATH.parent)))
    store = abrir(data_dir)
    memoria = store.cargar()
    if isinstance(memoria, dict) and memoria.get("dias"):
        return memoria, store
    memoria = json.loads(MEMORIA_PATH.read_text(encoding="utf-8"))
    return memoria, store


def main():
    print("=" * 50)
    print("  ENTRENAMIENTO DEL MODELO DE MACHINE LEARNING")
    print("=" * 50)
    memoria, store = _cargar()
    datos = cargar_datos_entrenamiento_desde_memoria(memoria)
    print(f"[ENTRENAMIENTO] Muestras disponibles: {len(datos)}")
    meta = auto_entrenar_ml(memoria, min_muestras=5)
    if meta.get("ok"):
        store.guardar(memoria)
        print(f"[ENTRENAMIENTO] {meta['mensaje']}")
        print("[ENTRENAMIENTO] Guardado en la base. El JSON del repo no se toca.")
    else:
        print(f"[ENTRENAMIENTO] {meta.get('mensaje', 'Sin cambios')}")


if __name__ == "__main__":
    main()
