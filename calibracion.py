"""
Calibración de probabilidades del modelo.

Aprende de (probPick → acierto/fallo) liquidados y ajusta el %
para que un 60% gane cerca del 60% de las veces (isotonic / Platt).

Capa 4: además del calibrador global, entrena uno por tipo_pick
(favorito_alto / underdog / scratch / limpio) cuando hay muestras.
"""

from __future__ import annotations

import json
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
_calibradores_segmento: dict[str, tuple[str, Any]] = {}
_meta: dict[str, Any] = {}

TIPOS_PICK = ("favorito_alto", "underdog", "scratch", "limpio")
SEGMENTOS_EXTRA = ("prob_alta", "underdog_cuota", "mc_over", "entorno_f5", "general")
MIN_MUESTRAS_TIPO = 40
MIN_MUESTRAS_SEGMENTO = 40
_MIN_TRAIN_HOLDOUT = 40
_MIN_HOLDOUT = 25


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
) -> tuple[np.ndarray, np.ndarray, list[str], list[str], np.ndarray]:
    from aprendizaje_mlb import peso_muestra_aprendizaje, segmento_calibracion

    xs: list[float] = []
    ys: list[int] = []
    tipos: list[str] = []
    segmentos: list[str] = []
    pesos: list[float] = []

    def _add_row(row: dict, y_val: int) -> None:
        p = float(row.get("probPick") or 0)
        if p < 45 or p > 90:
            return
        w = peso_muestra_aprendizaje(row)
        if w <= 0:
            return
        xs.append(p / 100.0)
        ys.append(y_val)
        tipos.append(_inferir_tipo(row))
        segmentos.append(segmento_calibracion(row))
        pesos.append(w)

    for dia in memoria.get("dias", []):
        for apuesta in dia.get("apuestas", []):
            if apuesta.get("estado") not in ("ganada", "perdida"):
                continue
            _add_row(apuesta, 1 if apuesta["estado"] == "ganada" else 0)
        vistos = {a.get("game_id") for a in dia.get("apuestas", []) if a.get("estado") in ("ganada", "perdida")}
        for pred in dia.get("predicciones", []):
            if pred.get("estado") != "liquidado":
                continue
            if pred.get("resultado") not in ("acierto", "fallo"):
                continue
            if pred.get("game_id") in vistos:
                continue
            _add_row(pred, 1 if pred["resultado"] == "acierto" else 0)
    if not xs:
        return np.array([]), np.array([]), [], [], np.array([])
    return (
        np.array(xs, dtype=float),
        np.array(ys, dtype=int),
        tipos,
        segmentos,
        np.array(pesos, dtype=float),
    )


def _fit_metodo(
    metodo: str,
    x: np.ndarray,
    y: np.ndarray,
    sample_weight: np.ndarray | None = None,
) -> tuple[str, Any] | None:
    """Ajusta isotonic o Platt. Platt es el fallback estable con poca historia."""
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    if len(x) < 8 or len(set(y.tolist())) < 2:
        return None
    usar_peso = sample_weight is not None and len(sample_weight) == len(x)
    try:
        if metodo == "isotonic":
            iso = IsotonicRegression(y_min=0.05, y_max=0.95, out_of_bounds="clip")
            if usar_peso:
                iso.fit(x, y, sample_weight=sample_weight)
            else:
                iso.fit(x, y)
            return ("isotonic", iso)
        lr = LogisticRegression(C=1.0, solver="lbfgs", max_iter=500)
        if usar_peso:
            lr.fit(x.reshape(-1, 1), y, sample_weight=sample_weight)
        else:
            lr.fit(x.reshape(-1, 1), y)
        return ("platt", lr)
    except Exception:
        return None


def _fit_uno(x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None) -> tuple[str, Any] | None:
    return _fit_metodo("isotonic", x, y, sample_weight) or _fit_metodo("platt", x, y, sample_weight)


