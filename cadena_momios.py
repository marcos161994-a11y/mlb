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

Si ninguna fuente real cotiza el partido, el modelo pone un momio estimado
con la vig de un -110/-110 (margen ~4.76%) y ``fuente_momio='estimado'``.
Ese último recurso vive en ``aplicar_momio_estimado`` y lo llama el modelo
cuando ya conoce la probabilidad. Nunca se salta el pick por falta de cuota.
"""

from __future__ import annotations

from typing import Any, Callable

# Dos lados a -110: cada uno implica 110/210. El overround es 220/210.
OVERROUND_MENOS_110 = 220.0 / 210.0

_CADENA_DEFAULT = ("espn_scoreboard", "espn_header", "odds_api")


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
    """Último recurso. Marca el partido como estimado; no es cuota de casa."""
    away_ml = american_con_vig(prob_away)
    home_ml = american_con_vig(prob_home)
    juego["odds_away_american"] = away_ml
    juego["odds_home_american"] = home_ml
    juego["odds_away_decimal"] = _decimal(away_ml)
    juego["odds_home_decimal"] = _decimal(home_ml)
    juego["fuente_momio"] = "estimado"
    juego["lineas_fuente"] = "estimado"
    juego["casa_momio"] = "estimado"
    juego["paso_momio"] = "estimado"
    juego["origen_momio"] = "estimado"
    if intentos is not None:
        juego["momio_intentos"] = list(intentos)
    fallos = _texto_fallos(juego.get("momio_intentos"))
    juego["momio_fallos"] = fallos
    print(
        f"[MOMIO] {juego.get('visitante')} @ {juego.get('home')} · "
        f"estimado {away_ml:+d}/{home_ml:+d} (vig -110)"
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
    try:
        n = int(ml)
    except (TypeError, ValueError):
        return None
    return n if n != 0 else None


def _precio_libro(libro: dict) -> dict[str, Any] | None:
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
    juego["paso_momio"] = _paso_de(paso)
    juego["origen_momio"] = f"{juego['paso_momio']}:{precio['fuente_momio']}"


def _libros_juego(juego: dict) -> list[dict]:
    libros = [b for b in (juego.get("lineas_libros") or []) if isinstance(b, dict)]
    if libros:
        return libros
    fuente = str(juego.get("lineas_fuente") or "").lower()
    if fuente in ("", "modelo", "none", "null", "import", "estimado"):
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
            }
        ]
    return []


def _tiene_momio_real(juego: dict) -> bool:
    fuente = str(juego.get("fuente_momio") or juego.get("lineas_fuente") or "").lower()
    if fuente in ("", "modelo", "none", "null", "import", "estimado"):
        return False
    return (
        juego.get("odds_away_american") not in (None, 0)
        and juego.get("odds_home_american") not in (None, 0)
    )


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
        if not libro or not _precio_libro(libro):
            _anotar(juego, f"{nombre_fuente}:{casa}", False, "no cotiza este partido")
            continue
        elegido = libro
        break
    if elegido is None:
        for libro in libros:
            casa = str(libro.get("casa") or "").lower().replace(" ", "")
            if casa in orden:
                continue
            if not _precio_libro(libro):
                _anotar(juego, f"{nombre_fuente}:{casa or 'casa'}", False, "moneyline incompleto")
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

    fila = buscar_lineas_partido(mapa, juego.get("visitante") or "", juego.get("home") or "")
    if not fila:
        _anotar(juego, nombre, False, "sin momio para este partido")
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
    }
    precio = _precio_libro(libro)
    if not precio:
        _anotar(juego, nombre, False, "moneyline incompleto")
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
        + " · se estimará con vig -110"
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
            f"Cadena: {reales} con casa real · {len(juegos) - reales} sin casa "
            "(se estiman con vig -110)"
        ),
        "espn": meta_espn.get("mensaje"),
        "odds_api": meta_api.get("mensaje"),
    }
    return juegos, meta


def momio_del_pick(juego: dict, pick: str, prob_pct: float) -> dict[str, Any]:
    """Precio del lado elegido, ya resuelto. Si no hay casa, estima con vig."""
    visitante = str(juego.get("visitante") or "")
    home = str(juego.get("home") or "")
    lado_away = bool(visitante and visitante in (pick or ""))
    if _tiene_momio_real(juego):
        american = juego.get("odds_away_american") if lado_away else juego.get("odds_home_american")
        if american not in (None, 0):
            american_i = int(american)
            return {
                "odds": _decimal(american_i),
                "odds_american": american_i,
                "fuente_momio": juego.get("fuente_momio") or juego.get("lineas_fuente"),
                "casa": juego.get("casa_momio") or juego.get("fuente_momio"),
                "paso_momio": juego.get("paso_momio") or "",
                "origen_momio": juego.get("origen_momio") or "",
                "estimado": False,
                "momio_fallos": juego.get("momio_fallos") or "",
            }
    away_p = float(prob_pct if lado_away else max(0.0, 100.0 - float(prob_pct)))
    home_p = float(prob_pct if not lado_away else max(0.0, 100.0 - float(prob_pct)))
    if not juego.get("fuente_momio"):
        aplicar_momio_estimado(juego, away_p, home_p, intentos=juego.get("momio_intentos"))
    american_i = int(juego.get("odds_away_american") if lado_away else juego.get("odds_home_american") or 0)
    if american_i == 0:
        american_i = american_con_vig(prob_pct)
        juego["fuente_momio"] = "estimado"
        juego["lineas_fuente"] = "estimado"
        juego["paso_momio"] = "estimado"
        juego["origen_momio"] = "estimado"
    return {
        "odds": _decimal(american_i),
        "odds_american": american_i,
        "fuente_momio": "estimado",
        "casa": "estimado",
        "paso_momio": "estimado",
        "origen_momio": "estimado",
        "estimado": True,
        "momio_fallos": juego.get("momio_fallos") or _texto_fallos(juego.get("momio_intentos")),
    }
