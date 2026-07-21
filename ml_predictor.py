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
from pathlib import Path

# Caché del modelo entrenado
_modelo_rf: Optional[RandomForestClassifier] = None
_scaler: Optional[StandardScaler] = None
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

FEATURE_COLUMNS = [
    "era_pitcher",
    "whip_pitcher",
    "k9_pitcher",
    "woba_equipo",
    "ops_equipo",
    "win_pct_equipo",
    "es_local",
    "park_factor",
    "fatiga_bullpen",
    "matchup_zurdo_diestro",
    "edge_estadistico",
    "bb9_pitcher",
    "hr9_pitcher",
    "racha_equipo",
    "diferencia_run",
    "vs_pitcher_hand",
]


def _modelo_path() -> Path:
    return DATA_DIR / "modelo_rf_mlb.pkl"


def _scaler_path() -> Path:
    return DATA_DIR / "scaler_rf_mlb.pkl"


def _features_vector(features: Dict[str, Any]) -> np.ndarray:
    return np.array([[features.get(col, 0) for col in FEATURE_COLUMNS]])


def _features_desde_registro(reg: Dict[str, Any]) -> Dict[str, Any]:
    """Aproxima features ML desde prob/edge (solo registros legacy sin ml_features)."""
    prob = float(reg.get("probPick") or 50) / 100.0
    edge = float(reg.get("edge") or 0)
    pick = reg.get("pick") or ""
    home = reg.get("home") or ""
    es_local = 1.0 if home and home in pick else 0.0
    return {
        "era_pitcher": 4.5 - edge / 200.0,
        "whip_pitcher": 1.35 - edge / 400.0,
        "k9_pitcher": 7.5 + edge / 50.0,
        "woba_equipo": 0.300 + prob * 0.05,
        "ops_equipo": 0.680 + prob * 0.06,
        "win_pct_equipo": prob,
        "es_local": es_local,
        "park_factor": 1.0,
        "fatiga_bullpen": 0.3,
        "matchup_zurdo_diestro": 0.0,
        "edge_estadistico": edge,
        "bb9_pitcher": 3.0,
        "hr9_pitcher": 1.0,
        "racha_equipo": prob,
        "diferencia_run": (prob - 0.5) * 20,
        "vs_pitcher_hand": 0.0,
    }


def serializar_features_ml(features: Dict[str, Any]) -> Dict[str, float]:
    """Normaliza el vector de features para guardar en memoria o entrenar."""
    out: Dict[str, float] = {}
    for col in FEATURE_COLUMNS:
        try:
            out[col] = float(features.get(col, 0) or 0)
        except (TypeError, ValueError):
            out[col] = 0.0
    return out


def features_entrenamiento_desde_registro(reg: Dict[str, Any]) -> Dict[str, Any]:
    """Features reales guardadas al predecir; fallback sintético en historial viejo."""
    saved = reg.get("ml_features")
    if isinstance(saved, dict) and saved:
        return serializar_features_ml(saved)
    return _features_desde_registro(reg)


def cargar_datos_entrenamiento_desde_memoria(memoria: dict) -> List[Dict[str, Any]]:
    """Apuestas y predicciones liquidadas → dataset para Random Forest."""
    datos: List[Dict[str, Any]] = []
    reales = sinteticas = 0
    for dia in memoria.get("dias", []):
        for apuesta in dia.get("apuestas", []):
            if apuesta.get("estado") not in ("ganada", "perdida"):
                continue
            fila = features_entrenamiento_desde_registro(apuesta)
            if apuesta.get("ml_features"):
                reales += 1
            else:
                sinteticas += 1
            fila["resultado"] = 1 if apuesta["estado"] == "ganada" else 0
            datos.append(fila)
        for pred in dia.get("predicciones", []):
            if pred.get("estado") != "liquidado" or pred.get("resultado") not in (
                "acierto",
                "fallo",
            ):
                continue
            fila = features_entrenamiento_desde_registro(pred)
            if pred.get("ml_features"):
                reales += 1
            else:
                sinteticas += 1
            fila["resultado"] = 1 if pred["resultado"] == "acierto" else 0
            datos.append(fila)
    if datos:
        print(
            f"[ML] Dataset entrenamiento: {reales} muestras con features reales, "
            f"{sinteticas} sintéticas (historial antiguo)"
        )
    return datos


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
    
    df = pd.DataFrame(datos_historicos)
    X = df[FEATURE_COLUMNS].fillna(0)
    y = df["resultado"]

    _scaler = StandardScaler()
    X_scaled = _scaler.fit_transform(X)

    _modelo_rf = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        min_samples_split=5,
        random_state=42,
        n_jobs=-1,
    )
    _modelo_rf.fit(X_scaled, y)

    with open(_modelo_path(), "wb") as f:
        pickle.dump(_modelo_rf, f)
    with open(_scaler_path(), "wb") as f:
        pickle.dump(_scaler, f)
    if DATA_DIR.resolve() != BASE_DIR.resolve():
        try:
            (BASE_DIR / "modelo_rf_mlb.pkl").write_bytes(_modelo_path().read_bytes())
            (BASE_DIR / "scaler_rf_mlb.pkl").write_bytes(_scaler_path().read_bytes())
        except OSError:
            pass

    acc = _modelo_rf.score(X_scaled, y)
    print(f"[ML] Modelo Random Forest entrenado con {len(datos_historicos)} muestras")
    print(f"[ML] Features usadas: {len(FEATURE_COLUMNS)}")
    print(f"[ML] Accuracy en entrenamiento: {acc:.3f}")
    return _modelo_rf


