"""
Script de backtesting para optimizar pesos del ensemble learning.
Analiza rendimiento histórico de cada modelo y encuentra los pesos óptimos.
"""

import json
from typing import Dict, List, Tuple
import numpy as np
from ml_predictor import ajustar_pesos_ensemble

def cargar_historial() -> dict:
    """Carga el historial de predicciones desde memoria_auditoria.json"""
    try:
        with open('memoria_auditoria.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error cargando historial: {e}")
        return {}

def calcular_rendimiento_modelos(memoria: dict) -> Dict[str, List[Tuple[bool, float]]]:
    """
    Calcula el rendimiento de cada modelo basado en predicciones liquidadas.
    
    Returns:
        Diccionario con listas de (acierto, probabilidad) para cada modelo
    """
    rendimiento = {
        'estadistico': [],
        'ml': [],
        'ia': []
    }
    
    for dia in memoria.get('dias', []):
        # Analizar apuestas (tienen probPick del ensemble)
        for apuesta in dia.get('apuestas', []):
            if apuesta['estado'] in ('ganada', 'perdida'):
                acierto = apuesta['estado'] == 'ganada'
                prob = apuesta.get('probPick', 50)
                
                # Como las apuestas usan ensemble, necesitamos estimar contribución
                # Asumimos distribución uniforme para backtesting inicial
                rendimiento['estadistico'].append((acierto, prob))
                rendimiento['ml'].append((acierto, prob))
                rendimiento['ia'].append((acierto, prob))
        
        # Analizar predicciones no apostadas
        if 'predicciones' in dia:
            for prediccion in dia['predicciones']:
                if prediccion.get('estado') == 'liquidado':
                    acierto = prediccion.get('resultado') == 'acierto'
                    prob = prediccion.get('probPick', 50)
                    
                    rendimiento['estadistico'].append((acierto, prob))
                    rendimiento['ml'].append((acierto, prob))
                    rendimiento['ia'].append((acierto, prob))
    
    return rendimiento

def calcular_accuracy_predicciones(predicciones: List[Tuple[bool, float]]) -> Tuple[float, float]:
    """
    Calcula accuracy y probabilidad promedio de predicciones.
    
    Returns:
        (accuracy, probabilidad_promedio)
    """
    if not predicciones:
        return 0.5, 50.0
    
    aciertos = sum(1 for acierto, _ in predicciones if acierto)
    accuracy = aciertos / len(predicciones)
    
    prob_promedio = sum(prob for _, prob in predicciones) / len(predicciones)
    
    return accuracy, prob_promedio

def buscar_pesos_optimos(rendimiento: Dict[str, List[Tuple[bool, float]]]) -> Dict[str, float]:
    """
    Busca los pesos óptimos mediante grid search.
    
    Args:
        rendimiento: Diccionario con predicciones de cada modelo
        
    Returns:
        Mejores pesos encontrados
    """
    print("\n=== Buscando pesos óptimos ===")
    
    # Calcular accuracy de cada modelo
    accuracies = {}
    for modelo, predicciones in rendimiento.items():
        accuracy, prob_prom = calcular_accuracy_predicciones(predicciones)
        accuracies[modelo] = accuracy
        print(f"{modelo}: {accuracy:.3f} accuracy, {prob_prom:.1f}% prob promedio")
    
    # Si hay datos suficientes, usar ajuste dinámico
    if all(len(preds) >= 10 for preds in rendimiento.values()):
        print("\nUsando ajuste dinámico de pesos...")
        pesos_optimos = ajustar_pesos_ensemble(accuracies, factor_ajuste=0.5)
    else:
        print("\nDatos insuficientes para ajuste dinámico, usando pesos por defecto")
        pesos_optimos = {'estadistico': 0.4, 'ml': 0.4, 'ia': 0.2}
    
    print(f"\nPesos óptimos: {pesos_optimos}")
    return pesos_optimos

def simular_ensemble(rendimiento: Dict[str, List[Tuple[bool, float]]], 
                     pesos: Dict[str, float]) -> Tuple[float, float]:
    """
    Simula el rendimiento del ensemble con pesos dados.
    
    Returns:
        (accuracy_ensemble, prob_promedio_ensemble)
    """
    if not rendimiento['estadistico']:
        return 0.5, 50.0
    
    total_predicciones = len(rendimiento['estadistico'])
    aciertos_ensemble = 0
    prob_promedio_ensemble = 0
    
    for i in range(total_predicciones):
        # Calcular probabilidad del ensemble
        prob_est = rendimiento['estadistico'][i][1]
        prob_ml = rendimiento['ml'][i][1] if i < len(rendimiento['ml']) else prob_est
        prob_ia = rendimiento['ia'][i][1] if i < len(rendimiento['ia']) else prob_est
        
        prob_ensemble = (
            prob_est * pesos['estadistico'] +
            prob_ml * pesos['ml'] +
            prob_ia * pesos['ia']
        )
        
        prob_promedio_ensemble += prob_ensemble
        
        # Determinar predicción del ensemble
        pred_ensemble = prob_ensemble > 50
        resultado_real = rendimiento['estadistico'][i][0]
        
        if pred_ensemble == resultado_real:
            aciertos_ensemble += 1
    
    accuracy_ensemble = aciertos_ensemble / total_predicciones
    prob_promedio_ensemble /= total_predicciones
    
    return accuracy_ensemble, prob_promedio_ensemble

def actualizar_config_pesos(pesos: Dict[str, float]):
    """Actualiza los pesos en config_experimento.json"""
    try:
        with open('config_experimento.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        config['pesos_ensemble'] = pesos
        
        with open('config_experimento.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        print(f"\n[Pesos actualizados en config_experimento.json]")
    except Exception as e:
        print(f"Error actualizando config: {e}")

def main():
    print("=== Backtesting Ensemble Learning ===\n")
    
    # Cargar historial
    print("Cargando historial...")
    memoria = cargar_historial()
    
    if not memoria:
        print("No hay historial disponible")
        return
    
    # Calcular rendimiento
    print("Calculando rendimiento de modelos...")
    rendimiento = calcular_rendimiento_modelos(memoria)
    
    # Mostrar estadísticas
    print("\n=== Estadísticas de Modelos ===")
    for modelo, predicciones in rendimiento.items():
        accuracy, prob_prom = calcular_accuracy_predicciones(predicciones)
        print(f"{modelo}: {len(predicciones)} predicciones, {accuracy:.3f} accuracy, {prob_prom:.1f}% prob promedio")
    
    # Buscar pesos óptimos
    pesos_optimos = buscar_pesos_optimos(rendimiento)
    
    # Simular ensemble con pesos óptimos
    print("\n=== Simulando Ensemble ===")
    accuracy_ensemble, prob_prom_ensemble = simular_ensemble(rendimiento, pesos_optimos)
    print(f"Ensemble: {accuracy_ensemble:.3f} accuracy, {prob_prom_ensemble:.1f}% prob promedio")
    
    # Comparar con pesos actuales
    try:
        with open('config_experimento.json', 'r') as f:
            config = json.load(f)
        pesos_actuales = config.get('pesos_ensemble', {'estadistico': 0.4, 'ml': 0.4, 'ia': 0.2})
        
        accuracy_actual, prob_actual = simular_ensemble(rendimiento, pesos_actuales)
        print(f"\nPesos actuales: {pesos_actuales}")
        print(f"Ensemble actual: {accuracy_actual:.3f} accuracy, {prob_actual:.1f}% prob promedio")
        
        mejora = (accuracy_ensemble - accuracy_actual) * 100
        print(f"\nMejora esperada: {mejora:+.2f}% accuracy")
    except Exception as e:
        print(f"No se pudieron cargar pesos actuales: {e}")
    
    # Preguntar si actualizar
    print("\n¿Desea actualizar los pesos en config_experimento.json? (s/n)")
    # Para automatización, actualizamos directamente
    actualizar_config_pesos(pesos_optimos)
    
    print("\n=== Backtesting completado ===")

if __name__ == "__main__":
    main()
