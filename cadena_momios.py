"""Cadena de momios reales y pago de sportsbook.

Orden, y se detiene en la primera casa que cotiza el partido:

1. ESPN scoreboard (público, sin key). Dentro de esa respuesta se prueban
   las casas de ``lineas.bookmakers`` y después cualquier otra casa del
   mismo payload.
2. ESPN header (segundo endpoint público, sin key) si el scoreboard no
   trajo ese partido.
3. The Odds API, una casa configurada a la vez. Hace falta ``ODDS_API_KEY``
   (o ``lineas.api_key`` / ``odds_api_key.txt``). Sin key se anota el fallo
   y se sigue; no se inventa un precio de esa API.

Si ninguna fuente real cotiza el partido con un precio fresco y en rango,
el pick se registra sin apuesta (``fuente_momio='sin_momio_real'``). El
estimado con vig -110 puede calcularse para mostrarlo, y no mueve la banca.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# Dos lados a -110: cada uno implica 110/210. El overround es 220/210.
OVERROUND_MENOS_110 = 220.0 / 210.0

_CADENA_DEFAULT = ("espn_scoreboard", "espn_header", "odds_api")
FRESH_MINUTES = 15
_FUENTES_SIN_PRECIO = frozenset(
    {"", "modelo", "none", "null", "import", "estimado", "sin_momio_real"}
)
_ML_SUFIJO = re.compile(r"\s+ML\s*$", re.IGNORECASE)

_rechazos: list[dict[str, Any]] = []
_ultimo_fetch_ok: datetime | None = None


def anotar_fetch_ok(cuando: datetime | None = None) -> None:
    """Marca un fetch de red que sí trajo momios. El cache de disco no cuenta."""
    global _ultimo_fetch_ok
    _ultimo_fetch_ok = cuando or datetime.now(timezone.utc)


def edad_ultimo_fetch_seg() -> float | None:
    if _ultimo_fetch_ok is None:
        return None
    return (datetime.now(timezone.utc) - _ultimo_fetch_ok).total_seconds()


def registrar_rechazo(raw: Any, motivo: str) -> None:
    _rechazos.append(
        {
            "raw": str(raw)[:40],
            "motivo": (motivo or "rechazado")[:120],
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    if len(_rechazos) > 300:
        del _rechazos[:150]


def resumen_rechazos() -> dict[str, Any]:
    razones: dict[str, int] = {}
    for item in _rechazos:
        razones[item["motivo"]] = razones.get(item["motivo"], 0) + 1
    return {"total": len(_rechazos), "razones": razones, "muestra": list(_rechazos[-20:])}


def reset_rechazos() -> None:
    _rechazos.clear()


def reset_auditoria_momios() -> None:
    """Limpia el contador de este proceso. No toca la base."""
    global _ultimo_fetch_ok
    _rechazos.clear()
    _ultimo_fetch_ok = None


def _clave_rechazo(item: dict) -> tuple:
    return (item.get("ts"), item.get("raw"), item.get("motivo"))


def exportar_auditoria_momios() -> dict[str, Any]:
    """Contadores de salud que tienen que viajar dentro del documento."""
    return {
        "ultimo_fetch_ok": _ultimo_fetch_ok.isoformat() if _ultimo_fetch_ok else None,
        "rechazos": [dict(item) for item in _rechazos[-300:]],
    }


def importar_auditoria_momios(data: Any) -> None:
    """Recupera el fetch y los rechazos guardados. No borra lo de este proceso."""
    global _ultimo_fetch_ok
    if not isinstance(data, dict):
        return
    marca = _marca_tiempo(data.get("ultimo_fetch_ok"))
    if marca is not None and (_ultimo_fetch_ok is None or marca > _ultimo_fetch_ok):
        _ultimo_fetch_ok = marca
    vistos = {_clave_rechazo(item) for item in _rechazos}
    for item in data.get("rechazos") or []:
        if not isinstance(item, dict):
            continue
        clave = _clave_rechazo(item)
        if clave in vistos:
            continue
        _rechazos.append(
            {
                "raw": str(item.get("raw") or "")[:40],
                "motivo": str(item.get("motivo") or "rechazado")[:120],
                "ts": str(item.get("ts") or ""),
            }
        )
        vistos.add(clave)
    if len(_rechazos) > 300:
        del _rechazos[: len(_rechazos) - 300]


def volcar_auditoria_en(memoria: dict) -> None:
    """Mete el conteo de este proceso en el documento, sin pisar uno ya guardado."""
    if not isinstance(memoria, dict):
        return
    importar_auditoria_momios(memoria.get("auditoria_momios"))
    memoria["auditoria_momios"] = exportar_auditoria_momios()


def parsear_momio_americano(raw: Any, *, registrar: bool = True) -> int | None:
    """Entero americano en -1000..-100 o +100..+1000. EVEN vale +100.

    Rechaza None, vacío, 0 y todo lo estrictamente entre -100 y +100,
    además de lo que se sale de ±1000.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.upper() in ("EVEN", "EV", "PK"):
            return 100
        s = s.replace("+", "").replace(",", "")
        try:
            n = int(round(float(s)))
        except ValueError:
            if registrar:
                registrar_rechazo(raw, "no numerico")
            return None
    else:
        try:
            n = int(round(float(raw)))
        except (TypeError, ValueError):
            if registrar:
                registrar_rechazo(raw, "no numerico")
            return None
    if n == 0 or -100 < n < 100 or n < -1000 or n > 1000:
        if registrar:
            if n == 0:
                registrar_rechazo(raw, "momio en cero")
            elif -100 < n < 100:
                registrar_rechazo(raw, "entre -100 y +100")
            else:
                registrar_rechazo(raw, "fuera de -1000..+1000")
        return None
    return int(n)


