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
    # Clima Open-Meteo (al final para compatibilidad con modelos viejos de 16 feats)
    "temp_f",
    "viento_mph",
    "run_env",
]


FEATURE_SCHEMA_VERSION = 2  # v2 = features reales guardadas en predicción


def _modelo_path() -> Path:
    return DATA_DIR / "modelo_rf_mlb.pkl"


def _scaler_path() -> Path:
    return DATA_DIR / "scaler_rf_mlb.pkl"


def normalizar_features(features: Dict[str, Any] | None) -> Dict[str, float]:
    """Deja solo columnas del modelo, como float."""
    feats = features or {}
    out: Dict[str, float] = {}
    for col in FEATURE_COLUMNS:
        try:
            out[col] = float(feats.get(col, 0) or 0)
        except (TypeError, ValueError):
            out[col] = 0.0
    return out


def _features_vector(features: Dict[str, Any], n_features: int | None = None) -> np.ndarray:
    cols = FEATURE_COLUMNS
    if n_features is not None and n_features > 0:
        if n_features <= len(FEATURE_COLUMNS):
            cols = FEATURE_COLUMNS[:n_features]
        else:
            vals = [features.get(col, 0) for col in FEATURE_COLUMNS]
            vals.extend([0] * (n_features - len(FEATURE_COLUMNS)))
            return np.array([vals])
    return np.array([[features.get(col, 0) for col in cols]])


def _features_sinteticas_desde_registro(reg: Dict[str, Any]) -> Dict[str, Any]:
    """Fallback legacy: aproxima features si no se guardaron las reales."""
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
        "temp_f": float((reg.get("clima") or {}).get("temp_f") or 72.0),
        "viento_mph": float((reg.get("clima") or {}).get("viento_mph") or 5.0),
        "run_env": float((reg.get("clima") or {}).get("run_env") or 0.0),
    }


def features_desde_registro(reg: Dict[str, Any]) -> tuple[Dict[str, float], str]:
    """
    Prefiere ml_features reales guardadas al predecir.
    Returns: (features, fuente) donde fuente es 'real' | 'sintetica'.
    """
    raw = reg.get("ml_features")
    if isinstance(raw, dict) and raw:
        # Debe tener al menos señales de pitcher reales (no el invento típico)
        feats = normalizar_features(raw)
        return feats, "real"
    return normalizar_features(_features_sinteticas_desde_registro(reg)), "sintetica"


# Compat nombre antiguo
def _features_desde_registro(reg: Dict[str, Any]) -> Dict[str, Any]:
    feats, _ = features_desde_registro(reg)
    return feats


def cargar_datos_entrenamiento_desde_memoria(memoria: dict) -> List[Dict[str, Any]]:
    """Apuestas y predicciones liquidadas → dataset para Random Forest.

    Si un juego tiene apuesta liquidada, no se añade también su predicción
    (evita doble muestra casi idéntica).
    Prefiere features reales; marca fuente en cada fila.
    """
    datos: List[Dict[str, Any]] = []
    for dia in memoria.get("dias", []):
        game_ids_apostados: set = set()
        for apuesta in dia.get("apuestas", []):
            if apuesta.get("estado") not in ("ganada", "perdida"):
                continue
            gid = apuesta.get("game_id")
            if gid is not None:
                game_ids_apostados.add(gid)
            fila, fuente = features_desde_registro(apuesta)
            fila["resultado"] = 1 if apuesta["estado"] == "ganada" else 0
            fila["_fuente_features"] = fuente
            datos.append(fila)
        for pred in dia.get("predicciones", []):
            if pred.get("estado") != "liquidado" or pred.get("resultado") not in (
                "acierto",
                "fallo",
            ):
                continue
            if pred.get("game_id") in game_ids_apostados:
                continue
            fila, fuente = features_desde_registro(pred)
            fila["resultado"] = 1 if pred["resultado"] == "acierto" else 0
            fila["_fuente_features"] = fuente
            datos.append(fila)
    return datos