def _predecir_par(par: tuple[str, Any], x: np.ndarray) -> np.ndarray:
    tipo, modelo = par
    if tipo == "isotonic":
        return np.asarray(modelo.predict(np.asarray(x, dtype=float)), dtype=float)
    matriz = np.asarray(x, dtype=float).reshape(-1, 1)
    return np.asarray(modelo.predict_proba(matriz)[:, 1], dtype=float)


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    pred = np.clip(np.asarray(p, dtype=float), 0.0, 1.0)
    real = np.asarray(y, dtype=float)
    if len(pred) == 0:
        return 0.0
    return float(np.mean((pred - real) ** 2))


def _reliability(p: np.ndarray, y: np.ndarray, n_bins: int = 8) -> list[dict[str, Any]]:
    pred = np.clip(np.asarray(p, dtype=float), 0.0, 1.0)
    real = np.asarray(y, dtype=float)
    bordes = np.linspace(0.40, 0.90, n_bins + 1)
    bins: list[dict[str, Any]] = []
    for i in range(n_bins):
        if i == n_bins - 1:
            mask = (pred >= bordes[i]) & (pred <= bordes[i + 1])
        else:
            mask = (pred >= bordes[i]) & (pred < bordes[i + 1])
        if not np.any(mask):
            continue
        bins.append(
            {
                "desde": round(float(bordes[i]), 3),
                "hasta": round(float(bordes[i + 1]), 3),
                "n": int(np.sum(mask)),
                "prob_media": round(float(np.mean(pred[mask])), 3),
                "frecuencia": round(float(np.mean(real[mask])), 3),
            }
        )
    return bins


def _ece_de_bins(bins: list[dict[str, Any]], n: int) -> float:
    if n <= 0:
        return 0.0
    total = 0.0
    for b in bins:
        total += (int(b["n"]) / n) * abs(float(b["frecuencia"]) - float(b["prob_media"]))
    return float(total)


