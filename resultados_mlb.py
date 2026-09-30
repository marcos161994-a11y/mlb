"""Agregados de solo lectura del experimento de papel ($100 de salida).

No escribe memoria ni llama a la red. El endpoint GET /api/resultados
solo devuelve este dict.

La curva, el ROI, la racha, el drawdown, el tipo de pick y la fuente
de la cuota salen de las apuestas liquidadas (las que mueven la banca).
El veredicto de la mente sale de las predicciones liquidadas: PASAR
nunca llega a ser una apuesta, así que no está en la banca.

Fuente de la cuota
-------------------
``fuente_momio`` guarda el nombre de la casa. ``estimado`` solo aparece
en dinero viejo, cuando un precio de respaldo sí se apostó. Hoy un
precio estimado no se convierte en apuesta: el pick queda
``sin_momio_real`` / registrado sin apuesta y no entra al ROI.
Si no hay ``fuente_momio``, se usa ``lineas_fuente``. Si tampoco hay
fuente, la apuesta queda en ``sin_dato``.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

# Dinero viejo con precio de respaldo, o historial sin casa (``lineas_fuente``).
# ``sin_momio_real`` no está aquí: no es una apuesta.
_FUENTES_ESTIMADAS = frozenset(
    {
        "",
        "estimado",
        "estimada",
        "estimados",
        "modelo",
        "none",
        "null",
        "import",
        "sintetico",
        "sintetica",
        "fair",
        "vig",
    }
)
# Orden de búsqueda. ``fuente_momio`` es el campo nuevo; el resto son
# alias por si el nombre cambia antes de mergear.
_CAMPOS_FUENTE = (
    "fuente_momio",
    "odds_source",
    "fuente_cuota",
    "origen_momio",
    "fuente_odds",
)
_ORDEN_TIPO = (
    "scratch",
    "underdog",
    "favorito_alto",
    "favorito",
    "favorite",
    "limpio",
)
_ETIQUETA_TIPO = {
    "scratch": "Scratch",
    "underdog": "Underdog",
    "favorito_alto": "Favorito",
    "favorito": "Favorito",
    "favorite": "Favorito",
    "limpio": "Limpio",
    "sin_tipo": "Sin tipo",
}
_ORDEN_MENTE = ("APOSTAR", "PASAR", "ESPERAR")
_GRACIA_MIN = 5.0


def calcular_resultados(memoria: dict | None) -> dict[str, Any]:
    """Resumen del experimento. No modifica ``memoria``."""
    memoria = memoria if isinstance(memoria, dict) else {}
    inicial = _num(memoria.get("capital_inicial"), 100.0)
    if inicial < 0:
        inicial = 0.0
    stake_papel = _num(memoria.get("stake_por_juego"), 3.0)
    if stake_papel <= 0:
        stake_papel = 3.0

    dias = [d for d in (memoria.get("dias") or []) if isinstance(d, dict)]
    dias_ord = sorted(
        enumerate(dias),
        key=lambda iv: (str(iv[1].get("fecha") or "9999-99-99"), iv[0]),
    )

    capital = inicial
    curva: list[dict[str, Any]] = [
        {
            "fecha": None,
            "dia": 0,
            "capital": round(inicial, 2),
            "profit_dia": 0.0,
            "n": 0,
        }
    ]
    equity = [inicial]
    estados: list[str] = []
    tipos: dict[str, dict[str, float]] = {}
    fuentes: dict[str, dict[str, float]] = {
        "real": _vacio(),
        "estimado": _vacio(),
        "sin_dato": _vacio(),
    }
    ganadas = perdidas = pendientes = sin_apuesta = 0
    profit = stake = 0.0

    for _, dia in dias_ord:
        profit_dia = 0.0
        n_dia = 0
        apuestas = [a for a in (dia.get("apuestas") or []) if isinstance(a, dict)]
        for apuesta in apuestas:
            if _es_sin_apuesta(apuesta):
                sin_apuesta += 1
                continue
            estado = str(apuesta.get("estado") or "").strip().lower()
            if estado == "pendiente":
                pendientes += 1
                continue
            if estado not in ("ganada", "perdida"):
                continue
            p = _num(apuesta.get("profit"), 0.0)
            s = _num(apuesta.get("stake"), 0.0)
            if s < 0:
                s = 0.0
            profit_dia += p
            n_dia += 1
            profit += p
            stake += s
            capital += p
            equity.append(capital)
            estados.append(estado)
            if estado == "ganada":
                ganadas += 1
            else:
                perdidas += 1
            _sumar(tipos.setdefault(_tipo(apuesta), _vacio()), estado, p, s)
            _sumar(fuentes[clasificar_fuente_cuota(apuesta)], estado, p, s)
        curva.append(
            {
                "fecha": dia.get("fecha") or None,
                "dia": dia.get("dia"),
                "capital": round(capital, 2),
                "profit_dia": round(profit_dia, 2),
                "n": n_dia,
            }
        )

    bloque = _bloque(ganadas, perdidas, profit, stake)
    roi_banca = round(100.0 * (capital - inicial) / inicial, 1) if inicial else None
    resumen = {
        "capital_inicial": round(inicial, 2),
        "capital": round(capital, 2),
        "roi_banca_pct": roi_banca,
        **bloque,
        "pendientes": pendientes,
    }
    por_tipo = [
        {"tipo": clave, "etiqueta": _etiqueta_tipo(clave), **_bloque_de(acc)}
        for clave, acc in sorted(tipos.items(), key=lambda kv: (_orden_tipo(kv[0]), kv[0]))
    ]
    return {
        "ok": True,
        "capital_inicial": round(inicial, 2),
        "curva": curva,
        "resumen": resumen,
        "por_tipo": por_tipo,
        "racha": _racha(estados),
        "drawdown": _drawdown(equity),
        "por_fuente": {
            "real": {"etiqueta": "Casa real", **_bloque_de(fuentes["real"])},
            "estimado": {"etiqueta": "Estimada", **_bloque_de(fuentes["estimado"])},
            "sin_dato": {"etiqueta": "Sin dato", **_bloque_de(fuentes["sin_dato"])},
        },
        "mente": _mente(dias_ord, stake_papel),
        "registrados_sin_apuesta": sin_apuesta,
    }


def _es_sin_apuesta(apuesta: dict | None) -> bool:
    """Pick registrado sin casa. No movió la banca."""
    if not isinstance(apuesta, dict):
        return False
    if apuesta.get("sin_momio_real"):
        return True
    if str(apuesta.get("estado_registro") or "").strip().lower() == "registrado sin apuesta":
        return True
    fuente = str(apuesta.get("fuente_momio") or apuesta.get("lineas_fuente") or "").strip().lower()
    return fuente == "sin_momio_real"


def clasificar_fuente_cuota(apuesta: dict | None) -> str:
    """``real``, ``estimado``, ``sin_dato`` o ``sin_apuesta``.

    ``fuente_momio`` (o un alias) manda. Si no viene, cae a ``lineas_fuente``.
    ``sin_momio_real`` no es una apuesta y no entra a ninguna tarjeta de ROI.
    ``estimado`` sigue siendo dinero viejo liquidado con precio de respaldo.
    """
    if not isinstance(apuesta, dict):
        return "sin_dato"
    if _es_sin_apuesta(apuesta):
        return "sin_apuesta"
    raw = _leer_fuente_explicita(apuesta)
    if raw is None:
        if "lineas_fuente" not in apuesta:
            return "sin_dato"
        bruto = apuesta.get("lineas_fuente")
        if bruto is None or str(bruto).strip() == "":
            return "sin_dato"
        raw = bruto
    return "estimado" if _es_estimada(raw) else "real"


def _leer_fuente_explicita(apuesta: dict) -> Any:
    for clave in _CAMPOS_FUENTE:
        if clave not in apuesta:
            continue
        bruto = apuesta.get(clave)
        if bruto is None or str(bruto).strip() == "":
            continue
        return bruto
    return None


def _es_estimada(raw: Any) -> bool:
    return str(raw).strip().lower() in _FUENTES_ESTIMADAS


def _tipo(apuesta: dict) -> str:
    bruto = apuesta.get("tipo_pick")
    if bruto is None or str(bruto).strip() == "":
        intel = apuesta.get("inteligencia")
        if isinstance(intel, dict):
            bruto = intel.get("tipo_pick")
    texto = str(bruto or "").strip().lower()
    if texto in ("", "none", "null"):
        return "sin_tipo"
    return texto


def _etiqueta_tipo(clave: str) -> str:
    if clave in _ETIQUETA_TIPO:
        return _ETIQUETA_TIPO[clave]
    limpio = clave.replace("_", " ").strip()
    return limpio[:1].upper() + limpio[1:] if limpio else "Sin tipo"


def _orden_tipo(clave: str) -> int:
    try:
        return _ORDEN_TIPO.index(clave)
    except ValueError:
        return 900 if clave == "sin_tipo" else 500


def _mente(dias_ord: list[tuple[int, dict]], stake_papel: float) -> dict[str, Any]:
    grupos: dict[str, dict[str, float]] = {}
    for _, dia in dias_ord:
        for pred in dia.get("predicciones") or []:
            if not isinstance(pred, dict):
                continue
            if not _prediccion_cuenta(pred):
                continue
            decision = _decision_mente(pred)
            if decision is None:
                continue
            priced = _profit_papel(pred, stake_papel)
            if priced is None:
                continue
            p, s = priced
            estado = "ganada" if pred.get("resultado") == "acierto" else "perdida"
            _sumar(grupos.setdefault(decision, _vacio()), estado, p, s)
    veredictos = [
        {"decision": decision, "etiqueta": decision, **_bloque_de(grupos[decision])}
        for decision in _ORDEN_MENTE
        if decision in grupos
    ]
    for decision, acc in sorted(grupos.items()):
        if decision in _ORDEN_MENTE:
            continue
        veredictos.append(
            {"decision": decision, "etiqueta": decision, **_bloque_de(acc)}
        )
    return {"disponible": bool(veredictos), "veredictos": veredictos}


def _decision_mente(pred: dict) -> str | None:
    for clave in ("ia_mente", "ia_veto"):
        bloque = pred.get(clave)
        if not isinstance(bloque, dict):
            continue
        decision = str(bloque.get("decision") or "").strip().upper()
        if decision in _ORDEN_MENTE:
            return decision
    return None


def _prediccion_cuenta(pred: dict) -> bool:
    if _es_sin_apuesta(pred):
        return False
    if pred.get("estado") != "liquidado":
        return False
    if pred.get("resultado") not in ("acierto", "fallo"):
        return False
    if pred.get("valida_stats") is False or pred.get("invalida_tarde") or pred.get("retroactivo"):
        return False
    predicho = _iso(pred.get("predicho_en"))
    inicio = _iso(pred.get("inicio_juego"))
    if predicho and inicio:
        mins = (predicho - inicio).total_seconds() / 60.0
        if mins > _GRACIA_MIN:
            return False
    return True


def _profit_papel(pred: dict, stake_papel: float) -> tuple[float, float] | None:
    stake = pred.get("stake_virtual")
    s = _num(stake, stake_papel) if stake not in (None, "") else stake_papel
    if s < 0:
        s = 0.0
    if pred.get("profit") is not None:
        return _num(pred.get("profit"), 0.0), s
    if pred.get("resultado") == "fallo":
        return round(-s, 2), s
    odds = _num(pred.get("odds"), 0.0)
    if odds <= 1.0:
        return None
    return round(s * (odds - 1.0), 2), s


def _iso(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _vacio() -> dict[str, float]:
    return {"ganadas": 0.0, "perdidas": 0.0, "profit": 0.0, "stake": 0.0}


def _sumar(acc: dict[str, float], estado: str, profit: float, stake: float) -> None:
    if estado == "ganada":
        acc["ganadas"] += 1
    else:
        acc["perdidas"] += 1
    acc["profit"] += profit
    acc["stake"] += stake


def _bloque_de(acc: dict[str, float]) -> dict[str, Any]:
    return _bloque(int(acc["ganadas"]), int(acc["perdidas"]), acc["profit"], acc["stake"])


def _bloque(ganadas: int, perdidas: int, profit: float, stake: float) -> dict[str, Any]:
    n = ganadas + perdidas
    win = round(100.0 * ganadas / n, 1) if n else 0.0
    roi = round(100.0 * profit / stake, 1) if stake > 0 else None
    return {
        "n": n,
        "ganadas": ganadas,
        "perdidas": perdidas,
        "record": f"{ganadas}-{perdidas}",
        "win_rate": win,
        "profit": round(profit, 2),
        "stake": round(stake, 2),
        "roi_pct": roi,
    }


def _racha(estados: list[str]) -> dict[str, Any]:
    if not estados:
        return {"tipo": None, "n": 0, "texto": "Sin racha"}
    ultimo = estados[-1]
    n = 0
    for estado in reversed(estados):
        if estado != ultimo:
            break
        n += 1
    if ultimo == "ganada":
        palabra = "ganada" if n == 1 else "ganadas"
        tipo = "ganada"
    else:
        palabra = "perdida" if n == 1 else "perdidas"
        tipo = "perdida"
    return {"tipo": tipo, "n": n, "texto": f"{n} {palabra}"}


def _drawdown(equity: list[float]) -> dict[str, Any]:
    if not equity:
        return {"max_usd": 0.0, "max_pct": 0.0, "pico": 0.0, "valle": 0.0}
    pico = equity[0]
    max_usd = 0.0
    max_pct = 0.0
    pico_dd = pico
    valle_dd = pico
    for capital in equity:
        if capital > pico:
            pico = capital
        dd = pico - capital
        if dd > max_usd + 1e-9:
            max_usd = dd
            pico_dd = pico
            valle_dd = capital
            max_pct = (dd / pico * 100.0) if pico else 0.0
    return {
        "max_usd": round(max_usd, 2),
        "max_pct": round(max_pct, 1),
        "pico": round(pico_dd, 2),
        "valle": round(valle_dd, 2),
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        n = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(n):
        return default
    return n
