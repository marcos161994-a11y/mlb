"""Arregla predicciones de ayer y liquida aciertos/fallos."""
from servidor_mlb import (
    sincronizar_experimento_a_hoy,
    rellenar_predicciones_recientes,
    liquidar_todo,
    cargar_memoria,
    resumen_predicciones_y_dinero,
)

sincronizar_experimento_a_hoy()
m = cargar_memoria()
n = rellenar_predicciones_recientes(m, dias_atras=7)
m = cargar_memoria()
liq = liquidar_todo(m)
split = resumen_predicciones_y_dinero(cargar_memoria())
print(f"Rellenadas: {n} | Liquidaciones: {liq}")
print("Predicciones:", split["predicciones"])
print("Dinero:", split["dinero"])
for d in cargar_memoria()["dias"]:
    if d.get("predicciones"):
        ac = sum(1 for p in d["predicciones"] if p.get("resultado") == "acierto")
        fa = sum(1 for p in d["predicciones"] if p.get("resultado") == "fallo")
        print(f"  {d['fecha']}: {len(d['predicciones'])} juegos, {ac} aciertos, {fa} fallos")
