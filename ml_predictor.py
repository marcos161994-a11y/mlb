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
_modelo_xgb = None  # XGBClassifier | None

try:
    from xgboost import XGBClassifier

    HAS_XGB = True
except ImportError:
    XGBClassifier = None  # type: ignore
    HAS_XGB = False

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))

# Defaults ML / aprendizaje (sobreescritos por config_experimento.json → aprendizaje.*)
ML_MIN_MUESTRAS_REALES = 10
ML_MIN_RATIO_FEATURES_REALES = 0.5
ML_PESO_FEATURES_SINTETICAS = 0.05


def _cfg_ml_aprendizaje() -> Dict[str, Any]:
    """Lee tuning ML desde config sin importar servidor_mlb (evita ciclo)."""
    try:
        import json

        path = BASE_DIR / "config_experimento.json"
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            ap = cfg.get("aprendizaje")
            if isinstance(ap, dict):
                return ap
    except Exception:
        pass
    return {}


def _param_ml_aprendizaje() -> tuple[int, float, float]:
    ap = _cfg_ml_aprendizaje()
    min_reales = int(ap.get("ml_min_muestras_reales", ML_MIN_MUESTRAS_REALES))
    min_ratio = float(ap.get("ml_min_ratio_features_reales", ML_MIN_RATIO_FEATURES_REALES))
    peso_sint = float(ap.get("ml_peso_features_sinteticas", ML_PESO_FEATURES_SINTETICAS))
    return min_reales, min_ratio, peso_sint


def _ml_n_jobs() -> int:
    """Render free tier: 1 hilo evita picos de RAM en entrenamiento."""
    v = os.environ.get("ML_N_JOBS")
    if v is not None and v.strip() != "":
        return int(v)
    if os.environ.get("RENDER"):
        return 1
    return -1
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
    # Clima Open-Meteo
    "temp_f",
    "viento_mph",
    "run_env",
    # Pitcher avanzado (FIP / xFIP / K% / BB%)
    "fip_pitcher",
    "xfip_pitcher",
    "k_pct_pitcher",
    "bb_pct_pitcher",
    # Factores humanos (viaje / descanso / serie / umpire)
    "fatiga_viaje",
    "dias_descanso",
    "cambio_zona",
    "leverage_serie",
    "umpire_runs",
]


FEATURE_SCHEMA_VERSION = 4  # v4 = + factores humanos


def _modelo_path() -> Path:
    return DATA_DIR / "modelo_rf_mlb.pkl"


def _scaler_path() -> Path:
    return DATA_DIR / "scaler_rf_mlb.pkl"


def _modelo_xgb_path() -> Path:
    return DATA_DIR / "modelo_xgb_mlb.pkl"


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


def _features_frame(features: Dict[str, Any], n_features: int | None = None) -> pd.DataFrame:
    """DataFrame 1×N con nombres de columna (evita warning de StandardScaler)."""
    cols = list(FEATURE_COLUMNS)
    if n_features is not None and n_features > 0:
        if n_features <= len(FEATURE_COLUMNS):
            cols = FEATURE_COLUMNS[:n_features]
        else:
            # Modelo antiguo con más columnas de las actuales: rellenar con 0
            row = {col: float(features.get(col, 0) or 0) for col in FEATURE_COLUMNS}
            for i in range(n_features - len(FEATURE_COLUMNS)):
                row[f"_pad_{i}"] = 0.0
            return pd.DataFrame([row])
    row = {col: float(features.get(col, 0) or 0) for col in cols}
    return pd.DataFrame([row], columns=cols)