def auto_entrenar_ml(memoria: dict, min_muestras: int = 5) -> dict:
    """Reentrena el Random Forest cuando hay nuevas liquidaciones."""
    meta_prev = memoria.get("ml_meta") or {}
    datos = cargar_datos_entrenamiento_desde_memoria(memoria)
    meta: Dict[str, Any] = {
        "ok": False,
        "muestras": len(datos),
        "mensaje": "",
        "accuracy_train": meta_prev.get("accuracy_train"),
        "ultimo_entreno": meta_prev.get("ultimo_entreno"),
    }
    if len(datos) < min_muestras:
        meta["mensaje"] = f"Esperando más datos ({len(datos)}/{min_muestras} muestras)"
        return meta
    if meta_prev.get("muestras") == len(datos) and _modelo_path().exists():
        meta["ok"] = True
        meta["mensaje"] = "Modelo ya entrenado con el historial actual"
        return meta

    modelo = entrenar_modelo_rf(datos)
    if not modelo or _scaler is None:
        meta["mensaje"] = "Error al entrenar"
        return meta

    X = pd.DataFrame(datos)[FEATURE_COLUMNS].fillna(0)
    acc = float(modelo.score(_scaler.transform(X), pd.DataFrame(datos)["resultado"]))
    meta.update(
        {
            "ok": True,
            "muestras": len(datos),
            "accuracy_train": round(acc, 3),
            "ultimo_entreno": datetime.now().isoformat(),
            "mensaje": f"Reentrenado con {len(datos)} muestras (acc {acc:.1%})",
        }
    )
    memoria["ml_meta"] = meta
    print(f"[ML] Auto-entrenamiento: {meta['mensaje']}")
    return meta


def cargar_modelo_rf() -> Optional[RandomForestClassifier]:
    """Carga el modelo entrenado desde disco."""
    global _modelo_rf, _scaler

    if _modelo_rf is not None:
        return _modelo_rf

    mp, sp = _modelo_path(), _scaler_path()
    if not mp.exists() and DATA_DIR.resolve() != BASE_DIR.resolve():
        repo_m, repo_s = BASE_DIR / "modelo_rf_mlb.pkl", BASE_DIR / "scaler_rf_mlb.pkl"
        if repo_m.exists() and repo_s.exists():
            try:
                mp.write_bytes(repo_m.read_bytes())
                sp.write_bytes(repo_s.read_bytes())
            except OSError:
                pass

    if mp.exists() and sp.exists():
        try:
            with open(mp, "rb") as f:
                _modelo_rf = pickle.load(f)
            with open(sp, "rb") as f:
                _scaler = pickle.load(f)
            print("[ML] Modelo Random Forest cargado desde disco")
            return _modelo_rf
        except Exception as e:
            print(f"[ML] Error cargando modelo: {e}")

    return None


def predecir_rf(features: Dict[str, Any]) -> Optional[float]:
    """
    Predice probabilidad de victoria usando Random Forest.
    """
    global _modelo_rf, _scaler

    if _modelo_rf is None:
        cargar_modelo_rf()

    if _modelo_rf is None or _scaler is None:
        return None

    X_scaled = _scaler.transform(_features_vector(features))
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
