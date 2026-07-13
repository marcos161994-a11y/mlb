"""
Módulo de Machine Learning para predicciones de MLB.
Implementa Random Forest y Ensemble Learning.
"""

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from typing import Dict, Any, List, Optional
import pickle
import os
from datetime import datetime

# Caché del modelo entrenado
_modelo_rf: Optional[RandomForestClassifier] = None
_scaler: Optional[StandardScaler] = None
_modelo_path = "modelo_rf_mlb.pkl"
_scaler_path = "scaler_rf_mlb.pkl"


def entrenar_modelo_rf(datos_historicos: List[Dict[str, Any]]) -> RandomForestClassifier:
    """
    Entrena un modelo Random Forest con datos históricos.
    
    Args:
        datos_historicos: Lista de diccionarios con features y resultados
        
    Returns:
        Modelo Random Forest entrenado
    """
    global _modelo_rf, _scaler
    
    if not datos_historicos:
        print("[ML] No hay datos históricos para entrenar")
        return None
    
    # Convertir a DataFrame
    df = pd.DataFrame(datos_historicos)
    
    # Features para el modelo
    feature_columns = [
        'era_pitcher', 'whip_pitcher', 'k9_pitcher',
        'woba_equipo', 'ops_equipo', 'win_pct_equipo',
        'es_local', 'park_factor', 'fatiga_bullpen',
        'matchup_zurdo_diestro', 'edge_estadistico',
        'bb9_pitcher', 'hr9_pitcher', 'racha_equipo',
        'diferencia_run', 'vs_pitcher_hand'
    ]
    
    # Filtrar solo columnas que existen
    available_features = [col for col in feature_columns if col in df.columns]
    
    if len(available_features) < 5:
        print(f"[ML] Insuficientes features disponibles: {available_features}")
        return None
    
    X = df[available_features].fillna(0)
    y = df.get('resultado', 0)  # 1 = ganada, 0 = perdida
    
    # Escalar features
    _scaler = StandardScaler()
    X_scaled = _scaler.fit_transform(X)
    
    # Entrenar Random Forest
    _modelo_rf = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        min_samples_split=5,
        random_state=42,
        n_jobs=-1
    )
    
    _modelo_rf.fit(X_scaled, y)
    
    # Guardar modelo y scaler
    with open(_modelo_path, 'wb') as f:
        pickle.dump(_modelo_rf, f)
    with open(_scaler_path, 'wb') as f:
        pickle.dump(_scaler, f)
    
    print(f"[ML] Modelo Random Forest entrenado con {len(datos_historicos)} muestras")
    print(f"[ML] Features usadas: {available_features}")
    print(f"[ML] Accuracy en entrenamiento: {_modelo_rf.score(X_scaled, y):.3f}")
    
    return _modelo_rf


def cargar_modelo_rf() -> Optional[RandomForestClassifier]:
    """Carga el modelo entrenado desde disco."""
    global _modelo_rf, _scaler
    
    if _modelo_rf is not None:
        return _modelo_rf
    
    if os.path.exists(_modelo_path) and os.path.exists(_scaler_path):
        try:
            with open(_modelo_path, 'rb') as f:
                _modelo_rf = pickle.load(f)
            with open(_scaler_path, 'rb') as f:
                _scaler = pickle.load(f)
            print("[ML] Modelo Random Forest cargado desde disco")
            return _modelo_rf
        except Exception as e:
            print(f"[ML] Error cargando modelo: {e}")
    
    return None


def predecir_rf(features: Dict[str, Any]) -> Optional[float]:
    """
    Predice probabilidad de victoria usando Random Forest.
    
    Args:
        features: Diccionario con features del juego
        
    Returns:
        Probabilidad de victoria (0-100) o None si no hay modelo
    """
    global _modelo_rf, _scaler
    
    # Cargar modelo si no está cargado
    if _modelo_rf is None:
        cargar_modelo_rf()
    
    if _modelo_rf is None or _scaler is None:
        return None
    
    # Extraer features en el orden correcto
    feature_order = [
        'era_pitcher', 'whip_pitcher', 'k9_pitcher',
        'woba_equipo', 'ops_equipo', 'win_pct_equipo',
        'es_local', 'park_factor', 'fatiga_bullpen',
        'matchup_zurdo_diestro', 'edge_estadistico',
        'bb9_pitcher', 'hr9_pitcher', 'racha_equipo',
        'diferencia_run', 'vs_pitcher_hand'
    ]
    
    X = np.array([[features.get(col, 0) for col in feature_order]])
    X_scaled = _scaler.transform(X)
    
    # Obtener probabilidad de clase positiva (ganada)
    prob = _modelo_rf.predict_proba(X_scaled)[0, 1] * 100
    
    return round(prob, 1)


