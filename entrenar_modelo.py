"""
Script para entrenar el modelo de Machine Learning con datos históricos.
Usa las apuestas realizadas como datos de entrenamiento.
"""

import json
from ml_predictor import entrenar_modelo_rf, cargar_modelo_rf
from modelo_mlb import stats_pitcher, stats_bateo, cargar_records
from datetime import datetime

def cargar_datos_historicos():
    """Carga datos históricos de apuestas para entrenamiento."""
    try:
        with open("memoria_auditoria.json", "r", encoding="utf-8") as f:
            memoria = json.load(f)
    except:
        print("[ENTRENAMIENTO] No se encontró memoria_auditoria.json")
        return []
    
    datos_entrenamiento = []
    season = 2026
    
    for dia in memoria.get("dias", []):
        for apuesta in dia.get("apuestas", []):
            # Solo usar apuestas liquidadas (ganadas o perdidas)
            if apuesta.get("estado") not in ["ganada", "perdida"]:
                continue
            
            # Extraer features del juego
            game_id = apuesta.get("game_id")
            pick = apuesta.get("pick", "")
            
            # Determinar si fue ganada o perdida
            resultado = 1 if apuesta.get("estado") == "ganada" else 0
            
            # Simular features (en producción esto vendría de datos reales)
            # Por ahora usamos valores aproximados basados en la apuesta
            features = {
                'era_pitcher': 4.0 + (apuesta.get("edge", 0) / 100.0),  # Aproximación
                'whip_pitcher': 1.3 + (apuesta.get("edge", 0) / 200.0),
                'k9_pitcher': 7.5 + (apuesta.get("edge", 0) / 50.0),
                'woba_equipo': 0.320 + (apuesta.get("probPick", 50) / 1000.0),
                'ops_equipo': 0.710 + (apuesta.get("probPick", 50) / 1000.0),
                'win_pct_equipo': apuesta.get("probPick", 50) / 100.0,
                'es_local': 0.5,  # Valor promedio
                'park_factor': 1.0,
                'fatiga_bullpen': 0.3,
                'matchup_zurdo_diestro': 0.0,
                'edge_estadistico': apuesta.get("edge", 0) / 100.0,
                'resultado': resultado
            }
            
            datos_entrenamiento.append(features)
    
    print(f"[ENTRENAMIENTO] Cargados {len(datos_entrenamiento)} datos históricos")
    return datos_entrenamiento

def main():
    print("=" * 50)
    print("  ENTRENAMIENTO DEL MODELO DE MACHINE LEARNING")
    print("=" * 50)
    print()
    
    # Cargar datos históricos
    datos = cargar_datos_historicos()
    
    if not datos:
        print("[ENTRENAMIENTO] No hay suficientes datos para entrenar")
        print("[ENTRENAMIENTO] El modelo se entrenará cuando haya más apuestas liquidadas")
        return
    
    # Entrenar modelo
    print("[ENTRENAMIENTO] Iniciando entrenamiento...")
    modelo = entrenar_modelo_rf(datos)
    
    if modelo:
        print()
        print("=" * 50)
        print("  ENTRENAMIENTO COMPLETADO EXITOSAMENTE")
        print("=" * 50)
        print()
        print("El modelo está listo para usar en predicciones.")
        print("El sistema usará Ensemble Learning automáticamente.")
        print()
        print("Para desactivar ML, cambia 'usar_ml' a false en config_experimento.json")
    else:
        print("[ENTRENAMIENTO] Error en el entrenamiento")

if __name__ == "__main__":
    main()
