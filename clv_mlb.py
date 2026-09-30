"""Closing Line Value (CLV) para MLB.

Hay dos lecturas:

- La histórica (`clv_pct`) compara la entrada con el cierre justo (sin vig)
  de Pinnacle o de la mediana de casas. La sigue usando el panel de la mente.
- La de cada apuesta (`clv`) guarda el precio tomado, el moneyline de cierre
  del mismo lado y la casa. Es solo medición: no decide si se apuesta.

`clv` medido es un dict. Si no hubo un precio de cierre real, `clv` es el
texto ``sin_cierre`` y `clv_motivo` dice por qué. No se inventa un número.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable

from inteligencia_mlb import odds_justas_sin_vig

# La foto de cierre se toma entre T-5 y el primer lanzamiento.
# El catch-up al despertar todavía vale ~1 min después de la hora programada
# si el partido sigue en pre-game: la cadena rechaza el momio en vivo.
VENTANA_CIERRE_MIN = 5.0
GRACIA_CIERRE_MIN = 1.0
_REFRESCO_CIERRE_SEG = 90.0

_FUENTES_SIN_PRECIO = frozenset(
    {
        "",
        "modelo",
        "none",
        "null",
        "import",
        "estimado",
        "estimada",
        "sin_momio_real",
        "sintetico",
        "sintetica",
        "fair",
        "vig",
    }
)
_ESTADOS_DINERO_CERRADOS = frozenset({"ganada", "perdida", "push", "anulada", "void"})
_ESTADOS_JUEGO_CERRADO = frozenset(
    {"EN VIVO", "FINALIZADO", "CANCELADO", "SUSPENDIDO", "SUSPENDIDO_OFICIAL"}
)
_CAMPOS_LADO = (
    "pick",
    "pick_lado",
    "pick_abbr",
    "pick_team_id",
    "away_id",
    "home_id",
    "away_abbr",
    "home_abbr",
    "visitante",
    "home",
)


def cuotas_pinnacle(juego: dict[str, Any]) -> tuple[float | None, float | None]:
    """Decimal away/home de Pinnacle si está en lineas_libros o fuente principal."""
    libros = juego.get("lineas_libros") if isinstance(juego.get("lineas_libros"), list) else []
    for b in libros:
        if not isinstance(b, dict):
            continue
        if str(b.get("casa") or "").lower() != "pinnacle":
            continue
        try:
            away = float(b.get("away") or 0)
            home = float(b.get("home") or 0)
        except (TypeError, ValueError):
            continue
        if away > 1.0 and home > 1.0:
            return away, home
    fuente = str(juego.get("lineas_fuente") or "").lower()
    if fuente == "pinnacle":
        try:
            away = float(juego.get("odds_away_decimal") or juego.get("odds") or 0)
            home = float(juego.get("odds_home_decimal") or 0)
        except (TypeError, ValueError):
            return None, None
        if away > 1.0 and home > 1.0:
            return away, home
    return None, None


def _mediana(valores: list[float]) -> float:
    ordenados = sorted(valores)
    medio = len(ordenados) // 2
    if len(ordenados) % 2:
        return ordenados[medio]
    return (ordenados[medio - 1] + ordenados[medio]) / 2.0


def _dos_lados(juego: dict[str, Any]) -> tuple[float | None, float | None]:
    try:
        away = float(juego.get("odds_away_decimal") or 0)
        home = float(juego.get("odds_home_decimal") or 0)
    except (TypeError, ValueError):
        return None, None
    if away > 1.0 and home > 1.0:
        return away, home
    return None, None


def cuotas_cierre(juego: dict[str, Any]) -> tuple[float | None, float | None, str]:
    """Cierre de referencia para medir el CLV.

    Pinnacle si aparece; si no, la mediana de las casas disponibles; y si solo
    hay una casa (hoy ESPN publica únicamente DraftKings), el precio de esa
    misma casa antes del juego. Sin vigorish, comparar la entrada contra ese
    cierre mide si la línea se movió a favor o en contra.
    """
    away, home = cuotas_pinnacle(juego)
    if away and home:
        return away, home, "pinnacle"
    libros = juego.get("lineas_libros") if isinstance(juego.get("lineas_libros"), list) else []
    aways: list[float] = []
    homes: list[float] = []
    casas: list[str] = []
    for b in libros:
        if not isinstance(b, dict):
            continue
        try:
            a = float(b.get("away") or 0)
            h = float(b.get("home") or 0)
        except (TypeError, ValueError):
            continue
        if a > 1.0 and h > 1.0:
            aways.append(a)
            homes.append(h)
            casas.append(str(b.get("casa") or "casa"))
    if len(aways) >= 2:
        return _mediana(aways), _mediana(homes), f"mediana_{len(aways)}_casas"
    if len(aways) == 1:
        return aways[0], homes[0], f"casa_unica_{casas[0]}"
    away, home = _dos_lados(juego)
    if away and home:
        casa = str(juego.get("lineas_fuente") or "casa")
        return away, home, f"casa_unica_{casa}"
    return None, None, "sin_cierre"


def cuota_pick_decimal(
    pick: str,
    visitante: str,
    home: str,
    dec_away: float,
    dec_home: float,
) -> float | None:
    p = (pick or "").strip()
    if visitante and visitante in p:
        return dec_away
    if home and home in p:
        return dec_home
    return None


def clv_pct(
    odds_entrada: float,
    dec_away: float,
    dec_home: float,
    pick: str,
    visitante: str,
    home: str,
) -> float | None:
    """
    CLV% = (odds_entrada / fair_decimal_cierre - 1) × 100
    fair_decimal = cuota justa sin vig del lado apostado (Pinnacle cierre).
    """
    try:
        ent = float(odds_entrada)
        da = float(dec_away)
        dh = float(dec_home)
    except (TypeError, ValueError):
        return None
    if ent <= 1.0 or da <= 1.0 or dh <= 1.0:
        return None
    fair = odds_justas_sin_vig(da, dh)
    if not fair.get("ok"):
        return None
    p = (pick or "").strip()
    if visitante and visitante in p:
        fair_dec = float(fair["dec_away_fair"])
    elif home and home in p:
        fair_dec = float(fair["dec_home_fair"])
    else:
        return None
    if fair_dec <= 1.0:
        return None
    return round((ent / fair_dec - 1.0) * 100.0, 2)


def actualizar_clv_registro(
    reg: dict[str, Any],
    juego: dict[str, Any],
    *,
    fase: str = "entrada",
) -> bool:
    """
    fase=entrada: congela odds de entrada vs Pinnacle del momento.
    fase=cierre: snapshot pre-partido (T-45/T-30 o último refresh).
    """
    if not isinstance(reg, dict) or not isinstance(juego, dict):
        return False
    away, home, fuente_cierre = cuotas_cierre(juego)
    if not away or not home:
        return False
    reg["clv_fuente"] = fuente_cierre
    pick = reg.get("pick") or juego.get("pick") or ""
    visitante = reg.get("visitante") or juego.get("visitante") or ""
    home_name = reg.get("home") or juego.get("home") or ""
    now = datetime.now().isoformat()

    if fase == "entrada":
        odds_in = reg.get("odds") or reg.get("odds_congelada") or juego.get("odds")
        try:
            odds_in_f = float(odds_in or 0)
        except (TypeError, ValueError):
            return False
        if odds_in_f <= 1.0:
            return False
        reg["clv_odds_entrada"] = odds_in_f
        reg["clv_pin_entrada_away"] = away
        reg["clv_pin_entrada_home"] = home
        reg["clv_entrada_en"] = now
        clv = clv_pct(odds_in_f, away, home, pick, visitante, home_name)
        if clv is not None:
            reg["clv_entrada_pct"] = clv
        return True

    if fase == "cierre":
        reg["clv_pin_cierre_away"] = away
        reg["clv_pin_cierre_home"] = home
        reg["clv_cierre_en"] = now
        odds_in = reg.get("clv_odds_entrada") or reg.get("odds") or reg.get("odds_congelada")
        try:
            odds_in_f = float(odds_in or 0)
        except (TypeError, ValueError):
            return False
        if odds_in_f <= 1.0:
            return True
        clv = clv_pct(odds_in_f, away, home, pick, visitante, home_name)
        if clv is not None:
            reg["clv_pct"] = clv
        return True

    return False


def resumen_clv_memoria(memoria: dict) -> dict[str, Any]:
    """Promedio CLV cierre y entrada para panel."""
    cierre: list[float] = []
    entrada: list[float] = []
    con_dinero: list[float] = []

    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        for reg in (dia.get("predicciones") or []) + (dia.get("apuestas") or []):
            if not isinstance(reg, dict):
                continue
            if reg.get("clv_pct") is not None:
                try:
                    v = float(reg["clv_pct"])
                    cierre.append(v)
                    if reg.get("con_dinero") or reg.get("estado") in ("ganada", "perdida", "pendiente"):
                        if reg in (dia.get("apuestas") or []):
                            con_dinero.append(v)
                except (TypeError, ValueError):
                    pass
            if reg.get("clv_entrada_pct") is not None:
                try:
                    entrada.append(float(reg["clv_entrada_pct"]))
                except (TypeError, ValueError):
                    pass

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 2) if xs else None

    avg = _avg(cierre)
    return {
        "muestras_cierre": len(cierre),
        "muestras_entrada": len(entrada),
        "muestras_dinero": len(con_dinero),
        "clv_promedio": avg,
        "clv_entrada_promedio": _avg(entrada),
        "clv_dinero_promedio": _avg(con_dinero),
        "clv_positivo_pct": round(100 * sum(1 for x in cierre if x > 0) / len(cierre), 1) if cierre else None,
        "nivel": (
            "ok" if avg is not None and avg >= 1.5
            else "aviso" if avg is not None and avg >= 0
            else "alerta" if avg is not None
            else "sin_datos"
        ),
        "mensaje": _mensaje_clv(avg, len(cierre), len(con_dinero)),
    }


def _mensaje_clv(avg: float | None, n: int, n_din: int) -> str:
    if avg is None or n == 0:
        return "CLV: sin muestras Pinnacle aún"
    sign = "+" if avg >= 0 else ""
    base = f"CLV medio {sign}{avg:.1f}% ({n} picks"
    if n_din:
        base += f", {n_din} dinero"
    base += ") vs cierre Pinnacle"
    if avg >= 2.0:
        base += " · edge sharp"
    elif avg < 0:
        base += " · peor que el cierre"
    return base


def _norm_casa(nombre: Any) -> str:
    return str(nombre or "").lower().replace(" ", "").replace("_", "")


def _marca(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        dt = valor
    elif not valor:
        return None
    else:
        try:
            dt = datetime.fromisoformat(str(valor).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fuente_real(reg: dict) -> str:
    for clave in ("fuente_momio", "casa_momio", "casa", "lineas_fuente"):
        raw = str(reg.get(clave) or "").strip()
        if raw and raw.lower() not in _FUENTES_SIN_PRECIO:
            return raw
    return ""


def es_apuesta_dinero(reg: dict | None) -> bool:
    """Apuesta que sí movió (o va a mover) la banca. El pick sin casa no entra."""
    if not isinstance(reg, dict):
        return False
    if reg.get("sin_momio_real"):
        return False
    if str(reg.get("estado_registro") or "").strip().lower() == "registrado sin apuesta":
        return False
    try:
        stake = float(reg.get("stake") or 0)
    except (TypeError, ValueError):
        return False
    if stake <= 0:
        return False
    fuente = str(reg.get("fuente_momio") or reg.get("lineas_fuente") or "").strip().lower()
    return fuente != "sin_momio_real"


def es_pick_sin_apuesta(reg: dict | None) -> bool:
    """Pick registrado que no es una apuesta de dinero."""
    if not isinstance(reg, dict):
        return False
    if reg.get("con_dinero"):
        return False
    if es_apuesta_dinero(reg) and reg.get("stake") not in (None, "", 0):
        return False
    if reg.get("sin_momio_real"):
        return True
    if str(reg.get("estado_registro") or "").strip().lower() == "registrado sin apuesta":
        return True
    return bool(str(reg.get("pick") or "").strip())


def clv_medido(reg: dict | None) -> bool:
    if not isinstance(reg, dict):
        return False
    clv = reg.get("clv")
    return (
        isinstance(clv, dict)
        and clv.get("clv_pp") is not None
        and clv.get("precio_cierre") not in (None, "")
    )


def marcar_sin_cierre(reg: dict, motivo: str) -> bool:
    """Deja `clv: sin_cierre`. No escribe un precio inventado."""
    if not isinstance(reg, dict):
        return False
    if clv_medido(reg):
        return False
    motivo_l = (motivo or "sin precio de cierre").strip()[:180]
    if reg.get("clv") == "sin_cierre" and reg.get("clv_motivo") == motivo_l:
        return False
    reg["clv"] = "sin_cierre"
    reg["clv_motivo"] = motivo_l
    return True


def precio_entrada(reg: dict) -> tuple[float | None, int | None, str]:
    """Decimal congelado, americano y casa. El estimado no es precio de apuesta."""
    if not isinstance(reg, dict):
        return None, None, ""
    if reg.get("sin_momio_real"):
        return None, None, ""
    fuente = str(reg.get("fuente_momio") or reg.get("lineas_fuente") or "").strip().lower()
    if fuente in _FUENTES_SIN_PRECIO:
        return None, None, ""
    try:
        dec = float(reg.get("odds") or reg.get("odds_congelada") or 0)
    except (TypeError, ValueError):
        return None, None, ""
    if dec <= 1.0:
        return None, None, ""
    amer = reg.get("odds_american")
    try:
        from cadena_momios import parsear_momio_americano

        amer_n = parsear_momio_americano(amer, registrar=False)
    except Exception:
        amer_n = None
    return dec, amer_n, _fuente_real(reg)


def medir_clv_precios(
    dec_entrada: float,
    dec_cierre: float,
    dec_otro: float | None = None,
) -> dict[str, Any] | None:
    """CLV en puntos de probabilidad y en % de cuota.

    Los puntos usan la probabilidad sin vig del cierre cuando existen los dos
    lados. El % compara la cuota decimal del lado, con el vig de la casa:
    positivo si el precio tomado paga más que el cierre.
    """
    try:
        ent = float(dec_entrada)
        cierre = float(dec_cierre)
    except (TypeError, ValueError):
        return None
    if ent <= 1.0 or cierre <= 1.0:
        return None
    p_ent = 1.0 / ent
    novig = False
    try:
        otro = float(dec_otro) if dec_otro not in (None, "") else 0.0
    except (TypeError, ValueError):
        otro = 0.0
    if otro > 1.0:
        p_c = 1.0 / cierre
        p_o = 1.0 / otro
        total = p_c + p_o
        if total <= 0:
            return None
        p_close = p_c / total
        novig = True
    else:
        p_close = 1.0 / cierre
    return {
        "clv_pp": round((p_close - p_ent) * 100.0, 2),
        "clv_pct": round((ent / cierre - 1.0) * 100.0, 2),
        "novig": novig,
    }


def _lado_registro(reg: dict, juego: dict | None) -> str:
    from cadena_momios import lado_del_pick

    base = dict(juego or {})
    for clave in _CAMPOS_LADO:
        if reg.get(clave) not in (None, ""):
            base[clave] = reg.get(clave)
    return lado_del_pick(base, str(reg.get("pick") or base.get("pick") or ""))


def _libros_de(juego: dict | None) -> list[dict]:
    if not isinstance(juego, dict):
        return []
    from cadena_momios import _libros_juego

    libros = _libros_juego(juego)
    return [b for b in libros if isinstance(b, dict)]


def elegir_libro_cierre(libros: list[dict], casa_preferida: str) -> tuple[dict | None, str]:
    """La casa de la apuesta si pasa las protecciones; si no, la siguiente real."""
    from cadena_momios import _motivo_si_no_apto, _precio_libro

    preferida = _norm_casa(casa_preferida)
    aptos: list[dict] = []
    ultimo = ""
    for libro in libros:
        if not isinstance(libro, dict):
            continue
        motivo = _motivo_si_no_apto(libro)
        if motivo:
            ultimo = motivo
            continue
        if not libro.get("fetched_at"):
            ultimo = "sin marca de hora del fetch"
            continue
        precio = _precio_libro(libro)
        if not precio:
            ultimo = "moneyline fuera de rango o incompleto"
            continue
        aptos.append(precio)
    if not aptos:
        return None, ultimo or "ninguna casa con moneyline de cierre"
    if preferida:
        for precio in aptos:
            if _norm_casa(precio.get("fuente_momio") or precio.get("casa_momio")) == preferida:
                return precio, ""
    return aptos[0], ""


def _aplicar_medicion(
    reg: dict,
    *,
    serie: str,
    dec_entrada: float,
    amer_entrada: int | None,
    casa_apuesta: str,
    precio: dict,
    lado: str,
    cuando: datetime,
) -> bool:
    if lado == "away":
        dec_cierre = precio.get("odds_away_decimal")
        amer_cierre = precio.get("odds_away_american")
        dec_otro = precio.get("odds_home_decimal")
    elif lado == "home":
        dec_cierre = precio.get("odds_home_decimal")
        amer_cierre = precio.get("odds_home_american")
        dec_otro = precio.get("odds_away_decimal")
    else:
        return marcar_sin_cierre(reg, "no se pudo identificar el lado del pick")
    medicion = medir_clv_precios(dec_entrada, dec_cierre, dec_otro)
    if medicion is None:
        return marcar_sin_cierre(reg, "precio de cierre fuera de rango o incompleto")
    casa_cierre = str(precio.get("casa_momio") or precio.get("fuente_momio") or "")
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=timezone.utc)
    reg["clv"] = {
        "precio_apuesta": round(float(dec_entrada), 3),
        "precio_apuesta_americano": amer_entrada,
        "precio_cierre": round(float(dec_cierre), 3),
        "precio_cierre_americano": amer_cierre,
        "casa_cierre": casa_cierre,
        "casa_apuesta": casa_apuesta,
        "misma_casa": bool(casa_apuesta) and _norm_casa(casa_apuesta) == _norm_casa(casa_cierre),
        "timestamp": cuando.astimezone(timezone.utc).isoformat(),
        "clv_pp": medicion["clv_pp"],
        "clv_pct": medicion["clv_pct"],
        "novig": bool(medicion["novig"]),
        "serie": serie,
    }
    reg.pop("clv_motivo", None)
    return True


def _casa_de_fuente_guardada(fuente: Any) -> str | None:
    texto = str(fuente or "").strip()
    if not texto or texto == "sin_cierre" or texto.startswith("mediana"):
        return None
    if texto.startswith("casa_unica_"):
        return texto[len("casa_unica_") :]
    return texto


def cierre_guardado(reg: dict) -> dict[str, Any] | None:
    """Snapshot ya persistido, solo si se tomó en la ventana de cierre.

    El precio de la apuesta y un cierre de T-60 no cuentan: no son el cierre.
    """
    if not isinstance(reg, dict):
        return None
    if clv_medido(reg):
        return reg.get("clv") if isinstance(reg.get("clv"), dict) else None
    try:
        away = float(reg.get("clv_pin_cierre_away") or 0)
        home = float(reg.get("clv_pin_cierre_home") or 0)
    except (TypeError, ValueError):
        return None
    if away <= 1.0 or home <= 1.0:
        return None
    casa = _casa_de_fuente_guardada(reg.get("clv_fuente"))
    if not casa:
        return None
    cuando = _marca(reg.get("clv_cierre_en"))
    inicio = _marca(reg.get("inicio_juego"))
    if cuando is None or inicio is None:
        return None
    mins = (inicio - cuando).total_seconds() / 60.0
    if mins < -GRACIA_CIERRE_MIN or mins > VENTANA_CIERRE_MIN:
        return None
    return {
        "odds_away_decimal": away,
        "odds_home_decimal": home,
        "odds_away_american": None,
        "odds_home_american": None,
        "fuente_momio": casa,
        "casa_momio": casa,
        "fetched_at": cuando.isoformat(),
        "timestamp": cuando,
    }


def _minutos_hasta(juego: dict | None, reg: dict, ahora: datetime) -> float | None:
    inicio = _marca((juego or {}).get("inicio_juego") or reg.get("inicio_juego"))
    if inicio is None:
        return None
    ahora_u = _marca(ahora) or datetime.now(timezone.utc)
    return (inicio - ahora_u).total_seconds() / 60.0


def _fase_ventana(juego: dict | None, reg: dict, ahora: datetime) -> str:
    """'temprano', 'abierta', 'cerrada' o 'pospuesto'."""
    estado = str((juego or {}).get("estado") or "").strip().upper()
    if estado == "POSPUESTO":
        return "pospuesto"
    mins = _minutos_hasta(juego, reg, ahora)
    if estado in _ESTADOS_JUEGO_CERRADO:
        return "cerrada"
    if mins is None:
        return "temprano"
    if -GRACIA_CIERRE_MIN < mins <= VENTANA_CIERRE_MIN and estado in ("", "PROGRAMADO"):
        return "abierta"
    if mins > VENTANA_CIERRE_MIN:
        return "temprano"
    return "cerrada"


def _cierre_reciente(reg: dict, ahora: datetime) -> bool:
    if not clv_medido(reg):
        return False
    cuando = _marca(reg["clv"].get("timestamp"))
    if cuando is None:
        return False
    ahora_u = _marca(ahora) or datetime.now(timezone.utc)
    return abs((ahora_u - cuando).total_seconds()) < _REFRESCO_CIERRE_SEG


def _motivo_fetch(juego: dict | None) -> str:
    if not isinstance(juego, dict):
        return "sin precio de cierre"
    fallos = str(juego.get("momio_fallos") or "").strip()
    if fallos:
        return fallos[:180]
    intentos = juego.get("momio_intentos")
    if isinstance(intentos, list) and intentos:
        ultimo = intentos[-1]
        if isinstance(ultimo, dict) and ultimo.get("motivo"):
            return str(ultimo["motivo"])[:180]
    return "sin precio de cierre antes del primer pitch"


def _liquidada_dinero(reg: dict) -> bool:
    return str(reg.get("estado") or "").strip().lower() in _ESTADOS_DINERO_CERRADOS


def rellenar_clv_historico(memoria: dict | None) -> int:
    """Las apuestas ya liquidadas solo usan un cierre que ya esté guardado.

    Si no hay esa foto, quedan `sin_cierre`. No sale a la red ni copia el
    precio de la entrada.
    """
    if not isinstance(memoria, dict):
        return 0
    cambios = 0
    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        for apuesta in dia.get("apuestas") or []:
            if not isinstance(apuesta, dict) or not es_apuesta_dinero(apuesta):
                continue
            if not _liquidada_dinero(apuesta):
                continue
            if clv_medido(apuesta) or apuesta.get("clv") == "sin_cierre":
                continue
            guardado = cierre_guardado(apuesta)
            if not guardado:
                if marcar_sin_cierre(apuesta, "sin precio de cierre guardado"):
                    cambios += 1
                continue
            dec, amer, casa = precio_entrada(apuesta)
            if dec is None:
                if marcar_sin_cierre(apuesta, "sin precio de entrada real"):
                    cambios += 1
                continue
            lado = _lado_registro(apuesta, apuesta)
            cuando = guardado.get("timestamp") if isinstance(guardado.get("timestamp"), datetime) else _marca(
                guardado.get("fetched_at")
            )
            if _aplicar_medicion(
                apuesta,
                serie="dinero",
                dec_entrada=dec,
                amer_entrada=amer,
                casa_apuesta=casa,
                precio=guardado,
                lado=lado,
                cuando=cuando or datetime.now(timezone.utc),
            ):
                cambios += 1
    return cambios


def _procesar_registro(
    reg: dict,
    *,
    serie: str,
    juego: dict | None,
    ahora: datetime,
    fetcher: Callable[[dict], dict] | None,
    cache: dict[str, dict | None],
    errores: dict[str, str],
) -> int:
    fase = _fase_ventana(juego, reg, ahora)
    if fase == "pospuesto" and not _liquidada_dinero(reg):
        return 0
    if fase == "temprano":
        return 0
    if clv_medido(reg) and (fase != "abierta" or _cierre_reciente(reg, ahora)):
        return 0
    if reg.get("clv") == "sin_cierre" and fase != "abierta":
        return 0

    dec, amer, casa = precio_entrada(reg)
    if dec is None:
        if fase in ("abierta", "cerrada"):
            return 1 if marcar_sin_cierre(reg, "sin precio de entrada real") else 0
        return 0

    if fase == "abierta" and fetcher is not None and isinstance(juego, dict):
        gid = str(juego.get("id") or juego.get("game_id") or reg.get("game_id") or "")
        if gid not in cache:
            try:
                cache[gid] = fetcher(copy.deepcopy(juego))
            except Exception as e:
                cache[gid] = None
                errores[gid] = str(e)[:180]
        snapshot = cache.get(gid)
        if isinstance(snapshot, dict):
            precio, motivo = elegir_libro_cierre(_libros_de(snapshot), casa)
            if precio:
                cuando = _marca(precio.get("fetched_at")) or ahora
                lado = _lado_registro(reg, snapshot)
                antes = copy.deepcopy(reg.get("clv"))
                if _aplicar_medicion(
                    reg,
                    serie=serie,
                    dec_entrada=dec,
                    amer_entrada=amer,
                    casa_apuesta=casa,
                    precio=precio,
                    lado=lado,
                    cuando=cuando if isinstance(cuando, datetime) else ahora,
                ):
                    return 0 if reg.get("clv") == antes else 1
            else:
                errores[gid] = motivo or _motivo_fetch(snapshot)
        if fase == "abierta":
            return 0

    if fase == "cerrada":
        guardado = cierre_guardado(reg)
        if guardado and not clv_medido(reg):
            lado = _lado_registro(reg, juego or reg)
            cuando = guardado.get("timestamp") if isinstance(guardado.get("timestamp"), datetime) else ahora
            if _aplicar_medicion(
                reg,
                serie=serie,
                dec_entrada=dec,
                amer_entrada=amer,
                casa_apuesta=casa,
                precio=guardado,
                lado=lado,
                cuando=cuando if isinstance(cuando, datetime) else ahora,
            ):
                return 1
        gid = str(reg.get("game_id") or "")
        motivo = errores.get(gid) or "sin precio de cierre antes del primer pitch"
        return 1 if marcar_sin_cierre(reg, motivo) else 0
    return 0


def sincronizar_clv(
    memoria: dict | None,
    juegos: list[dict] | None,
    *,
    ahora: datetime | None = None,
    fetcher: Callable[[dict], dict] | None = None,
) -> dict[str, Any]:
    """Foto de cierre en T-5..T-0 y catch-up. No toca stake ni el precio apostado.

    El backfill histórico solo mira apuestas de dinero ya liquidadas. Los picks
    de hoy sin apuesta se miden aparte, y solo si el partido está en `juegos`.
    """
    if not isinstance(memoria, dict):
        return {"cambios": 0, "motivos": {}}
    ahora = ahora or datetime.now(timezone.utc)
    cambios = rellenar_clv_historico(memoria)
    por_id = {
        str(j.get("id") or j.get("game_id") or ""): j
        for j in (juegos or [])
        if isinstance(j, dict) and str(j.get("id") or j.get("game_id") or "")
    }
    cache: dict[str, dict | None] = {}
    errores: dict[str, str] = {}
    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        apuestas = [a for a in (dia.get("apuestas") or []) if isinstance(a, dict)]
        dinero_ids = {str(a.get("game_id") or "") for a in apuestas if es_apuesta_dinero(a)}
        for apuesta in apuestas:
            gid = str(apuesta.get("game_id") or "")
            if es_apuesta_dinero(apuesta):
                if gid not in por_id:
                    continue
                cambios += _procesar_registro(
                    apuesta,
                    serie="dinero",
                    juego=por_id.get(gid),
                    ahora=ahora,
                    fetcher=fetcher,
                    cache=cache,
                    errores=errores,
                )
                continue
            if gid in por_id and es_pick_sin_apuesta(apuesta):
                cambios += _procesar_registro(
                    apuesta,
                    serie="sin_apuesta",
                    juego=por_id.get(gid),
                    ahora=ahora,
                    fetcher=fetcher,
                    cache=cache,
                    errores=errores,
                )
        for pred in dia.get("predicciones") or []:
            if not isinstance(pred, dict) or pred.get("con_dinero"):
                continue
            gid = str(pred.get("game_id") or "")
            if not gid or gid in dinero_ids or gid not in por_id:
                continue
            if not es_pick_sin_apuesta(pred):
                continue
            cambios += _procesar_registro(
                pred,
                serie="sin_apuesta",
                juego=por_id.get(gid),
                ahora=ahora,
                fetcher=fetcher,
                cache=cache,
                errores=errores,
            )
    return {"cambios": cambios, "motivos": errores}


def _fila_clv(reg: dict, fecha: Any, serie: str) -> dict[str, Any]:
    clv = reg.get("clv") if isinstance(reg.get("clv"), dict) else {}
    return {
        "game_id": reg.get("game_id"),
        "fecha": fecha,
        "pick": reg.get("pick"),
        "visitante": reg.get("visitante"),
        "home": reg.get("home"),
        "serie": serie,
        "precio_apuesta": clv.get("precio_apuesta"),
        "precio_cierre": clv.get("precio_cierre"),
        "casa_cierre": clv.get("casa_cierre"),
        "casa_apuesta": clv.get("casa_apuesta"),
        "misma_casa": clv.get("misma_casa"),
        "timestamp": clv.get("timestamp"),
        "clv_pp": clv.get("clv_pp"),
        "clv_pct": clv.get("clv_pct"),
        "novig": clv.get("novig"),
    }


def _resumen_serie(filas_medidas: list[dict], n_sin: int) -> dict[str, Any]:
    def _avg(clave: str) -> float | None:
        vals = []
        for fila in filas_medidas:
            try:
                vals.append(float(fila[clave]))
            except (TypeError, ValueError, KeyError):
                continue
        if not vals:
            return None
        return round(sum(vals) / len(vals), 2)

    n = len(filas_medidas)
    bate = sum(1 for f in filas_medidas if _num_o_none(f.get("clv_pp")) is not None and float(f["clv_pp"]) > 0)
    ordenadas = sorted(filas_medidas, key=lambda f: str(f.get("timestamp") or f.get("fecha") or ""), reverse=True)
    return {
        "n": n,
        "promedio_pp": _avg("clv_pp"),
        "promedio_pct": _avg("clv_pct"),
        "bate_cierre_pct": round(100.0 * bate / n, 1) if n else None,
        "sin_cierre": n_sin,
        "ultimos": ordenadas[:10],
    }


def _num_o_none(valor: Any) -> float | None:
    try:
        if valor is None or valor == "":
            return None
        return float(valor)
    except (TypeError, ValueError):
        return None


def resumen_clv_publico(memoria: dict | None) -> dict[str, Any]:
    """Bloque de /api/health y /api/resultados. No escribe ni llama a la red.

    El promedio que hay que leer es `promedio_pp`: puntos de probabilidad
    implícita (sin vig si el cierre trae los dos lados). Positivo = el precio
    de la apuesta era mejor que el cierre. `promedio_pct` es la misma lectura
    en % de cuota. `bate_cierre_pct` es el % de muestras con puntos positivos.
    """
    memoria = memoria if isinstance(memoria, dict) else {}
    dinero: list[dict] = []
    papel: list[dict] = []
    sin_dinero = 0
    sin_papel = 0
    for dia in memoria.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        fecha = dia.get("fecha")
        apuestas = [a for a in (dia.get("apuestas") or []) if isinstance(a, dict)]
        dinero_ids = {str(a.get("game_id") or "") for a in apuestas if es_apuesta_dinero(a)}
        for apuesta in apuestas:
            if not es_apuesta_dinero(apuesta):
                if es_pick_sin_apuesta(apuesta) and apuesta.get("clv") == "sin_cierre":
                    sin_papel += 1
                elif clv_medido(apuesta) and not es_apuesta_dinero(apuesta):
                    papel.append(_fila_clv(apuesta, fecha, "sin_apuesta"))
                continue
            if clv_medido(apuesta):
                dinero.append(_fila_clv(apuesta, fecha, "dinero"))
            elif apuesta.get("clv") == "sin_cierre" or _liquidada_dinero(apuesta):
                sin_dinero += 1
        for pred in dia.get("predicciones") or []:
            if not isinstance(pred, dict) or pred.get("con_dinero"):
                continue
            gid = str(pred.get("game_id") or "")
            if gid and gid in dinero_ids:
                continue
            if not es_pick_sin_apuesta(pred):
                continue
            if clv_medido(pred):
                papel.append(_fila_clv(pred, fecha, "sin_apuesta"))
            elif pred.get("clv") == "sin_cierre":
                sin_papel += 1
    bloque = _resumen_serie(dinero, sin_dinero)
    bloque["sin_apuesta"] = _resumen_serie(papel, sin_papel)
    bloque["leyenda"] = (
        "CLV positivo: el precio de la apuesta era mejor que el cierre. "
        "Los puntos (pp) son de probabilidad implícita, sin vig cuando el cierre "
        "tiene los dos lados. El % compara la cuota decimal. "
        "sin_cierre significa que no hubo un precio real: no se inventa uno."
    )
    return bloque