def ensemble_prediction(
    prob_estadistico: float,
    prob_ml: Optional[float],
    prob_ia: Optional[float],
    pesos: Optional[Dict[str, float]] = None
) -> float:
    """
    Combina predicciones de múltiples modelos usando Ensemble Learning.
    
    Args:
        prob_estadistico: Probabilidad del modelo estadístico
        prob_ml: Probabilidad del modelo ML (Random Forest)
        prob_ia: Probabilidad del modelo IA (Gemini)
        pesos: Pesos para cada modelo (default: estadístico=0.4, ML=0.4, IA=0.2)
        
    Returns:
        Probabilidad combinada (0-100)
    """
    if pesos is None:
        pesos = {
            'estadistico': 0.4,
            'ml': 0.4,
            'ia': 0.2
        }
    
    # Normalizar pesos
    total_peso = sum(pesos.values())
    pesos = {k: v / total_peso for k, v in pesos.items()}
    
    # Calcular ponderación
    prob_combinada = (
        prob_estadistico * pesos['estadistico'] +
        (prob_ml if prob_ml is not None else prob_estadistico) * pesos['ml'] +
        (prob_ia if prob_ia is not None else prob_estadistico) * pesos['ia']
    )
    
    return round(prob_combinada, 1)


def ajustar_pesos_ensemble(
    rendimiento_historico: Dict[str, float],
    pesos_actuales: Optional[Dict[str, float]] = None,
    factor_ajuste: float = 0.3
) -> Dict[str, float]:
    """
    Ajusta pesos del ensemble basado en rendimiento histórico con suavizado.
    
    Args:
        rendimiento_historico: Diccionario con accuracy de cada modelo
        Ej: {'estadistico': 0.52, 'ml': 0.55, 'ia': 0.50}
        pesos_actuales: Pesos actuales del ensemble (para suavizado)
        factor_ajuste: Factor de ajuste (0-1), menor = cambios más graduales
        
    Returns:
        Nuevos pesos para el ensemble
    """
    # Si no hay datos, usar pesos por defecto
    if not rendimiento_historico:
        return {'estadistico': 0.4, 'ml': 0.4, 'ia': 0.2}
    
    # Calcular pesos basados en rendimiento (mayor accuracy = mayor peso)
    total_rendimiento = sum(rendimiento_historico.values())
    pesos_objetivo = {
        modelo: rendimiento / total_rendimiento
        for modelo, rendimiento in rendimiento_historico.items()
    }
    
    # Asegurar que todos los modelos tengan peso mínimo
    peso_minimo = 0.10
    for modelo in pesos_objetivo:
        pesos_objetivo[modelo] = max(peso_minimo, pesos_objetivo[modelo])
    
    # Renormalizar pesos objetivo
    total = sum(pesos_objetivo.values())
    pesos_objetivo = {k: v / total for k, v in pesos_objetivo.items()}
    
    # Aplicar suavizado exponencial si hay pesos actuales
    if pesos_actuales:
        pesos_finales = {}
        for modelo in pesos_objetivo:
            peso_actual = pesos_actuales.get(modelo, 0.33)
            peso_obj = pesos_objetivo[modelo]
            # Suavizado: nuevo = actual * (1 - factor) + objetivo * factor
            pesos_finales[modelo] = peso_actual * (1 - factor_ajuste) + peso_obj * factor_ajuste
    else:
        pesos_finales = pesos_objetivo
    
    # Renormalizar pesos finales
    total_final = sum(pesos_finales.values())
    pesos_finales = {k: v / total_final for k, v in pesos_finales.items()}
    
    return pesos_finales


def extraer_features_ml(juego: Dict[str, Any], stats_pitcher: Dict[str, Any], 
                        stats_equipo: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extrae features para el modelo ML desde un juego.
    
    Args:
        juego: Datos del juego
        stats_pitcher: Estadísticas del pitcher
        stats_equipo: Estadísticas del equipo
        cfg: Configuración del sistema
        
    Returns:
        Diccionario con features para ML
    """
    return {
        'era_pitcher': stats_pitcher.get('era', 4.5),
        'whip_pitcher': stats_pitcher.get('whip', 1.35),
        'k9_pitcher': stats_pitcher.get('k9', 7.5),
        'woba_equipo': stats_equipo.get('woba', 0.320),
        'ops_equipo': stats_equipo.get('ops', 0.710),
        'win_pct_equipo': stats_equipo.get('win_pct', 0.500),
        'es_local': 1.0 if juego.get('es_local', False) else 0.0,
        'park_factor': juego.get('park_factor', 1.0),
        'fatiga_bullpen': juego.get('fatiga_bullpen', 0.3),
        'matchup_zurdo_diestro': juego.get('matchup_adj', 0.0),
        'edge_estadistico': juego.get('edge', 0.0),
        # Nuevas features mejoradas
        'bb9_pitcher': stats_pitcher.get('bb9', 3.0),  # Bases por bolas por 9 innings
        'hr9_pitcher': stats_pitcher.get('hr9', 1.0),  # Home runs por 9 innings
        'racha_equipo': stats_equipo.get('racha_ultimos_10', 0.5),  # Racha últimos 10 juegos
        'diferencia_run': stats_equipo.get('run_diferencial', 0),  # Diferencial de runs
        'vs_pitcher_hand': stats_equipo.get('vs_pitcher_hand', 0.0),  # Performance vs mano del pitcher
    }
