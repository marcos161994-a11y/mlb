"""
Calibración de probabilidades del modelo.

Aprende de (probPick → acierto/fallo) liquidados y ajusta el %
para que un 60% gane cerca del 60% de las veces (isotonic / Platt).

Capa 4: además del calibrador global, entrena uno por tipo_pick
(favorito_alto / underdog / scratch / limpio) cuando hay muestras.
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
_calibradores_tipo: dict[str, tuple[str, Any]] = {}
_meta: dict[str, Any] = {}

TIPOS_PICK = ("favorito_alto", "underdog", "scratch", "limpio")
MIN_MUESTRAS_TIPO = 12


def _path() -> Path:
    return DATA_DIR / "calibrador_prob.pkl"


def _inferir_tipo(row: dict) -> str:
    t = str(row.get("tipo_pick") or "").strip().lower()
    if t in TIPOS_PICK:
        return t
    scratch = row.get("scratch_lineup") if isinstance(row.get("scratch_lineup"), dict) else {}
    if scratch.get("riesgo"):
        return "scratch"
    try:
        odds = float(row.get("odds") or 0)
        prob = float(row.get("probPick") or 50)
    except (TypeError, ValueError):
        return "limpio"
    if odds >= 2.0 or (odds >= 1.70 and prob < 55):
        return "underdog"
    if prob >= 62 or (1.01 <= odds <= 1.55):
        return "favorito_alto"
    return "limpio"


def _cargar_pares_desde_memoria(
    memoria: dict,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    xs: list[float] = []
    ys: list[int] = []
    tipos: list[str] = []
    for dia in memoria.get("dias", []):
        for apuesta in dia.get("apuestas", []):
            if apuesta.get("estado") not in ("ganada", "perdida"):
                continue
            p = float(apuesta.get("probPick") or 0)
            if p < 45 or p > 90:
                continue
            xs.append(p / 100.0)
            ys.append(1 if apuesta["estado"] == "ganada" else 0)
            tipos.append(_inferir_tipo(apuesta))
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
            tipos.append(_inferir_tipo(pred))
    if not xs:
        return np.array([]), np.array([]), []
    return np.array(xs, dtype=float), np.array(ys, dtype=int), tipos


def _fit_uno(x: np.ndarray, y: np.ndarray) -> tuple[str, Any] | None:
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    if len(x) < 8 or len(set(y.tolist())) < 2:
        return None
    try:
        iso = IsotonicRegression(y_min=0.05, y_max=0.95, out_of_bounds="clip")
        iso.fit(x, y)
        return ("isotonic", iso)
    except Exception:
        try:
            lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=500)
            lr.fit(x.reshape(-1, 1), y)
            return ("platt", lr)
        except Exception:
            return None


def entrenar_calibrador(memoria: dict, min_muestras: int = 30) -> dict[str, Any]:
    """Ajusta isotonic (o Platt). Global + por tipo_pick si hay datos."""
    global _calibrador, _calibradores_tipo, _meta
    x, y, tipos = _cargar_pares_desde_memoria(memoria)
    meta: dict[str, Any] = {
        "ok": False,
        "muestras": int(len(x)),
        "metodo": None,
        "por_tipo": {},
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

    fit = _fit_uno(x, y)
    if not fit:
        meta["mensaje"] = "No se pudo ajustar calibrador"
        _meta = meta
        return meta
    _calibrador = fit
    metodo = fit[0]

    por_tipo: dict[str, dict[str, Any]] = {}
    nuevos_tipo: dict[str, tuple[str, Any]] = {}
    for tipo in TIPOS_PICK:
        idx = [i for i, t in enumerate(tipos) if t == tipo]
        if len(idx) < MIN_MUESTRAS_TIPO:
            por_tipo[tipo] = {"ok": False, "muestras": len(idx)}
            continue
        xt, yt = x[idx], y[idx]
        ft = _fit_uno(xt, yt)
        if not ft:
            por_tipo[tipo] = {"ok": False, "muestras": len(idx)}
            continue
        nuevos_tipo[tipo] = ft
        por_tipo[tipo] = {
            "ok": True,
            "muestras": len(idx),
            "metodo": ft[0],
        }
    _calibradores_tipo = nuevos_tipo

    payload = {
        "tipo": _calibrador[0],
        "modelo": _calibrador[1],
        "muestras": len(x),
        "por_tipo": {
            k: {"tipo": v[0], "modelo": v[1], "muestras": por_tipo.get(k, {}).get("muestras")}
            for k, v in nuevos_tipo.items()
        },
    }
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

    ece = _ece(x, y, n_bins=5)
    n_tipos_ok = sum(1 for v in por_tipo.values() if v.get("ok"))
    meta.update(
        {
            "ok": True,
            "metodo": metodo,
            "ece": round(ece, 3),
            "por_tipo": por_tipo,
            "mensaje": (
                f"Calibrado {metodo} con {len(x)} muestras (ECE≈{ece:.2f})"
                + (f" · {n_tipos_ok} tipos" if n_tipos_ok else "")
            ),
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
    global _calibrador, _calibradores_tipo, _meta
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
        _calibradores_tipo = {}
        for k, v in (payload.get("por_tipo") or {}).items():
            if isinstance(v, dict) and v.get("modelo") is not None:
                _calibradores_tipo[str(k)] = (v.get("tipo") or "isotonic", v["modelo"])
        _meta = {
            "ok": True,
            "muestras": payload.get("muestras"),
            "metodo": payload.get("tipo"),
            "tipos_activos": list(_calibradores_tipo.keys()),
            "mensaje": "Calibrador cargado",
        }
        return _calibrador[1] is not None
    except Exception as e:
        print(f"[CALIB] Error cargando: {e}")
        return False


def _aplicar(modelo_pair: tuple[str, Any], p: float) -> float:
    tipo, modelo = modelo_pair
    if tipo == "isotonic":
        return float(modelo.predict([p])[0])
    return float(modelo.predict_proba([[p]])[0, 1])


def calibrar_probabilidad(
    prob_pct: float,
    cfg: dict | None = None,
    *,
    tipo_pick: str | None = None,
) -> float:
    """
    Ajusta probabilidad 0-100. Si hay calibrador por tipo_pick, lo usa;
    si no, el global. Si está desactivado, devuelve igual.
    """
    cfg = cfg or {}
    if not cfg.get("usar_calibracion", True):
        return round(float(prob_pct), 1)
    if _calibrador is None:
        cargar_calibrador()
    if _calibrador is None or _calibrador[1] is None:
        return round(float(prob_pct), 1)

    p = max(0.01, min(0.99, float(prob_pct) / 100.0))
    pair = _calibrador
    t = str(tipo_pick or "").strip().lower()
    if t and t in _calibradores_tipo:
        pair = _calibradores_tipo[t]
    try:
        p2 = _aplicar(pair, p)
        p2 = max(0.22, min(0.78, p2))
        return round(p2 * 100.0, 1)
    except Exception:
        return round(float(prob_pct), 1)


def calibrar_par(
    prob_away: float,
    prob_home: float,
    cfg: dict | None = None,
    *,
    tipo_pick: str | None = None,
) -> tuple[float, float]:
    """Calibra ambos lados y renormaliza a 100%."""
    a = calibrar_probabilidad(prob_away, cfg, tipo_pick=tipo_pick)
    h = calibrar_probabilidad(prob_home, cfg, tipo_pick=tipo_pick)
    s = a + h
    if s <= 0:
        return prob_away, prob_home
    a = round(100.0 * a / s, 1)
    h = round(100.0 - a, 1)
    return a, h


def meta_calibracion() -> dict[str, Any]:
    if not _meta and _calibrador is None:
        cargar_calibrador()
    out = dict(_meta) if _meta else {"ok": False, "mensaje": "Sin calibrador"}
    if _calibradores_tipo:
        out["tipos_activos"] = list(_calibradores_tipo.keys())
    return out
