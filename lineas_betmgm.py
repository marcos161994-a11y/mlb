"""Empareja el moneyline de un partido (equipo, fecha y hora).

ESPN y Action Network usan estas funciones para no cruzar otro día
ni otro partido.
"""

from __future__ import annotations

import re
from typing import Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Nombres de MLB StatsAPI hacia la forma que traen ESPN y Action Network.
ALIASES: dict[str, str] = {
    "athletics": "athletics",
    "oakland athletics": "athletics",
    "oakland": "athletics",
    "as": "athletics",
    "arizona diamondbacks": "arizona diamondbacks",
    "arizona dbacks": "arizona diamondbacks",
    "dbacks": "arizona diamondbacks",
    "chicago cubs": "chicago cubs",
    "chicago white sox": "chicago white sox",
    "los angeles angels": "los angeles angels",
    "la angels": "los angeles angels",
    "anaheim angels": "los angeles angels",
    "los angeles dodgers": "los angeles dodgers",
    "la dodgers": "los angeles dodgers",
    "new york mets": "new york mets",
    "ny mets": "new york mets",
    "new york yankees": "new york yankees",
    "ny yankees": "new york yankees",
    "tampa bay rays": "tampa bay rays",
    "tampa bay": "tampa bay rays",
    "st louis cardinals": "st louis cardinals",
    "saint louis cardinals": "st louis cardinals",
    "stl cardinals": "st louis cardinals",
    "san francisco giants": "san francisco giants",
    "sf giants": "san francisco giants",
    "washington nationals": "washington nationals",
    "boston red sox": "boston red sox",
    "cleveland guardians": "cleveland guardians",
    "cleveland indians": "cleveland guardians",
    "kansas city royals": "kansas city royals",
    "miami marlins": "miami marlins",
    "florida marlins": "miami marlins",
    "san diego padres": "san diego padres",
    "seattle mariners": "seattle mariners",
    "texas rangers": "texas rangers",
    "toronto blue jays": "toronto blue jays",
    "minnesota twins": "minnesota twins",
    "milwaukee brewers": "milwaukee brewers",
    "houston astros": "houston astros",
    "detroit tigers": "detroit tigers",
    "cincinnati reds": "cincinnati reds",
    "pittsburgh pirates": "pittsburgh pirates",
    "philadelphia phillies": "philadelphia phillies",
    "atlanta braves": "atlanta braves",
    "baltimore orioles": "baltimore orioles",
    "colorado rockies": "colorado rockies",
}


def _norm(nombre: str) -> str:
    s = nombre.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


def normalizar_nombre_equipo(nombre: str) -> str:
    n = _norm(nombre)
    return ALIASES.get(n, n)


def _match_key(away: str, home: str) -> tuple[str, str]:
    return normalizar_nombre_equipo(away), normalizar_nombre_equipo(home)


def american_a_decimal(price: float | int) -> float:
    p = float(price)
    if p > 0:
        return round(1 + p / 100, 3)
    if p < 0:
        return round(1 + 100 / abs(p), 3)
    return 1.0


def decimal_a_american(decimal: float) -> int:
    if decimal >= 2.0:
        return int(round((decimal - 1) * 100))
    return int(round(-100 / (decimal - 1)))


def fecha_iso(valor: Any) -> str | None:
    """YYYY-MM-DD o YYYYMMDD. No acepta otra forma."""
    if valor is None:
        return None
    s = str(valor).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def fecha_slate_desde_instante(iso: Any) -> str | None:
    """Día MLB (hora Nueva York) de un commence_time. Alineado con officialDate."""
    if not iso:
        return None
    texto = str(iso).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _marca_instante(valor: Any) -> datetime | None:
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
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def _fila_sin_variantes(fila: dict) -> dict:
    return {k: v for k, v in fila.items() if k != "variantes"}


def anexar_linea(mapa: dict, clave: tuple[str, str], fila: dict) -> None:
    """Guarda otra línea del mismo par de equipos sin pisar otro día u otra hora."""
    piezas = fila.get("variantes") if isinstance(fila.get("variantes"), list) else None
    if not piezas:
        piezas = [fila]
    for pieza in piezas:
        if not isinstance(pieza, dict):
            continue
        limpia = _fila_sin_variantes(pieza)
        actual = mapa.get(clave)
        if not isinstance(actual, dict):
            mapa[clave] = {**limpia, "variantes": [dict(limpia)]}
            continue
        vars_: list[dict] = []
        for previa in actual.get("variantes") or [actual]:
            if isinstance(previa, dict):
                vars_.append(_fila_sin_variantes(previa))
        eid = str(limpia.get("espn_id") or limpia.get("id") or "")
        inicio = str(limpia.get("inicio") or "")
        fecha = str(limpia.get("fecha") or "")
        repetida = False
        for previa in vars_:
            prev_id = str(previa.get("espn_id") or previa.get("id") or "")
            if eid and prev_id == eid:
                repetida = True
                break
            if (
                not eid
                and fecha
                and inicio
                and str(previa.get("fecha") or "") == fecha
                and str(previa.get("inicio") or "") == inicio
            ):
                repetida = True
                break
        if repetida:
            continue
        vars_.append(dict(limpia))
        primero = vars_[0]
        actual.clear()
        actual.update(primero)
        actual["variantes"] = vars_


