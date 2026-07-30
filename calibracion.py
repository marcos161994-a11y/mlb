"""
Calibración de probabilidades del modelo.

Aprende de (probPick → acierto/fallo) liquidados y ajusta el %
para que un 60% gane cerca del 60% de las veces (isotonic / Platt).
"""

from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any

import numpy as np

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_calibrador = None
_meta: dict[str, Any] = {}


def _path() -> Path:
    return DATA_DIR / "calibrador_prob.pkl"


def _cargar_pares_desde_memoria(memoria: dict) -> tuple[np.ndarray, np.ndarray]:
    xs: list[float] = []
    ys: list[int] = []
    for dia in memoria.get("dias", []):
        for apuesta in dia.get("apuestas", []):
            if apuesta.get("estado") not in ("ganada", "perdida"):
                continue
            p = float(apuesta.get("probPick") or 0)
            if p < 45 or p > 90:
                continue
            xs.append(p / 100.0)
            ys.append(1 if apuesta["estado"] == "ganada" else 0)
        vistos = {a.get("game_id") for a in dia.get("apuestas", []) if a.get("estado") in ("ganada", "perdida")}
        for pred in dia.get("predicciones", []):
            if pred.get("estado") != "liquidado":
                continue
            if pred.get("resultado") not in ("acierto", "fallo"):
                continue
            if pred.get("game_id") in vistos:
                continue
            p = float(pred.get("probPick") or 0)
            if p < 45 or p > 90:
                continue
            xs.append(p / 100.0)
            ys.append(1 if pred["resultado"] == "acierto" else 0)
    if not xs:
        return np.array([]), np.array([])
    return np.array(xs, dtype=float), np.array(ys, dtype=int)


def entrenar_calibrador(memoria: dict, min_muestras: int = 30) -> dict[str, Any]:
    """Ajusta isotonic (o Platt si pocas clases en bins). Guarda en disco."""
    global _calibrador, _meta
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    x, y = _cargar_pares_desde_memoria(memoria)
    meta: dict[str, Any] = {
        "ok": False,
        "muestras": int(len(x)),
        "metodo": None,
        "mensaje": "",
    }
    if len(x) < min_muestras:
        meta["mensaje"] = f"Esperando más liquidaciones ({len(x)}/{min_muestras})"
        _meta = meta
        return meta
    if len(set(y.tolist())) < 2:
        meta["mensaje"] = "Necesita aciertos y fallos para calibrar"
        _meta = meta
        return meta

    # Isotonic: no asume forma; bueno con 30+ muestras
    try:
        iso = IsotonicRegression(y_min=0.05, y_max=0.95, out_of_bounds="clip")
        iso.fit(x, y)
        # Sanity: predicción en 0.58 no debe ser extrema sin datos
        _calibrador = ("isotonic", iso)
        metodo = "isotonic"
    except Exception:
        lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=500)
        lr.fit(x.reshape(-1, 1), y)
        _calibrador = ("platt", lr)
        metodo = "platt"

    payload = {"tipo": _calibrador[0], "modelo": _calibrador[1], "muestras": len(x)}
    try:
        with open(_path(), "wb") as f:
            pickle.dump(payload, f)
        if DATA_DIR.resolve() != BASE_DIR.resolve():
            try:
                (BASE_DIR / "calibrador_prob.pkl").write_bytes(_path().read_bytes())
            except OSError:
                pass
    except OSError as e:
        meta["mensaje"] = f"No se pudo guardar calibrador: {e}"
        _meta = meta
        return meta

    # Diagnóstico simple: ECE aproximado en 5 bins
    ece = _ece(x, y, n_bins=5)
    meta.update(
        {
            "ok": True,
            "metodo": metodo,
            "ece": round(ece, 3),
            "mensaje": f"Calibrado {metodo} con {len(x)} muestras (ECE≈{ece:.2f})",
        }
    )
    _meta = meta
    print(f"[CALIB] {meta['mensaje']}")
    return meta


def _ece(x: np.ndarray, y: np.ndarray, n_bins: int = 5) -> float:
    bins = np.linspace(0.45, 0.90, n_bins + 1)
    total = 0.0
    n = len(x)
    if n == 0:
        return 0.0
    for i in range(n_bins):
        m = (x >= bins[i]) & (x < bins[i + 1])
        if not np.any(m):
            continue
        conf = float(np.mean(x[m]))
        acc = float(np.mean(y[m]))
        total += (np.sum(m) / n) * abs(acc - conf)
    return float(total)


def cargar_calibrador() -> bool:
    global _calibrador, _meta
    if _calibrador is not None:
        return True
    path = _path()
    if not path.exists() and DATA_DIR.resolve() != BASE_DIR.resolve():
        alt = BASE_DIR / "calibrador_prob.pkl"
        if alt.exists():
            try:
                path.write_bytes(alt.read_bytes())
            except OSError:
                pass
    if not path.exists():
        return False
    try:
        with open(path, "rb") as f:
            payload = pickle.load(f)
        _calibrador = (payload.get("tipo"), payload.get("modelo"))
        _meta = {
            "ok": True,
            "muestras": payload.get("muestras"),
            "metodo": payload.get("tipo"),
            "mensaje": "Calibrador cargado",
        }
        return _calibrador[1] is not None
    except Exception as e:
        print(f"[CALIB] Error cargando: {e}")
        return False


def calibrar_probabilidad(prob_pct: float, cfg: dict | None = None) -> float:
    """
    Ajusta probabilidad 0-100. Si no hay calibrador o está desactivado, devuelve igual.
    """
    cfg = cfg or {}
    if not cfg.get("usar_calibracion", True):
        return round(float(prob_pct), 1)
    if _calibrador is None:
        cargar_calibrador()
    if _calibrador is None or _calibrador[1] is None:
        return round(float(prob_pct), 1)

    p = max(0.01, min(0.99, float(prob_pct) / 100.0))
    tipo, modelo = _calibrador
    try:
        if tipo == "isotonic":
            p2 = float(modelo.predict([p])[0])
        else:
            p2 = float(modelo.predict_proba([[p]])[0, 1])
        p2 = max(0.22, min(0.78, p2))
        return round(p2 * 100.0, 1)
    except Exception:
        return round(float(prob_pct), 1)


def calibrar_par(prob_away: float, prob_home: float, cfg: dict | None = None) -> tuple[float, float]:
    """Calibra ambos lados y renormaliza a 100%."""
    a = calibrar_probabilidad(prob_away, cfg)
    h = calibrar_probabilidad(prob_home, cfg)
    s = a + h
    if s <= 0:
        return prob_away, prob_home
    a = round(100.0 * a / s, 1)
    h = round(100.0 - a, 1)
    return a, h


def meta_calibracion() -> dict[str, Any]:
    if not _meta and _calibrador is None:
        cargar_calibrador()
    return dict(_meta) if _meta else {"ok": False, "mensaje": "Sin calibrador"}