def comparar_en_holdout(
    x: np.ndarray,
    y: np.ndarray,
    pesos: np.ndarray | None = None,
) -> dict[str, Any]:
    """Elige isotonic o Platt por Brier en el tramo final (holdout cronológico).

    Con poca historia gana Platt: isotonic se parte en escalones y memoriza.
    Isotonic solo gana si baja el Brier del holdout en al menos 0.005.
    """
    n = int(len(x))
    w = pesos if pesos is not None and len(pesos) == n else np.ones(n, dtype=float)
    base: dict[str, Any] = {
        "n": n,
        "elegido": "platt",
        "motivo": "",
        "n_train": 0,
        "n_holdout": 0,
        "brier_antes": None,
        "brier_despues": None,
        "ece_antes": None,
        "ece_despues": None,
        "bins_antes": [],
        "bins_despues": [],
        "candidatos": {},
    }
    if n < _MIN_TRAIN_HOLDOUT + _MIN_HOLDOUT or len(set(y.tolist())) < 2:
        base["motivo"] = (
            "Muestra corta para un holdout estable: Platt es más robusto que isotonic."
        )
        return base
    corte = int(round(n * 0.70))
    corte = min(max(corte, _MIN_TRAIN_HOLDOUT), n - _MIN_HOLDOUT)
    x_tr, y_tr, w_tr = x[:corte], y[:corte], w[:corte]
    x_ho, y_ho = x[corte:], y[corte:]
    base["n_train"] = int(len(x_tr))
    base["n_holdout"] = int(len(x_ho))
    if len(set(y_tr.tolist())) < 2 or len(set(y_ho.tolist())) < 2:
        base["motivo"] = "El holdout no tiene aciertos y fallos: se usa Platt."
        return base

    brier_antes = _brier(x_ho, y_ho)
    bins_antes = _reliability(x_ho, y_ho)
    ece_antes = _ece_de_bins(bins_antes, len(y_ho))
    candidatos: dict[str, Any] = {}
    for metodo in ("isotonic", "platt"):
        ajuste = _fit_metodo(metodo, x_tr, y_tr, w_tr)
        if not ajuste:
            candidatos[metodo] = {"ok": False}
            continue
        pred = np.clip(_predecir_par(ajuste, x_ho), 0.01, 0.99)
        bins = _reliability(pred, y_ho)
        candidatos[metodo] = {
            "ok": True,
            "brier": round(_brier(pred, y_ho), 4),
            "ece": round(_ece_de_bins(bins, len(y_ho)), 4),
        }
    iso = candidatos.get("isotonic") or {}
    platt = candidatos.get("platt") or {}
    # Isotonic solo entra si le gana al crudo y a Platt. Si no, memoriza
    # escalones y no corrige la sobreconfianza. Platt es el modelo robusto.
    elegido = "platt"
    motivo = (
        "Platt es más robusto con este historial: isotonic no mejora el Brier "
        "crudo del holdout."
    )
    if iso.get("ok") and platt.get("ok"):
        iso_brier = float(iso["brier"])
        platt_brier = float(platt["brier"])
        if iso_brier + 0.005 < platt_brier and iso_brier < float(brier_antes):
            elegido = "isotonic"
            motivo = (
                "Isotonic mejora el Brier del holdout frente al crudo y frente a Platt."
            )
        elif platt_brier < float(brier_antes) and platt_brier <= iso_brier:
            motivo = "Platt baja el Brier del holdout y es más estable que isotonic."
    elif iso.get("ok") and not platt.get("ok"):
        elegido = "isotonic"
        motivo = "Platt no ajustó en el train; se usa isotonic."
    elif not iso.get("ok") and not platt.get("ok"):
        motivo = "Ningún método ajustó en el train; se reintenta Platt con toda la muestra."

    ajuste_ho = _fit_metodo(elegido, x_tr, y_tr, w_tr)
    if ajuste_ho:
        pred_fin = np.clip(_predecir_par(ajuste_ho, x_ho), 0.01, 0.99)
        brier_despues = _brier(pred_fin, y_ho)
        bins_despues = _reliability(pred_fin, y_ho)
        ece_despues = _ece_de_bins(bins_despues, len(y_ho))
    else:
        brier_despues = brier_antes
        bins_despues = bins_antes
        ece_despues = ece_antes
    base.update(
        {
            "elegido": elegido,
            "motivo": motivo,
            "brier_antes": round(brier_antes, 4),
            "brier_despues": round(brier_despues, 4),
            "ece_antes": round(ece_antes, 4),
            "ece_despues": round(ece_despues, 4),
            "bins_antes": bins_antes,
            "bins_despues": bins_despues,
            "candidatos": candidatos,
        }
    )
    return base


