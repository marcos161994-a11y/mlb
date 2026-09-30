"""Filtro de valor: apostar solo si la probabilidad calibrada le gana a la casa.

Contrato de cuota real (compartido con el trabajo de momios de bc-015ab330,
que al cerrar este cambio aún no tenía PR):

- `cuota_real_decimal`: decimal explícito de una casa. Si viene, manda.
- `precio_congelado`: se usa solo cuando es decimal (entre 1.01 y 15).
  Un momio americano (+150 / -150) no entra por aquí.
- `odds`: decimal del lado del pick, el campo que ya guarda el servidor.
- `fuente_momio`: id de la casa, o `estimado` cuando la cadena de momios
  no encontró precio real. `estimado` no cuenta como edge.
- `lineas_fuente`: fuente actual (`draftkings`, `pinnacle`, `espn`…).
  `modelo` y `estimado` no son casa.

Sin cuota real el filtro no apuesta. No toca el tamaño del stake.
"""

from __future__ import annotations

from typing import Any, Callable

_FUENTES_NO_REALES = frozenset({"", "modelo", "none", "null", "import", "estimado"})
_TIPOS = ("favorito_alto", "underdog", "scratch", "limpio")


def fuente_casa_real(registro: dict | None) -> bool:
    """True si el precio viene de una casa, no del modelo ni de un estimado."""
    if not isinstance(registro, dict):
        return False
    fuente_momio = registro.get("fuente_momio")
    if fuente_momio is not None and str(fuente_momio).strip() != "":
        return str(fuente_momio).strip().lower() not in _FUENTES_NO_REALES
    return str(registro.get("lineas_fuente") or "").strip().lower() not in _FUENTES_NO_REALES


def _decimal_valido(valor: Any) -> float | None:
    try:
        decimal = float(valor)
    except (TypeError, ValueError):
        return None
    if 1.01 < decimal <= 15.0:
        return decimal
    return None


def cuota_decimal_real(registro: dict | None) -> float | None:
    """Decimal de casa para el lado ya elegido, o None si no hay precio real."""
    if not fuente_casa_real(registro):
        return None
    assert registro is not None
    for campo in ("cuota_real_decimal", "precio_congelado", "odds"):
        decimal = _decimal_valido(registro.get(campo))
        if decimal is not None:
            return decimal
    return None


def cuota_lado_real(juego: dict | None, lado: str) -> float | None:
    """Decimal de casa del visitante (`away`) o del local (`home`)."""
    if not fuente_casa_real(juego):
        return None
    assert juego is not None
    campo = "odds_away_decimal" if lado == "away" else "odds_home_decimal"
    return _decimal_valido(juego.get(campo))


def filtro_valor_cfg(cfg: dict | None) -> dict[str, Any]:
    cfg = cfg or {}
    estrategia = cfg.get("estrategia") if isinstance(cfg.get("estrategia"), dict) else {}
    raw = estrategia.get("filtro_valor") if isinstance(estrategia.get("filtro_valor"), dict) else {}
    activo = bool(raw.get("activo", False))
    try:
        base = float(raw.get("margen_min_pct", 2.5))
    except (TypeError, ValueError):
        base = 2.5
    try:
        underdog = float(raw.get("margen_underdog_pct", 5.0))
    except (TypeError, ValueError):
        underdog = 5.0
    try:
        scratch = float(raw.get("margen_scratch_pct", base))
    except (TypeError, ValueError):
        scratch = base
    if "penalizar_scratch" in raw:
        penalizar = bool(raw.get("penalizar_scratch"))
    else:
        # Sin la clave se conserva el veto histórico de scratch.
        penalizar = True
    return {
        "activo": activo,
        "margen_min_pct": base,
        "margen_underdog_pct": underdog,
        "margen_scratch_pct": scratch,
        "penalizar_scratch": penalizar,
    }


def filtro_activo(cfg: dict | None) -> bool:
    return bool(filtro_valor_cfg(cfg)["activo"])


def penaliza_scratch(cfg: dict | None) -> bool:
    return bool(filtro_valor_cfg(cfg)["penalizar_scratch"])


def tipo_para_cuota(juego: dict | None, prob: float, odds: float) -> str:
    juego = juego or {}
    try:
        from inteligencia_mlb import clasificar_tipo_pick

        tipo = clasificar_tipo_pick(juego, prob=prob, odds=odds)
    except Exception:
        tipo = str(juego.get("tipo_pick") or "")
    tipo = str(tipo or "").strip().lower()
    return tipo if tipo in _TIPOS else "limpio"