def _features_vector(features: Dict[str, Any], n_features: int | None = None) -> np.ndarray:
    """Compat: vector numpy; preferir `_features_frame` al transformar con scaler."""
    return _features_frame(features, n_features).to_numpy(dtype=float)


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
        "fip_pitcher": 4.5 - edge / 180.0,
        "xfip_pitcher": 4.5 - edge / 200.0,
        "k_pct_pitcher": 20.0 + edge / 10.0,
        "bb_pct_pitcher": 8.0 - edge / 40.0,
        "fatiga_viaje": float((reg.get("factores_humanos") or {}).get("features_away", {}).get("fatiga_viaje") or 0.0),
        "dias_descanso": 1.0,
        "cambio_zona": 0.0,
        "leverage_serie": float(((reg.get("factores_humanos") or {}).get("serie") or {}).get("leverage") or 0.0),
        "umpire_runs": float((reg.get("factores_humanos") or {}).get("sesgo_umpire_runs") or 0.0),
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
    """Apuestas y predicciones liquidadas → dataset para ML (con pesos de muestra)."""
    from aprendizaje_mlb import peso_muestra_aprendizaje

    _, _, peso_sintetica = _param_ml_aprendizaje()
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
            fila["_peso"] = peso_muestra_aprendizaje(apuesta)
            if fuente == "sintetica" and peso_sintetica <= 0:
                continue
            if fuente == "sintetica":
                fila["_peso"] = float(fila["_peso"]) * peso_sintetica
            if fila["_peso"] > 0:
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
            fila["_peso"] = peso_muestra_aprendizaje(pred)
            if fuente == "sintetica" and peso_sintetica <= 0:
                continue
            if fuente == "sintetica":
                fila["_peso"] = float(fila["_peso"]) * peso_sintetica
            if fila["_peso"] > 0:
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
    sample_weight = df["_peso"].fillna(1.0).astype(float) if "_peso" in df.columns else None

    n = len(df)
    acc_holdout = None
    if n >= 20:
        from sklearn.model_selection import train_test_split

        sw = sample_weight
        if sw is not None:
            X_tr, X_te, y_tr, y_te, sw_tr, _sw_te = train_test_split(
                X, y, sw, test_size=0.2, random_state=42, stratify=y if y.nunique() > 1 else None
            )
        else:
            X_tr, X_te, y_tr, y_te = train_test_split(
                X, y, test_size=0.2, random_state=42, stratify=y if y.nunique() > 1 else None
            )
            sw_tr = None
        scaler_tmp = StandardScaler()
        X_tr_s = scaler_tmp.fit_transform(X_tr)
        X_te_s = scaler_tmp.transform(X_te)
        rf_tmp = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            min_samples_split=5,
            random_state=42,
            n_jobs=_ml_n_jobs(),
        )
        rf_tmp.fit(X_tr_s, y_tr, sample_weight=sw_tr.to_numpy() if sw_tr is not None else None)
        acc_holdout = float(rf_tmp.score(X_te_s, y_te))
        print(f"[ML] Accuracy holdout (20%): {acc_holdout:.3f}")

    _scaler = StandardScaler()
    X_scaled = _scaler.fit_transform(X)

    _modelo_rf = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        min_samples_split=5,
        random_state=42,
        n_jobs=_ml_n_jobs(),
    )
    _modelo_rf.fit(X_scaled, y, sample_weight=sample_weight.to_numpy() if sample_weight is not None else None)

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


def entrenar_modelo_xgb(datos_historicos: List[Dict[str, Any]]) -> Any:
    """Entrena XGBoost con el mismo scaler/features que el RF."""
    global _modelo_xgb, _scaler

    if not HAS_XGB or XGBClassifier is None:
        print("[ML] XGBoost no instalado — se omite")
        return None
    if not datos_historicos:
        return None
    if _scaler is None:
        print("[ML] Scaler ausente; entrena RF antes que XGB")
        return None

    df = pd.DataFrame(datos_historicos)
    X = df[FEATURE_COLUMNS].fillna(0)
    y = df["resultado"]
    sample_weight = df["_peso"].fillna(1.0).astype(float) if "_peso" in df.columns else None
    X_scaled = _scaler.transform(X)

    _modelo_xgb = XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.08,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.5,
        min_child_weight=2,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        n_jobs=_ml_n_jobs(),
    )
    _modelo_xgb.fit(X_scaled, y, sample_weight=sample_weight.to_numpy() if sample_weight is not None else None)

    acc_h = None
    if len(df) >= 20 and y.nunique() > 1:
        from sklearn.model_selection import train_test_split

        X_tr, X_te, y_tr, y_te = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )
        xgb_tmp = XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.08,
            subsample=0.9,
            colsample_bytree=0.9,
            reg_lambda=1.5,
            min_child_weight=2,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
            n_jobs=_ml_n_jobs(),
        )
        xgb_tmp.fit(_scaler.transform(X_tr), y_tr)
        acc_h = float(xgb_tmp.score(_scaler.transform(X_te), y_te))
        print(f"[ML] XGB holdout (20%): {acc_h:.3f}")

    with open(_modelo_xgb_path(), "wb") as f:
        pickle.dump(_modelo_xgb, f)
    if DATA_DIR.resolve() != BASE_DIR.resolve():
        try:
            (BASE_DIR / "modelo_xgb_mlb.pkl").write_bytes(_modelo_xgb_path().read_bytes())
        except OSError:
            pass

    acc = float(_modelo_xgb.score(X_scaled, y))
    print(f"[ML] XGBoost entrenado con {len(df)} muestras (acc {acc:.3f})")
    _modelo_xgb._acc_holdout = acc_h  # type: ignore[attr-defined]
    return _modelo_xgb