def _marca_tiempo(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        dt = valor
    elif not valor:
        return None
    else:
        texto = str(valor).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(texto)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def precio_fresco(fetched_at: Any, *, stale: bool = False) -> tuple[bool, str]:
    """True solo si el precio se trajo en los últimos 15 minutos y no es cache de disco."""
    if stale:
        return False, "cache de disco, solo display (stale)"
    dt = _marca_tiempo(fetched_at)
    if dt is None:
        return False, "sin marca de hora del fetch"
    edad = datetime.now(timezone.utc) - dt
    if edad < timedelta(minutes=-1) or edad > timedelta(minutes=FRESH_MINUTES):
        minutos = int(edad.total_seconds() // 60)
        return False, f"precio con {minutos} min (máximo {FRESH_MINUTES})"
    return True, ""


def _estado_cuota_es_pre(estado: Any) -> bool:
    s = str(estado or "").strip().lower()
    if not s:
        return True
    return s in ("pre", "scheduled", "preview", "programado", "pre-game", "pregame", "status_scheduled")


def _juego_sin_empezar(juego: dict) -> tuple[bool, str]:
    """Solo scheduled/pre, antes del primer lanzamiento. Nunca momio en vivo."""
    if juego.get("momio_en_vivo"):
        return False, "momio en vivo"
    cuota = juego.get("estado_cuota")
    if cuota and not _estado_cuota_es_pre(cuota):
        return False, "momio en vivo"
    estado = str(juego.get("estado") or "").strip().upper()
    if estado in (
        "EN VIVO",
        "FINALIZADO",
        "POSPUESTO",
        "CANCELADO",
        "SUSPENDIDO",
        "SUSPENDIDO_OFICIAL",
    ):
        return False, f"juego {estado}"
    if estado and estado not in ("PROGRAMADO",):
        return False, f"juego {estado}"
    inicio = juego.get("inicio_juego") or juego.get("commence_time")
    if inicio:
        dt = _marca_tiempo(inicio)
        if dt is not None and dt <= datetime.now(timezone.utc) - timedelta(seconds=60):
            return False, "el juego ya empezó"
    return True, ""


def lado_del_pick(juego: dict, pick: str) -> str:
    """'away' o 'home' por id o abreviatura canónica. No usa un substring del texto."""
    pid = juego.get("pick_team_id")
    if pid not in (None, ""):
        if str(pid) == str(juego.get("away_id") or ""):
            return "away"
        if str(pid) == str(juego.get("home_id") or ""):
            return "home"
    lado = str(juego.get("pick_lado") or "").strip().lower()
    if lado in ("away", "visitante"):
        return "away"
    if lado in ("home", "local"):
        return "home"
    abbr = str(juego.get("pick_abbr") or "").strip().upper()
    away_abbr = str(juego.get("away_abbr") or "").strip().upper()
    home_abbr = str(juego.get("home_abbr") or "").strip().upper()
    if abbr and abbr == away_abbr:
        return "away"
    if abbr and abbr == home_abbr:
        return "home"
    texto = _ML_SUFIJO.sub("", str(pick or "")).strip()
    try:
        from lineas_betmgm import normalizar_nombre_equipo

        nt = normalizar_nombre_equipo(texto)
        away = normalizar_nombre_equipo(str(juego.get("visitante") or ""))
        home = normalizar_nombre_equipo(str(juego.get("home") or ""))
    except Exception:
        nt = texto.lower()
        away = str(juego.get("visitante") or "").lower()
        home = str(juego.get("home") or "").lower()
    if nt and away and nt == away and nt != home:
        return "away"
    if nt and home and nt == home and nt != away:
        return "home"
    tokens = {t.upper() for t in re.findall(r"\b[A-Za-z]{2,4}\b", str(pick or ""))}
    if away_abbr and away_abbr in tokens and home_abbr not in tokens:
        return "away"
    if home_abbr and home_abbr in tokens and away_abbr not in tokens:
        return "home"
    return ""


def apuesta_fija_dolares(cfg: dict | None = None) -> float:
    """Stake fijo de cada apuesta nueva. No depende de banca, edge ni Kelly."""
    cfg = cfg or {}
    try:
        stake = float(cfg.get("apuesta_fija", 3.0))
    except (TypeError, ValueError):
        stake = 3.0
    if stake <= 0:
        stake = 3.0
    return round(stake, 2)


def profit_moneyline_americano(stake: float, american: int | float, resultado: str) -> float:
    """P/L de sportsbook a cuota americana congelada.

    Ganada y momio negativo: stake * 100 / |momio|.
    Ganada y momio positivo: stake * momio / 100.
    Perdida: se pierde el stake.
    Push o void: se devuelve el stake (P/L 0).
    """
    res = str(resultado or "").strip().lower()
    if res in ("push", "void", "empate", "anulada", "nula"):
        return 0.0
    monto = round(float(stake), 2)
    if res in ("perdida", "loss", "fallo"):
        return round(-monto, 2)
    if res not in ("ganada", "win", "acierto"):
        raise ValueError(f"resultado de liquidación no reconocido: {resultado}")
    momio = int(american)
    if momio == 0:
        raise ValueError("momio americano en cero")
    if momio < 0:
        return round(monto * 100.0 / abs(momio), 2)
    return round(monto * momio / 100.0, 2)


def _redondear_como_casa(raw: float) -> int:
    """Entero de a 5, como publican las casas. Nunca queda entre -100 y +100."""
    n = int(round(float(raw) / 5.0) * 5)
    if n == 0:
        n = -110 if raw < 0 else 110
    if -100 < n < 100:
        n = -100 if raw < 0 else 100
    return int(n)


def american_con_vig(prob_pct: float) -> int:
    """Probabilidad del modelo → momio americano con vig de -110 por lado.

    50% cae en -110. El resultado se redondea de 5 en 5.
    """
    p = max(0.05, min(0.95, float(prob_pct) / 100.0))
    implied = min(0.95, max(0.05, p * OVERROUND_MENOS_110))
    if implied >= 0.5:
        raw = -100.0 * implied / (1.0 - implied)
    else:
        raw = 100.0 * (1.0 - implied) / implied
    return _redondear_como_casa(raw)


def _decimal(american: int) -> float:
    from lineas_betmgm import american_a_decimal

    return float(american_a_decimal(int(american)))


def _texto_fallos(intentos: list[dict] | None) -> str:
    partes = []
    for item in intentos or []:
        if item.get("ok"):
            continue
        fuente = str(item.get("fuente") or "fuente")
        motivo = str(item.get("motivo") or "sin momio").strip()
        partes.append(f"{fuente}: {motivo}")
    return "; ".join(partes)[:400]


def aplicar_momio_estimado(
    juego: dict[str, Any],
    prob_away: float,
    prob_home: float,
    *,
    intentos: list[dict] | None = None,
) -> dict[str, Any]:
    """Referencia con vig -110. No es cuota de casa y no autoriza dinero."""
    away_ml = american_con_vig(prob_away)
    home_ml = american_con_vig(prob_home)
    juego["odds_estimado_away_american"] = away_ml
    juego["odds_estimado_home_american"] = home_ml
    juego["odds_estimado_away_decimal"] = _decimal(away_ml)
    juego["odds_estimado_home_decimal"] = _decimal(home_ml)
    # El cache viejo puede seguir en pantalla, marcado stale. No se pisa con el estimado.
    if not juego.get("momio_stale"):
        juego["odds_away_american"] = away_ml
        juego["odds_home_american"] = home_ml
        juego["odds_away_decimal"] = _decimal(away_ml)
        juego["odds_home_decimal"] = _decimal(home_ml)
    juego["fuente_momio"] = "sin_momio_real"
    juego["lineas_fuente"] = "sin_momio_real"
    juego["casa_momio"] = "sin_momio_real"
    juego["paso_momio"] = "sin_casa"
    juego["origen_momio"] = "sin_momio_real"
    juego["estado_registro"] = "registrado sin apuesta"
    juego["sin_momio_real"] = True
    juego["odds_informativo"] = True
    if intentos is not None:
        juego["momio_intentos"] = list(intentos)
    fallos = _texto_fallos(juego.get("momio_intentos"))
    juego["momio_fallos"] = fallos
    print(
        f"[MOMIO] {juego.get('visitante')} @ {juego.get('home')} · "
        f"sin momio real, solo registrado · referencia {away_ml:+d}/{home_ml:+d}"
        + (f" · falló: {fallos}" if fallos else "")
    )
    return juego


def _anotar(juego: dict, fuente: str, ok: bool, motivo: str) -> None:
    intentos = juego.setdefault("momio_intentos", [])
    if not isinstance(intentos, list):
        intentos = []
        juego["momio_intentos"] = intentos
    intentos.append({"fuente": fuente, "ok": bool(ok), "motivo": (motivo or "")[:180]})


def _orden_casas(cfg: dict | None) -> list[str]:
    lineas = (cfg or {}).get("lineas") or {}
    books = lineas.get("bookmakers") or "pinnacle,draftkings,fanduel,betmgm"
    if isinstance(books, list):
        crudo = [str(b) for b in books]
    else:
        crudo = [b.strip() for b in str(books).split(",") if b.strip()]
    out = []
    vistos = set()
    for casa in crudo:
        key = casa.lower().replace(" ", "")
        if key and key not in vistos:
            vistos.add(key)
            out.append(key)
    return out or ["draftkings"]


def _pasos_cadena(cfg: dict | None) -> list[str]:
    lineas = (cfg or {}).get("lineas") or {}
    raw = lineas.get("cadena_momios") or list(_CADENA_DEFAULT)
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip()]
    pasos = []
    for paso in raw:
        nombre = str(paso or "").strip().lower()
        if nombre in _CADENA_DEFAULT and nombre not in pasos:
            pasos.append(nombre)
    return pasos or list(_CADENA_DEFAULT)


def _ml_de(libro: dict, lado: str) -> int | None:
    ml_key = "ml_away" if lado == "away" else "ml_home"
    dec_key = lado
    ml = libro.get(ml_key)
    if ml is None:
        ml = libro.get("american") if libro.get("lado") == lado else None
    if ml is None and libro.get(dec_key) not in (None, ""):
        try:
            from lineas_betmgm import decimal_a_american

            ml = decimal_a_american(float(libro[dec_key]))
        except (TypeError, ValueError):
            return None
    return parsear_momio_americano(ml)


def _motivo_si_no_apto(libro: dict) -> str | None:
    """None si el libro puede usarse para apostar."""
    if libro.get("stale") or libro.get("momio_stale"):
        return "cache de disco, solo display (stale)"
    if libro.get("en_vivo") or libro.get("momio_en_vivo"):
        return "momio en vivo, no se usa"
    estado = libro.get("estado_cuota") or libro.get("status")
    if estado and not _estado_cuota_es_pre(estado):
        return "momio en vivo, no se usa"
    if libro.get("fetched_at"):
        fresco, motivo = precio_fresco(libro.get("fetched_at"), stale=False)
        if not fresco:
            return motivo
    if _ml_de(libro, "away") is None or _ml_de(libro, "home") is None:
        return "moneyline fuera de rango o incompleto"
    return None


def _precio_libro(libro: dict) -> dict[str, Any] | None:
    if _motivo_si_no_apto(libro):
        return None
    away = _ml_de(libro, "away")
    home = _ml_de(libro, "home")
    if away is None or home is None:
        return None
    casa = str(libro.get("casa") or libro.get("provider") or "casa").lower().replace(" ", "")
    return {
        "fuente_momio": casa,
        "casa_momio": str(libro.get("provider") or casa),
        "odds_away_american": away,
        "odds_home_american": home,
        "odds_away_decimal": _decimal(away),
        "odds_home_decimal": _decimal(home),
        "fetched_at": libro.get("fetched_at"),
        "stale": False,
        "en_vivo": False,
        "estado_cuota": libro.get("estado_cuota") or "pre",
        "fecha": libro.get("fecha"),
        "inicio": libro.get("inicio"),
        "espn_id": libro.get("espn_id"),
    }


def _paso_de(nombre: str) -> str:
    """Nombre del eslabón de la cadena, sin la casa."""
    nombre = str(nombre or "").strip().lower()
    if nombre.startswith("odds_api"):
        return "odds_api"
    if nombre.startswith("espn_header"):
        return "espn_header"
    if nombre.startswith("espn_scoreboard"):
        return "espn_scoreboard"
    return (nombre.split(":")[0] or "casa")


def _escribir_precio(juego: dict, precio: dict, paso: str) -> None:
    juego.update(precio)
    juego["lineas_fuente"] = precio["fuente_momio"]
    juego["fuente_momio"] = precio["fuente_momio"]
    juego["casa_momio"] = precio.get("casa_momio") or precio["fuente_momio"]
    juego["paso_momio"] = _paso_de(paso)
    juego["origen_momio"] = f"{juego['paso_momio']}:{precio['fuente_momio']}"
    juego["momio_stale"] = False
    juego["momio_en_vivo"] = False
    juego["estado_cuota"] = precio.get("estado_cuota") or juego.get("estado_cuota") or "pre"
    if precio.get("fetched_at"):
        juego["momio_fetched_at"] = precio["fetched_at"]
    elif not juego.get("momio_fetched_at"):
        juego["momio_fetched_at"] = datetime.now(timezone.utc).isoformat()
    juego["sin_momio_real"] = False


def _libros_juego(juego: dict) -> list[dict]:
    libros = [b for b in (juego.get("lineas_libros") or []) if isinstance(b, dict)]
    if libros:
        return libros
    fuente = str(juego.get("lineas_fuente") or "").lower()
    if fuente in _FUENTES_SIN_PRECIO:
        return []
    if juego.get("odds_away_decimal") and juego.get("odds_home_decimal"):
        return [
            {
                "casa": fuente,
                "provider": juego.get("casa_momio") or fuente,
                "away": juego.get("odds_away_decimal"),
                "home": juego.get("odds_home_decimal"),
                "ml_away": juego.get("odds_away_american"),
                "ml_home": juego.get("odds_home_american"),
                "stale": juego.get("momio_stale"),
                "en_vivo": juego.get("momio_en_vivo"),
                "estado_cuota": juego.get("estado_cuota"),
                "fetched_at": juego.get("momio_fetched_at"),
                "fecha": juego.get("fecha_linea") or juego.get("fecha"),
                "inicio": juego.get("inicio_linea") or juego.get("inicio_juego"),
            }
        ]
    return []


def _tiene_momio_real(juego: dict) -> bool:
    fuente = str(juego.get("fuente_momio") or juego.get("lineas_fuente") or "").lower()
    if fuente in _FUENTES_SIN_PRECIO:
        return False
    if juego.get("momio_stale") or juego.get("momio_en_vivo"):
        return False
    if juego.get("estado_cuota") and not _estado_cuota_es_pre(juego.get("estado_cuota")):
        return False
    if parsear_momio_americano(juego.get("odds_away_american"), registrar=False) is None:
        return False
    if parsear_momio_americano(juego.get("odds_home_american"), registrar=False) is None:
        return False
    if juego.get("momio_fetched_at"):
        fresco, _motivo = precio_fresco(juego.get("momio_fetched_at"), stale=False)
        if not fresco:
            return False
    return True


def _elegir_de_libros(juego: dict, orden: list[str], nombre_fuente: str) -> bool:
    libros = _libros_juego(juego)
    if not libros:
        _anotar(juego, nombre_fuente, False, "sin momio para este partido")
        return False
    por_casa: dict[str, dict] = {}
    for libro in libros:
        casa = str(libro.get("casa") or "").lower().replace(" ", "")
        if casa and casa not in por_casa:
            por_casa[casa] = libro
    elegido = None
    for casa in orden:
        libro = por_casa.get(casa)
        if not libro:
            _anotar(juego, f"{nombre_fuente}:{casa}", False, "no cotiza este partido")
            continue
        motivo_libro = _motivo_si_no_apto(libro)
        if motivo_libro:
            _anotar(juego, f"{nombre_fuente}:{casa}", False, motivo_libro)
            continue
        elegido = libro
        break
    if elegido is None:
        for libro in libros:
            casa = str(libro.get("casa") or "").lower().replace(" ", "")
            if casa in orden:
                continue
            motivo_libro = _motivo_si_no_apto(libro)
            if motivo_libro:
                _anotar(juego, f"{nombre_fuente}:{casa or 'casa'}", False, motivo_libro)
                continue
            elegido = libro
            break
    if elegido is None:
        _anotar(juego, nombre_fuente, False, "ninguna casa del payload trae moneyline")
        return False
    precio = _precio_libro(elegido)
    if not precio:
        return False
    _escribir_precio(juego, precio, nombre_fuente)
    _anotar(juego, f"{nombre_fuente}:{precio['fuente_momio']}", True, "momio real")
    return True


def _aplicar_mapa(juego: dict, mapa: dict | None, nombre: str, meta: dict | None) -> bool:
    if not mapa:
        motivo = (meta or {}).get("mensaje") or "sin partidos"
        _anotar(juego, nombre, False, str(motivo)[:180])
        return False
    from lineas_betmgm import buscar_lineas_partido

    fila = buscar_lineas_partido(
        mapa,
        juego.get("visitante") or "",
        juego.get("home") or "",
        fecha=juego.get("fecha"),
        inicio=juego.get("inicio_juego"),
        evento_id=juego.get("espn_id") or juego.get("id"),
    )
    if not fila:
        _anotar(juego, nombre, False, "sin momio de esta fecha")
        return False
    away = fila.get("away") if isinstance(fila.get("away"), dict) else {}
    home = fila.get("home") if isinstance(fila.get("home"), dict) else {}
    libro = {
        "casa": away.get("casa") or home.get("casa") or nombre.split(":")[-1],
        "provider": away.get("casa") or home.get("casa") or nombre,
        "ml_away": away.get("american"),
        "ml_home": home.get("american"),
        "away": away.get("decimal"),
        "home": home.get("decimal"),
        "stale": fila.get("stale"),
        "en_vivo": fila.get("en_vivo"),
        "estado_cuota": fila.get("estado_cuota"),
        "fetched_at": fila.get("fetched_at"),
        "fecha": fila.get("fecha"),
        "inicio": fila.get("inicio"),
        "espn_id": fila.get("espn_id"),
    }
    motivo_libro = _motivo_si_no_apto(libro)
    if motivo_libro:
        _anotar(juego, nombre, False, motivo_libro)
        return False
    precio = _precio_libro(libro)
    if not precio:
        _anotar(juego, nombre, False, "moneyline fuera de rango o incompleto")
        return False
    _escribir_precio(juego, precio, nombre)
    if isinstance(fila.get("libros"), list) and not juego.get("lineas_libros"):
        juego["lineas_libros"] = [dict(b) for b in fila["libros"] if isinstance(b, dict)]
    _anotar(juego, f"{nombre}:{precio['fuente_momio']}", True, "momio real")
    return True


def _log_resultado(juego: dict) -> None:
    nombre = f"{juego.get('visitante')} @ {juego.get('home')}"
    fallos = _texto_fallos(juego.get("momio_intentos"))
    juego["momio_fallos"] = fallos
    if _tiene_momio_real(juego) and not juego.get("fuente_momio"):
        juego["fuente_momio"] = str(juego.get("lineas_fuente") or "casa")
        juego.setdefault("casa_momio", juego["fuente_momio"])
    if _tiene_momio_real(juego):
        extra = f" · falló antes: {fallos}" if fallos else ""
        print(
            f"[MOMIO] {nombre} · {juego.get('origen_momio') or juego.get('fuente_momio')} "
            f"{int(juego['odds_away_american']):+d}/{int(juego['odds_home_american']):+d}{extra}"
        )
        return
    print(
        f"[MOMIO] {nombre} · sin momio real"
        + (f" · {fallos}" if fallos else "")
        + " · registrado sin apuesta"
    )


def _fetch_espn_default(juegos: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    from lineas_espn import aplicar_lineas_espn

    try:
        return aplicar_lineas_espn(juegos, cfg, solo_vacios=False)
    except Exception as e:
        print(f"[MOMIO] espn_scoreboard falló: {e}")
        return juegos, {"ok": False, "mensaje": f"ESPN scoreboard: {e}"[:180]}


def _fetch_header_default() -> tuple[dict, dict]:
    from lineas_espn import obtener_mapa_header_espn

    try:
        return obtener_mapa_header_espn()
    except Exception as e:
        print(f"[MOMIO] espn_header falló: {e}")
        return {}, {"ok": False, "mensaje": f"ESPN header: {e}"[:180]}


def _fetch_odds_default(cfg: dict) -> tuple[dict[str, dict], dict]:
    from lineas_betmgm import obtener_mapas_por_casa

    try:
        return obtener_mapas_por_casa(cfg)
    except Exception as e:
        print(f"[MOMIO] odds_api falló: {e}")
        return {}, {"ok": False, "mensaje": f"The Odds API: {e}"[:180]}


def aplicar_cadena_momios(
    juegos: list[dict],
    cfg: dict | None = None,
    *,
    fetch_espn: Callable | None = None,
    fetch_header: Callable | None = None,
    fetch_odds: Callable | None = None,
) -> tuple[list[dict], dict]:
    """Rellena momios reales. Los partidos sin casa quedan para el estimado."""
    cfg = cfg or {}
    orden = _orden_casas(cfg)
    pasos = _pasos_cadena(cfg)
    fetch_espn = fetch_espn or _fetch_espn_default
    fetch_header = fetch_header or _fetch_header_default
    fetch_odds = fetch_odds or _fetch_odds_default

    meta_espn: dict = {"ok": False, "mensaje": "ESPN no consultado"}
    if "espn_scoreboard" in pasos:
        juegos, meta_espn = fetch_espn(juegos, cfg)
        if not isinstance(meta_espn, dict):
            meta_espn = {"ok": False, "mensaje": "ESPN scoreboard sin meta"}
        if not meta_espn.get("ok") and not any(_libros_juego(j) for j in juegos):
            for juego in juegos:
                _anotar(
                    juego,
                    "espn_scoreboard",
                    False,
                    str(meta_espn.get("mensaje") or "ESPN scoreboard sin cuotas")[:180],
                )
        else:
            for juego in juegos:
                if _tiene_momio_real(juego) and juego.get("fuente_momio"):
                    continue
                _elegir_de_libros(juego, orden, "espn_scoreboard")

    if "espn_header" in pasos:
        faltan = [j for j in juegos if not _tiene_momio_real(j)]
        if faltan:
            mapa_h, meta_h = fetch_header()
            if not isinstance(meta_h, dict):
                meta_h = {"ok": False, "mensaje": "ESPN header sin meta"}
            for juego in faltan:
                _aplicar_mapa(juego, mapa_h if isinstance(mapa_h, dict) else {}, "espn_header", meta_h)

    meta_api: dict = {"ok": False, "mensaje": "The Odds API no consultada"}
    if "odds_api" in pasos:
        faltan = [j for j in juegos if not _tiene_momio_real(j)]
        if faltan:
            mapas, meta_api = fetch_odds(cfg)
            if not isinstance(meta_api, dict):
                meta_api = {"ok": False, "mensaje": "The Odds API sin meta"}
            mapas = mapas if isinstance(mapas, dict) else {}
            if not mapas:
                motivo = str(meta_api.get("mensaje") or "sin momios")[:180]
                for juego in faltan:
                    _anotar(juego, "odds_api", False, motivo)
            else:
                casas = list(orden) + [c for c in mapas if c not in orden]
                for casa in casas:
                    mapa = mapas.get(casa) or {}
                    for juego in faltan:
                        if _tiene_momio_real(juego):
                            continue
                        _aplicar_mapa(
                            juego,
                            mapa,
                            f"odds_api:{casa}",
                            {"ok": bool(mapa), "mensaje": "esta casa no cotiza el partido"},
                        )

    reales = 0
    for juego in juegos:
        _log_resultado(juego)
        if _tiene_momio_real(juego):
            reales += 1
    meta = {
        "ok": reales > 0,
        "fuente": "cadena",
        "partidos": reales,
        "reales": reales,
        "sin_casa": len(juegos) - reales,
        "cadena": pasos,
        "bookmakers": orden,
        "mensaje": (
            f"Cadena: {reales} con casa real · {len(juegos) - reales} sin momio real "
            "(registrado sin apuesta)"
        ),
        "espn": meta_espn.get("mensaje"),
        "odds_api": meta_api.get("mensaje"),
    }
    return juegos, meta


def _precio_real_del_lado(juego: dict, lado: str) -> dict[str, Any] | None:
    if lado not in ("away", "home"):
        return None
    if not _tiene_momio_real(juego):
        return None
    sigue, _motivo = _juego_sin_empezar(juego)
    if not sigue:
        return None
    fresco, _motivo_f = precio_fresco(juego.get("momio_fetched_at"), stale=bool(juego.get("momio_stale")))
    if not fresco:
        return None
    raw = juego.get("odds_away_american") if lado == "away" else juego.get("odds_home_american")
    american = parsear_momio_americano(raw)
    if american is None:
        return None
    return {
        "odds": _decimal(american),
        "odds_american": american,
        "fuente_momio": juego.get("fuente_momio") or juego.get("lineas_fuente"),
        "casa": juego.get("casa_momio") or juego.get("fuente_momio"),
        "paso_momio": juego.get("paso_momio") or "",
        "origen_momio": juego.get("origen_momio") or "",
        "estimado": False,
        "apto_para_apuesta": True,
        "fetched_at": juego.get("momio_fetched_at"),
        "momio_fallos": juego.get("momio_fallos") or "",
    }


def momio_del_pick(juego: dict, pick: str, prob_pct: float) -> dict[str, Any]:
    """Precio del lado elegido. Sin casa fresca y en rango, no es apuesta."""
    lado = lado_del_pick(juego, pick)
    real = _precio_real_del_lado(juego, lado)
    if real is not None:
        return real
    if lado == "away":
        away_p, home_p = float(prob_pct), max(0.0, 100.0 - float(prob_pct))
    elif lado == "home":
        home_p, away_p = float(prob_pct), max(0.0, 100.0 - float(prob_pct))
    else:
        away_p = home_p = float(prob_pct)
    if str(juego.get("fuente_momio") or "") not in ("sin_momio_real", "estimado"):
        aplicar_momio_estimado(juego, away_p, home_p, intentos=juego.get("momio_intentos"))
    if lado == "away":
        ref = juego.get("odds_estimado_away_american", juego.get("odds_away_american"))
    elif lado == "home":
        ref = juego.get("odds_estimado_home_american", juego.get("odds_home_american"))
    else:
        ref = None
    american = parsear_momio_americano(ref, registrar=False)
    if american is None:
        american = american_con_vig(prob_pct)
    return {
        "odds": _decimal(american),
        "odds_american": american,
        "fuente_momio": "sin_momio_real",
        "casa": "sin_momio_real",
        "paso_momio": "sin_casa",
        "origen_momio": "sin_momio_real",
        "estimado": True,
        "apto_para_apuesta": False,
        "estado_registro": "registrado sin apuesta",
        "informativo": True,
        "momio_fallos": juego.get("momio_fallos") or _texto_fallos(juego.get("momio_intentos")),
    }


def campos_precio_congelado(precio: dict, stake: float) -> dict[str, Any]:
    """Campos que se guardan al apostar. El pago sale del momio congelado."""
    american = int(precio["odds_american"])
    return {
        "odds": float(precio["odds"]),
        "odds_american": american,
        "lineas_fuente": precio.get("fuente_momio"),
        "fuente_momio": precio.get("fuente_momio"),
        "casa": precio.get("casa"),
        "paso_momio": precio.get("paso_momio") or "",
        "origen_momio": precio.get("origen_momio") or "",
        "payout_si_gana": profit_moneyline_americano(stake, american, "ganada"),
        "momio_fallos": precio.get("momio_fallos") or "",
        "precio_congelado": True,
        "momio_fetched_at": precio.get("fetched_at"),
    }