def margen_para_tipo(cfg: dict | None, tipo: str) -> float:
    """Scratch usa el margen base: no se le exige el recargo de underdog."""
    fv = filtro_valor_cfg(cfg)
    if tipo == "scratch":
        return float(fv["margen_scratch_pct"])
    if tipo == "underdog":
        return float(fv["margen_underdog_pct"])
    return float(fv["margen_min_pct"])


def _minimos(cfg: dict | None) -> tuple[float, float]:
    estrategia = (cfg or {}).get("estrategia") if isinstance((cfg or {}).get("estrategia"), dict) else {}
    try:
        min_prob = float(estrategia.get("min_prob_modelo", 58.0))
    except (TypeError, ValueError):
        min_prob = 58.0
    try:
        min_edge = float(estrategia.get("min_edge_pct", 6.0))
    except (TypeError, ValueError):
        min_edge = 6.0
    return min_prob, min_edge


def evaluar_valor(registro: dict | None, cfg: dict | None) -> dict[str, Any]:
    """Decide si hay edge real contra la cuota de casa.

    Con el filtro apagado igual calcula el edge, y el margen es `min_edge_pct`.
    Con el filtro activo el margen depende del tipo: underdog paga más,
    scratch paga el margen base.
    """
    fv = filtro_valor_cfg(cfg)
    min_prob, min_edge = _minimos(cfg)
    reg = registro or {}
    try:
        prob = float(reg.get("probPick") or 0)
    except (TypeError, ValueError):
        prob = 0.0
    cuota = cuota_decimal_real(reg)
    out: dict[str, Any] = {
        "filtro_activo": fv["activo"],
        "apostar": False,
        "prob": round(prob, 1),
        "cuota": cuota,
        "implicita": None,
        "edge": None,
        "margen": None,
        "tipo": None,
        "min_prob": min_prob,
        "motivo": "Sin cuota real de casa",
    }
    if cuota is None:
        return out
    implicita = round(100.0 / cuota, 1)
    edge = round(prob - implicita, 1)
    tipo = str(reg.get("tipo_pick") or "").strip().lower()
    if tipo not in _TIPOS:
        tipo = tipo_para_cuota(reg, prob, cuota)
    margen = margen_para_tipo(cfg, tipo) if fv["activo"] else min_edge
    apostar = prob >= min_prob and edge >= margen
    if apostar:
        motivo = f"Valor +{edge:.1f} pts vs casa (margen {margen:.1f}, {tipo})"
    elif prob < min_prob:
        motivo = f"Prob. calibrada {prob:.1f}% bajo {min_prob:.0f}%"
    else:
        motivo = f"Sin valor: edge {edge:+.1f} pts < margen {margen:.1f} ({tipo})"
    out.update(
        {
            "apostar": apostar,
            "implicita": implicita,
            "edge": edge,
            "margen": margen,
            "tipo": tipo,
            "motivo": motivo,
        }
    )
    return out


def filas_liquidadas(memoria: dict | None) -> list[dict[str, Any]]:
    """Picks liquidados, una fila por juego. La apuesta con dinero gana al papel."""
    filas: list[dict[str, Any]] = []
    for dia in (memoria or {}).get("dias") or []:
        if not isinstance(dia, dict):
            continue
        fecha = str(dia.get("fecha") or "")
        vistos: set[str] = set()
        for apuesta in dia.get("apuestas") or []:
            if not isinstance(apuesta, dict):
                continue
            if apuesta.get("estado") not in ("ganada", "perdida"):
                continue
            fila = dict(apuesta)
            fila["_fecha"] = fecha
            fila["_resultado"] = "acierto" if apuesta.get("estado") == "ganada" else "fallo"
            filas.append(fila)
            if apuesta.get("game_id") is not None:
                vistos.add(str(apuesta.get("game_id")))
        for pred in dia.get("predicciones") or []:
            if not isinstance(pred, dict):
                continue
            if pred.get("resultado") not in ("acierto", "fallo"):
                continue
            gid = str(pred.get("game_id") or "")
            if gid and gid in vistos:
                continue
            fila = dict(pred)
            fila["_fecha"] = fecha
            fila["_resultado"] = pred.get("resultado")
            filas.append(fila)
    filas.sort(key=lambda row: (str(row.get("_fecha") or ""), str(row.get("game_id") or "")))
    return filas