def auto_entrenar_ml(memoria: dict, min_muestras: int = 5) -> dict:
    """Reentrena RF (+ XGBoost si está disponible) tras liquidaciones."""
    global _modelo_xgb
    meta_prev = memoria.get("ml_meta") or {}
    datos = cargar_datos_entrenamiento_desde_memoria(memoria)
    n_real = sum(1 for d in datos if d.get("_fuente_features") == "real")
    n_sint = len(datos) - n_real
    ratio_real = round(n_real / len(datos), 3) if datos else 0.0
    min_reales, min_ratio, _peso_sint = _param_ml_aprendizaje()
    meta: Dict[str, Any] = {
        "ok": False,
        "muestras": len(datos),
        "muestras_reales": n_real,
        "muestras_sinteticas": n_sint,
        "ratio_features_reales": ratio_real,
        "entreno_bloqueado": False,
        "schema": FEATURE_SCHEMA_VERSION,
        "mensaje": "",
        "accuracy_train": meta_prev.get("accuracy_train"),
        "accuracy_holdout": meta_prev.get("accuracy_holdout"),
        "accuracy_xgb_train": meta_prev.get("accuracy_xgb_train"),
        "accuracy_xgb_holdout": meta_prev.get("accuracy_xgb_holdout"),
        "ultimo_entreno": meta_prev.get("ultimo_entreno"),
        "xgb": HAS_XGB,
    }
    if len(datos) < min_muestras:
        meta["mensaje"] = f"Esperando más datos ({len(datos)}/{min_muestras} muestras)"
        return meta

    ratio_ok = len(datos) == 0 or (n_real / len(datos)) >= min_ratio
    if n_real < min_reales or not ratio_ok:
        modelo_existe = _modelo_path().exists()
        meta["entreno_bloqueado"] = True
        meta["ok"] = bool(meta_prev.get("ok")) and modelo_existe
        meta["mensaje"] = (
            f"Entreno pausado: {n_real}/{len(datos)} features reales "
            f"(mín {min_reales}, ratio {min_ratio:.0%}) — se mantiene modelo anterior"
        )
        memoria["ml_meta"] = meta
        print(f"[ML] {meta['mensaje']}")
        return meta

    xgb_listo = (not HAS_XGB) or _modelo_xgb_path().exists()
    mismo_historial = (
        meta_prev.get("muestras") == len(datos)
        and meta_prev.get("schema") == FEATURE_SCHEMA_VERSION
        and meta_prev.get("muestras_reales") == n_real
        and meta_prev.get("muestras_sinteticas") == n_sint
        and meta_prev.get("xgb") == HAS_XGB
        and _modelo_path().exists()
        and xgb_listo
    )
    if mismo_historial:
        meta["ok"] = True
        meta["mensaje"] = "Modelo ya entrenado con el historial actual"
        return meta

    modelo = entrenar_modelo_rf(datos)
    if not modelo or _scaler is None:
        meta["mensaje"] = "Error al entrenar"
        return meta

    acc_xgb = None
    acc_xgb_h = None
    if HAS_XGB:
        xgb = entrenar_modelo_xgb(datos)
        if xgb is not None:
            X = pd.DataFrame(datos)[FEATURE_COLUMNS].fillna(0)
            acc_xgb = float(xgb.score(_scaler.transform(X), pd.DataFrame(datos)["resultado"]))
            acc_xgb_h = getattr(xgb, "_acc_holdout", None)

    X = pd.DataFrame(datos)[FEATURE_COLUMNS].fillna(0)
    acc = float(modelo.score(_scaler.transform(X), pd.DataFrame(datos)["resultado"]))
    acc_h = getattr(modelo, "_acc_holdout", None)
    partes = [
        f"Reentrenado con {len(datos)} muestras ({n_real} reales",
        f"RF acc {acc:.1%}",
    ]
    if acc_h is not None:
        partes.append(f"RF holdout {acc_h:.1%}")
    if acc_xgb is not None:
        partes.append(f"XGB acc {acc_xgb:.1%}")
    if acc_xgb_h is not None:
        partes.append(f"XGB holdout {acc_xgb_h:.1%}")
    mensaje = ", ".join(partes) + ")"
    meta.update(
        {
            "ok": True,
            "muestras": len(datos),
            "muestras_reales": n_real,
            "muestras_sinteticas": n_sint,
            "ratio_features_reales": ratio_real,
            "entreno_bloqueado": False,
            "schema": FEATURE_SCHEMA_VERSION,
            "accuracy_train": round(acc, 3),
            "accuracy_holdout": round(acc_h, 3) if acc_h is not None else None,
            "accuracy_xgb_train": round(acc_xgb, 3) if acc_xgb is not None else None,
            "accuracy_xgb_holdout": round(acc_xgb_h, 3) if acc_xgb_h is not None else None,
            "ultimo_entreno": datetime.now().isoformat(),
            "xgb": HAS_XGB and _modelo_xgb is not None,
            "mensaje": mensaje,
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


def cargar_modelo_xgb() -> Any:
    """Carga XGBoost desde disco."""
    global _modelo_xgb, _scaler
    if not HAS_XGB:
        return None
    if _modelo_xgb is not None:
        return _modelo_xgb
    if _scaler is None:
        cargar_modelo_rf()
    path = _modelo_xgb_path()
    if not path.exists() and DATA_DIR.resolve() != BASE_DIR.resolve():
        alt = BASE_DIR / "modelo_xgb_mlb.pkl"
        if alt.exists():
            try:
                path.write_bytes(alt.read_bytes())
            except OSError:
                pass
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            _modelo_xgb = pickle.load(f)
        print("[ML] Modelo XGBoost cargado desde disco")
        return _modelo_xgb
    except Exception as e:
        print(f"[ML] Error cargando XGB: {e}")
        return None


def predecir_rf(features: Dict[str, Any]) -> Optional[float]:
    """Predice probabilidad de victoria usando Random Forest."""
    global _modelo_rf, _scaler

    if _modelo_rf is None:
        cargar_modelo_rf()

    if _modelo_rf is None or _scaler is None:
        return None

    try:
        n_exp = int(getattr(_scaler, "n_features_in_", len(FEATURE_COLUMNS)))
        X = _features_frame(features, n_exp)
        # Si el scaler se entrenó sin nombres, transformar por valores
        try:
            X_scaled = _scaler.transform(X)
        except ValueError:
            X_scaled = _scaler.transform(X.to_numpy(dtype=float))
        prob = _modelo_rf.predict_proba(X_scaled)[0, 1] * 100
        return round(prob, 1)
    except Exception as e:
        print(f"[ML] Error prediciendo RF (¿features nuevas?): {e}")
        return None


def predecir_xgb(features: Dict[str, Any]) -> Optional[float]:
    """Predice probabilidad con XGBoost (0-100)."""
    global _modelo_xgb, _scaler
    if not HAS_XGB:
        return None
    if _modelo_xgb is None:
        cargar_modelo_xgb()
    if _modelo_xgb is None or _scaler is None:
        return None
    try:
        n_exp = int(getattr(_scaler, "n_features_in_", len(FEATURE_COLUMNS)))
        X = _features_frame(features, n_exp)
        try:
            X_scaled = _scaler.transform(X)
        except ValueError:
            X_scaled = _scaler.transform(X.to_numpy(dtype=float))
        prob = float(_modelo_xgb.predict_proba(X_scaled)[0, 1]) * 100.0
        return round(prob, 1)
    except Exception as e:
        print(f"[ML] Error prediciendo XGB: {e}")
        return None


def _resolver_pesos_ensemble(
    pesos: Optional[Dict[str, float]],
    *,
    has_rf: bool,
    has_xgb: bool,
) -> Dict[str, float]:
    """Normaliza pesos; convierte `ml` legacy → rf/xgb."""
    p = dict(pesos or {"estadistico": 0.4, "ml": 0.4, "ia": 0.2})
    if "ml" in p and "rf" not in p and "xgb" not in p:
        ml = float(p.pop("ml"))
        if has_rf and has_xgb:
            p["rf"] = ml * 0.4
            p["xgb"] = ml * 0.6
        elif has_xgb:
            p["xgb"] = ml
        else:
            p["rf"] = ml
    p.setdefault("estadistico", 0.4)
    p.setdefault("ia", 0.0)
    p.setdefault("rf", 0.0)
    p.setdefault("xgb", 0.0)
    if not has_rf:
        p["rf"] = 0.0
    if not has_xgb:
        p["xgb"] = 0.0
    total = sum(float(v) for v in p.values())
    if total <= 0:
        return {"estadistico": 1.0, "rf": 0.0, "xgb": 0.0, "ia": 0.0}
    return {k: float(v) / total for k, v in p.items()}


def ensemble_prediction(
    prob_estadistico: float,
    prob_ml: Optional[float] = None,
    prob_ia: Optional[float] = None,
    pesos: Optional[Dict[str, float]] = None,
    *,
    prob_rf: Optional[float] = None,
    prob_xgb: Optional[float] = None,
) -> float:
    """
    Combina estadístico + RF + XGBoost (+ IA opcional).
    `prob_ml` se trata como RF por compatibilidad.
    """
    rf = prob_rf if prob_rf is not None else prob_ml
    xgb = prob_xgb
    # Resolver con todos los brazos posibles del config; los ausentes se reasignan luego
    pesos_n = _resolver_pesos_ensemble(
        pesos,
        has_rf=True,
        has_xgb=True,
    )

    est = float(prob_estadistico)
    peso_rf = float(pesos_n.get("rf", 0)) if rf is not None else 0.0
    peso_xgb = float(pesos_n.get("xgb", 0)) if xgb is not None else 0.0
    peso_ia = float(pesos_n.get("ia", 0)) if prob_ia is not None else 0.0
    peso_est = float(pesos_n.get("estadistico", 0))
    # Pesos de brazos ausentes → estadístico
    peso_est += float(pesos_n.get("rf", 0)) - peso_rf
    peso_est += float(pesos_n.get("xgb", 0)) - peso_xgb
    peso_est += float(pesos_n.get("ia", 0)) - peso_ia

    total = peso_est + peso_rf + peso_xgb + peso_ia
    if total <= 0:
        return round(est, 1)
    combinada = (
        est * peso_est
        + (float(rf) if rf is not None else 0.0) * peso_rf
        + (float(xgb) if xgb is not None else 0.0) * peso_xgb
        + (float(prob_ia) if prob_ia is not None else 0.0) * peso_ia
    ) / total
    return round(combinada, 1)


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
        'fip_pitcher': stats_pitcher.get('fip', stats_pitcher.get('era', 4.5)),
        'xfip_pitcher': stats_pitcher.get('xfip', stats_pitcher.get('fip', stats_pitcher.get('era', 4.5))),
        'k_pct_pitcher': stats_pitcher.get('k_pct', float(stats_pitcher.get('k9', 7.5)) * 2.4),
        'bb_pct_pitcher': stats_pitcher.get('bb_pct', float(stats_pitcher.get('bb9', 3.0)) * 2.5),
        'fatiga_viaje': float(juego.get('fatiga_viaje', 0.0) or 0.0),
        'dias_descanso': float(juego.get('dias_descanso', 1.0) or 1.0),
        'cambio_zona': float(juego.get('cambio_zona', 0.0) or 0.0),
        'leverage_serie': float(juego.get('leverage_serie', 0.0) or 0.0),
        'umpire_runs': float(juego.get('umpire_runs', 0.0) or 0.0),
    }