def _guardar_reporte(meta: dict[str, Any]) -> None:
    """JSON legible al lado del pickle. No incluye el modelo."""
    publico = {
        "ok": meta.get("ok"),
        "metodo": meta.get("metodo"),
        "muestras": meta.get("muestras"),
        "ece": meta.get("ece"),
        "mensaje": meta.get("mensaje"),
        "holdout": meta.get("holdout"),
        "por_tipo": meta.get("por_tipo"),
    }
    try:
        (_path().with_name("calibracion_reporte.json")).write_text(
            json.dumps(publico, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[CALIB] No se pudo guardar el reporte: {exc}")


def entrenar_calibrador(memoria: dict, min_muestras: int = 30) -> dict[str, Any]:
    """Ajusta isotonic (o Platt). Global + por tipo_pick si hay datos."""
    global _calibrador, _calibradores_tipo, _calibradores_segmento, _meta
    x, y, tipos, segmentos, pesos = _cargar_pares_desde_memoria(memoria)
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

    pesos_fit = pesos if len(pesos) == len(x) else None
    comparacion = comparar_en_holdout(x, y, pesos_fit if pesos_fit is not None else np.ones(len(x)))
    meta["holdout"] = comparacion
    metodo_elegido = str(comparacion.get("elegido") or "platt")
    fit = _fit_metodo(metodo_elegido, x, y, pesos_fit)
    if not fit:
        fit = _fit_uno(x, y, pesos_fit)
    if not fit:
        meta["mensaje"] = "No se pudo ajustar calibrador"
        _meta = meta
        _guardar_reporte(meta)
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
        ft = _fit_metodo(metodo, xt, yt, pesos[idx] if len(pesos) == len(x) else None)
        if not ft:
            ft = _fit_uno(xt, yt, pesos[idx] if len(pesos) == len(x) else None)
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

    _calibradores_segmento = {}
    por_seg: dict[str, dict[str, Any]] = {}
    for seg in SEGMENTOS_EXTRA:
        idx = [i for i, s in enumerate(segmentos) if s == seg]
        if len(idx) < MIN_MUESTRAS_SEGMENTO:
            por_seg[seg] = {"ok": False, "muestras": len(idx)}
            continue
        xt, yt = x[idx], y[idx]
        wt = pesos[idx] if len(pesos) == len(x) else None
        fs = _fit_metodo(metodo, xt, yt, wt) or _fit_uno(xt, yt, wt)
        if not fs:
            por_seg[seg] = {"ok": False, "muestras": len(idx)}
            continue
        _calibradores_segmento[seg] = fs
        por_seg[seg] = {"ok": True, "muestras": len(idx), "metodo": fs[0]}
    meta["por_segmento"] = por_seg

    payload = {
        "tipo": _calibrador[0],
        "modelo": _calibrador[1],
        "muestras": len(x),
        "por_tipo": {
            k: {"tipo": v[0], "modelo": v[1], "muestras": por_tipo.get(k, {}).get("muestras")}
            for k, v in nuevos_tipo.items()
        },
        "por_segmento": {
            k: {"tipo": v[0], "modelo": v[1], "muestras": por_seg.get(k, {}).get("muestras")}
            for k, v in _calibradores_segmento.items()
        },
        "holdout": comparacion,
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
    brier_txt = ""
    if comparacion.get("brier_antes") is not None and comparacion.get("brier_despues") is not None:
        brier_txt = (
            f" · holdout Brier {comparacion['brier_antes']:.3f}→{comparacion['brier_despues']:.3f}"
        )
    meta.update(
        {
            "ok": True,
            "metodo": metodo,
            "ece": round(ece, 3),
            "por_tipo": por_tipo,
            "holdout": comparacion,
            "mensaje": (
                f"Calibrado {metodo} con {len(x)} muestras (ECE≈{ece:.2f})"
                + (f" · {n_tipos_ok} tipos" if n_tipos_ok else "")
                + brier_txt
            ),
        }
    )
    _meta = meta
    _guardar_reporte(meta)
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
    global _calibrador, _calibradores_tipo, _calibradores_segmento, _meta
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
        _calibradores_segmento = {}
        for k, v in (payload.get("por_segmento") or {}).items():
            if isinstance(v, dict) and v.get("modelo") is not None:
                _calibradores_segmento[str(k)] = (v.get("tipo") or "isotonic", v["modelo"])
        _meta = {
            "ok": True,
            "muestras": payload.get("muestras"),
            "metodo": payload.get("tipo"),
            "holdout": payload.get("holdout"),
            "tipos_activos": list(_calibradores_tipo.keys()),
            "segmentos_activos": list(_calibradores_segmento.keys()),
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
    Ajusta la probabilidad final 0-100 con el calibrador global.
    `tipo_pick` se conserva en la firma para los llamadores; no cambia el ajuste.
    """
    cfg = cfg or {}
    if not cfg.get("usar_calibracion", True):
        return round(float(prob_pct), 1)
    if _calibrador is None:
        cargar_calibrador()
    if _calibrador is None or _calibrador[1] is None:
        return round(float(prob_pct), 1)

    p = max(0.01, min(0.99, float(prob_pct) / 100.0))
    # Siempre el calibrador global. Partir por tipo con esta historia
    # sobreajusta y vuelve a inflar la probabilidad final.
    pair = _calibrador
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