def _swap_una(m: dict) -> dict:
    out = {k: v for k, v in m.items() if k not in ("away", "home", "libros", "variantes")}
    out["away"] = m.get("home")
    out["home"] = m.get("away")
    if isinstance(m.get("total"), dict):
        out["total"] = dict(m["total"])
    if isinstance(m.get("libros"), list):
        swapped = []
        for b in m["libros"]:
            if not isinstance(b, dict):
                continue
            row = {
                "casa": b.get("casa"),
                "provider": b.get("provider"),
                "away": b.get("home"),
                "home": b.get("away"),
            }
            if b.get("ml_home") is not None:
                row["ml_away"] = b.get("ml_home")
            if b.get("ml_away") is not None:
                row["ml_home"] = b.get("ml_away")
            for extra in ("stale", "en_vivo", "estado_cuota", "fetched_at", "fecha", "inicio"):
                if extra in b:
                    row[extra] = b.get(extra)
            swapped.append(row)
        out["libros"] = swapped
    return out


def _candidatos_linea(fila: dict) -> list[dict]:
    vars_ = fila.get("variantes") if isinstance(fila, dict) else None
    if isinstance(vars_, list) and any(isinstance(v, dict) for v in vars_):
        return [v for v in vars_ if isinstance(v, dict)]
    return [fila] if isinstance(fila, dict) else []


def _mas_cercana(cands: list[dict], inicio: Any) -> dict | None:
    objetivo = _marca_instante(inicio)
    if objetivo is None:
        return None
    mejor = None
    mejor_delta = None
    for cand in cands:
        dt = _marca_instante(cand.get("inicio") or cand.get("commence_time"))
        if dt is None:
            continue
        delta = abs((dt - objetivo).total_seconds())
        if delta <= 45 * 60 and (mejor_delta is None or delta < mejor_delta):
            mejor = cand
            mejor_delta = delta
    return mejor


def seleccionar_linea_partido(
    fila: dict,
    *,
    fecha: Any = None,
    inicio: Any = None,
    evento_id: Any = None,
) -> dict | None:
    """Elige la línea de este partido. Nunca devuelve la de otro día."""
    cands = _candidatos_linea(fila)
    fecha_n = fecha_iso(fecha) if fecha else None
    if evento_id not in (None, ""):
        por_id = [
            c
            for c in cands
            if str(c.get("espn_id") or c.get("id") or "") == str(evento_id)
        ]
        if por_id:
            if fecha_n:
                por_id = [c for c in por_id if fecha_iso(c.get("fecha")) == fecha_n]
                if not por_id:
                    return None
            cands = por_id
    if fecha_n:
        mismos = [c for c in cands if fecha_iso(c.get("fecha")) == fecha_n]
        if not mismos:
            return None
        cands = mismos
    if len(cands) > 1:
        if inicio:
            return _mas_cercana(cands, inicio)
        if fecha_n:
            return None
        return cands[0]
    if len(cands) == 1:
        unico = cands[0]
        if inicio and (unico.get("inicio") or unico.get("commence_time")):
            return _mas_cercana([unico], inicio)
        return unico
    return None


def buscar_lineas_partido(
    mapa: dict[tuple[str, str], dict],
    visitante: str,
    home: str,
    *,
    fecha: Any = None,
    inicio: Any = None,
    evento_id: Any = None,
) -> dict | None:
    ka, kh = _match_key(visitante, home)
    fila = None
    if (ka, kh) in mapa:
        fila = mapa[(ka, kh)]
    elif (kh, ka) in mapa:
        fila = _swap_una(mapa[(kh, ka)])
        vars_ = mapa[(kh, ka)].get("variantes")
        if isinstance(vars_, list):
            fila["variantes"] = [_swap_una(v) for v in vars_ if isinstance(v, dict)]
    if not isinstance(fila, dict):
        return None
    if fecha is None and inicio is None and evento_id in (None, ""):
        return fila
    return seleccionar_linea_partido(fila, fecha=fecha, inicio=inicio, evento_id=evento_id)


def _juegos_con_cuota(juegos: list[dict]) -> int:
    return sum(
        1
        for j in juegos
        if j.get("odds_away_decimal") and j.get("odds_home_decimal")
    )


def aplicar_lineas_a_juegos(juegos: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    """Cadena de momios: ESPN scoreboard, ESPN header y Action Network."""
    from cadena_momios import aplicar_cadena_momios

    return aplicar_cadena_momios(juegos, cfg)