def entrenar_modelo_rf(datos_historicos: List[Dict[str, Any]]) -> RandomForestClassifier:
    """
    Entrena Random Forest. Si hay >=20 muestras, reporta accuracy holdout (20%).
    """
    global _modelo_rf, _scaler

    if not datos_historicos:
        print("[ML] No hay datos históricos para entrenar")
        return None

    df = pd.DataFrame(datos_historicos)
    X = df[FEATURE_COLUMNS].fillna(0)
    y = df["resultado"]

    n = len(df)
    acc_holdout = None
    if n >= 20:
        from sklearn.model_selection import train_test_split

        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y if y.nunique() > 1 else None
        )
        scaler_tmp = StandardScaler()
        X_tr_s = scaler_tmp.fit_transform(X_tr)
        X_te_s = scaler_tmp.transform(X_te)
        rf_tmp = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            min_samples_split=5,
            random_state=42,
            n_jobs=-1,
        )
        rf_tmp.fit(X_tr_s, y_tr)
        acc_holdout = float(rf_tmp.score(X_te_s, y_te))
        print(f"[ML] Accuracy holdout (20%): {acc_holdout:.3f}")

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

    acc = float(_modelo_rf.score(X_scaled, y))
    n_real = sum(1 for d in datos_historicos if d.get("_fuente_features") == "real")
    print(f"[ML] Modelo Random Forest entrenado con {n} muestras ({n_real} reales)")
    print(f"[ML] Features usadas: {len(FEATURE_COLUMNS)}")
    print(f"[ML] Accuracy en entrenamiento: {acc:.3f}")
    _modelo_rf._acc_holdout = acc_holdout  # type: ignore[attr-defined]
    _modelo_rf._n_reales = n_real  # type: ignore[attr-defined]
    return _modelo_rf


def auto_entrenar_ml(memoria: dict, min_muestras: int = 5) -> dict:
    """Reentrena el Random Forest cuando hay nuevas liquidaciones."""
    meta_prev = memoria.get("ml_meta") or {}
    datos = cargar_datos_entrenamiento_desde_memoria(memoria)
    n_real = sum(1 for d in datos if d.get("_fuente_features") == "real")
    meta: Dict[str, Any] = {
        "ok": False,
        "muestras": len(datos),
        "muestras_reales": n_real,
        "schema": FEATURE_SCHEMA_VERSION,
        "mensaje": "",
        "accuracy_train": meta_prev.get("accuracy_train"),
        "accuracy_holdout": meta_prev.get("accuracy_holdout"),
        "ultimo_entreno": meta_prev.get("ultimo_entreno"),
    }
    if len(datos) < min_muestras:
        meta["mensaje"] = f"Esperando más datos ({len(datos)}/{min_muestras} muestras)"
        return meta

    mismo_historial = (
        meta_prev.get("muestras") == len(datos)
        and meta_prev.get("schema") == FEATURE_SCHEMA_VERSION
        and meta_prev.get("muestras_reales") == n_real
        and _modelo_path().exists()
    )
    if mismo_historial:
        meta["ok"] = True
        meta["mensaje"] = "Modelo ya entrenado con el historial actual"
        return meta

    modelo = entrenar_modelo_rf(datos)
    if not modelo or _scaler is None:
        meta["mensaje"] = "Error al entrenar"
        return meta

    X = pd.DataFrame(datos)[FEATURE_COLUMNS].fillna(0)
    acc = float(modelo.score(_scaler.transform(X), pd.DataFrame(datos)["resultado"]))
    acc_h = getattr(modelo, "_acc_holdout", None)
    meta.update(
        {
            "ok": True,
            "muestras": len(datos),
            "muestras_reales": n_real,
            "schema": FEATURE_SCHEMA_VERSION,
            "accuracy_train": round(acc, 3),
            "accuracy_holdout": round(acc_h, 3) if acc_h is not None else None,
            "ultimo_entreno": datetime.now().isoformat(),
            "mensaje": (
                f"Reentrenado con {len(datos)} muestras "
                f"({n_real} reales, acc {acc:.1%}"
                + (f", holdout {acc_h:.1%}" if acc_h is not None else "")
                + ")"
            ),
        }
    )
    memoria["ml_meta"] = meta
    print(f"[ML] Auto-entrenamiento: {meta['mensaje']}")
    return meta


def empaquetar_features_del_pick(
    pick: str,
    visitante: str,
    home: str,
    features_away: Dict[str, Any] | None,
    features_home: Dict[str, Any] | None,
    edge: float | None = None,
) -> Dict[str, float] | None:
    """Elige features del lado apostado y fija edge_estadistico."""
    if not features_away or not features_home:
        return None
    p = pick or ""
    if home and home in p:
        feats = dict(features_home)
    elif visitante and visitante in p:
        feats = dict(features_away)
    else:
        feats = dict(features_home)
    if edge is not None:
        try:
            feats["edge_estadistico"] = float(edge)
        except (TypeError, ValueError):
            pass
    return normalizar_features(feats)


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

    try:
        n_exp = int(getattr(_scaler, "n_features_in_", len(FEATURE_COLUMNS)))
        X_scaled = _scaler.transform(_features_vector(features, n_exp))
        prob = _modelo_rf.predict_proba(X_scaled)[0, 1] * 100
        return round(prob, 1)
    except Exception as e:
        print(f"[ML] Error prediciendo RF (¿features nuevas?): {e}")
        return None


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
        'temp_f': juego.get('temp_f', 72.0),
        'viento_mph': juego.get('viento_mph', 5.0),
        'run_env': juego.get('run_env', 0.0),
    }