def _stake_fila(fila: dict[str, Any]) -> float:
    for campo in ("stake_virtual", "stake"):
        try:
            stake = float(fila.get(campo))
        except (TypeError, ValueError):
            continue
        if stake > 0:
            return stake
    return 5.0


def _pnl_fila(fila: dict[str, Any]) -> tuple[float, float] | None:
    cuota = cuota_decimal_real(fila)
    if cuota is None:
        return None
    stake = _stake_fila(fila)
    if fila.get("_resultado") == "acierto":
        return stake * (cuota - 1.0), stake
    if fila.get("_resultado") == "fallo":
        return -stake, stake
    return None


def _roi(filas: list[dict[str, Any]], *, filtrar: bool, cfg: dict, cal: Callable | None) -> dict[str, Any]:
    profit = 0.0
    stake = 0.0
    n = 0
    aciertos = 0
    for fila in filas:
        reg = dict(fila)
        if cal is not None:
            reg["probPick"] = float(cal(fila))
        if filtrar and not evaluar_valor(reg, cfg)["apostar"]:
            continue
        pnl = _pnl_fila(reg)
        if pnl is None:
            continue
        profit += pnl[0]
        stake += pnl[1]
        n += 1
        if fila.get("_resultado") == "acierto":
            aciertos += 1
    return {
        "n": n,
        "aciertos": aciertos,
        "roi_pct": round(100.0 * profit / stake, 1) if stake else None,
        "profit": round(profit, 2),
        "stake": round(stake, 2),
    }


def _memoria_de_filas(filas: list[dict[str, Any]]) -> dict[str, Any]:
    por_fecha: dict[str, list[dict[str, Any]]] = {}
    for fila in filas:
        fecha = str(fila.get("_fecha") or "1970-01-01")
        pred = dict(fila)
        pred["estado"] = "liquidado"
        pred["resultado"] = fila.get("_resultado")
        por_fecha.setdefault(fecha, []).append(pred)
    return {
        "dias": [
            {"fecha": fecha, "predicciones": preds, "apuestas": []}
            for fecha, preds in por_fecha.items()
        ]
    }


def backtest_historico(memoria: dict, cfg: dict | None = None) -> dict[str, Any]:
    """Backtest: holdout final (30%) con calibrador ajustado solo en el 70% previo.

    Antes = todos los liquidados con cuota real. Después = los que pasan el
    filtro con la probabilidad calibrada. No reescribe la memoria.
    """
    import calibracion

    cfg = cfg or {}
    con_cuota = [fila for fila in filas_liquidadas(memoria) if cuota_decimal_real(fila) is not None]
    n = len(con_cuota)
    corte = int(n * 0.70)
    if corte < 30 or n - corte < 15:
        return {
            "ok": False,
            "motivo": f"Pocas filas con cuota real para un holdout ({n})",
            "n_con_cuota_real": n,
        }
    train, holdout = con_cuota[:corte], con_cuota[corte:]
    snapshot = (
        calibracion._calibrador,
        dict(calibracion._calibradores_tipo),
        dict(calibracion._calibradores_segmento),
        dict(calibracion._meta),
    )
    try:
        calibracion._calibrador = None
        calibracion._calibradores_tipo = {}
        calibracion._calibradores_segmento = {}
        meta = calibracion.entrenar_calibrador(_memoria_de_filas(train), min_muestras=30)

        def _cal(fila: dict[str, Any]) -> float:
            return calibracion.calibrar_probabilidad(
                float(fila.get("probPick") or 0),
                cfg,
                tipo_pick=None,
            )

        antes = _roi(holdout, filtrar=False, cfg=cfg, cal=None)
        despues = _roi(holdout, filtrar=True, cfg=cfg, cal=_cal)
        return {
            "ok": bool(meta.get("ok")),
            "etiqueta": "backtest",
            "n_con_cuota_real": n,
            "n_train": len(train),
            "n_holdout": len(holdout),
            "metodo": meta.get("metodo"),
            "holdout_calibracion": meta.get("holdout"),
            "antes": antes,
            "despues": despues,
        }
    finally:
        (
            calibracion._calibrador,
            calibracion._calibradores_tipo,
            calibracion._calibradores_segmento,
            calibracion._meta,
        ) = snapshot
