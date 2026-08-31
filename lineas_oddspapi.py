"""
Cuotas MLB vía OddsPapi (https://oddspapi.io).

Prioriza API v5 (https://v5.oddspapi.io) — keys nuevas del dashboard.
Fallback a v4 (https://api.oddspapi.io/v4) si v5 falla por auth/plan.

Env: ODDSPAPI_API_KEY (también ODDS_PAPI_KEY / lineas.api_key / archivo en DATA_DIR).
No usar ODDS_API_KEY (pertenece a The Odds API y rompe OddsPapi).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

from lineas_betmgm import (
    decimal_a_american,
    normalizar_nombre_equipo,
)

BASE_URL_V5 = "https://v5.oddspapi.io/en"
BASE_URL_V4 = "https://api.oddspapi.io/v4"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
KEY_FILE = BASE_DIR / "oddspapi_api_key.txt"
KEY_FILE_DATA = DATA_DIR / "oddspapi_api_key.txt"

SPORT_ID_BASEBALL = 13
TOURNAMENT_MLB = 109
# Baseball moneyline (2-way): marketId 131 → outcomes 131=home/1, 132=away/2
MARKET_MONEYLINE = 131
OUTCOME_HOME = {131, "131", "1", "home", "h", "participant1"}
OUTCOME_AWAY = {132, "132", "2", "away", "a", "participant2"}
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_cache: dict[tuple[str, str], dict[str, Any]] | None = None
_cache_ts: datetime | None = None
CACHE_MINUTES = 10

# Cortacircuito: no martillar OddsPapi en cada recarga del panel.
PAUSE_AUTH_MIN = 90  # 401 / 403 / key inválida
PAUSE_RATE_MIN = 30  # 429
PAUSE_NET_MIN = 10  # red / 5xx
PAUSE_MAX_MIN = 180


def _circuit_path() -> Path:
    d = Path(os.environ.get("DATA_DIR") or str(DATA_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d / "oddspapi_circuit.json"


def invalidar_cache_oddspapi() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = None


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return None


def minutos_pausa_por_fallo(
    http_status: int | None = None,
    retry_after_segundos: int | None = None,
    mensaje: str = "",
) -> int:
    st = int(http_status or 0)
    blob = (mensaje or "").lower()
    if st in (401, 403) or "invalid" in blob:
        return PAUSE_AUTH_MIN
    if st == 429 or "rate limit" in blob:
        if retry_after_segundos:
            mins = max(5, (int(retry_after_segundos) + 59) // 60)
            return min(PAUSE_MAX_MIN, mins)
        return PAUSE_RATE_MIN
    if st >= 500:
        return 15
    return PAUSE_NET_MIN


def fallo_abre_circuito(meta: dict[str, Any] | None) -> bool:
    """Solo fallos que queman cupo o no se arreglan solos en segundos."""
    meta = meta or {}
    st = int(meta.get("http_status") or 0)
    if st in (401, 403, 429) or st >= 500:
        return True
    blob = f"{meta.get('error_api') or ''} {meta.get('mensaje') or ''}".lower()
    return "invalid" in blob or "rate limit" in blob or " 429" in blob


def estado_circuito() -> dict[str, Any]:
    path = _circuit_path()
    if not path.exists():
        return {"abierto": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"abierto": False}
    hasta = _parse_dt(data.get("hasta"))
    if not hasta:
        return {"abierto": False}
    ahora = datetime.now()
    if ahora >= hasta:
        cerrar_circuito()
        return {"abierto": False, "expiro": True}
    restante = int((hasta - ahora).total_seconds() // 60) + 1
    motivo = redactar_secretos(data.get("motivo") or "OddsPapi falló")
    return {
        "abierto": True,
        "desde": data.get("desde"),
        "hasta": hasta.isoformat(timespec="minutes"),
        "hasta_hora": hasta.strftime("%H:%M"),
        "minutos_restantes": restante,
        "motivo": motivo,
        "http_status": data.get("http_status"),
        "mensaje": (
            f"OddsPapi en pausa automática hasta {hasta.strftime('%H:%M')} "
            f"({restante} min). Usando ESPN/DraftKings."
        ),
    }


def circuito_abierto() -> bool:
    return bool(estado_circuito().get("abierto"))


def abrir_circuito(
    motivo: str,
    http_status: int | None = None,
    minutos: int | None = None,
    retry_after_segundos: int | None = None,
) -> dict[str, Any]:
    mins = int(
        minutos
        if minutos is not None
        else minutos_pausa_por_fallo(http_status, retry_after_segundos, motivo)
    )
    mins = max(5, min(PAUSE_MAX_MIN, mins))
    ahora = datetime.now()
    hasta = ahora + timedelta(minutes=mins)
    data = {
        "abierto": True,
        "desde": ahora.isoformat(timespec="minutes"),
        "hasta": hasta.isoformat(timespec="minutes"),
        "motivo": redactar_secretos(motivo)[:180],
        "http_status": http_status,
        "minutos": mins,
    }
    try:
        _circuit_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return estado_circuito()


def cerrar_circuito() -> None:
    path = _circuit_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def registrar_fallo_circuito(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not fallo_abre_circuito(meta):
        return None
    meta = meta or {}
    st = estado_circuito()
    if st.get("abierto"):
        return st
    return abrir_circuito(
        motivo=str(meta.get("mensaje") or meta.get("error_api") or "OddsPapi falló"),
        http_status=int(meta.get("http_status") or 0) or None,
        retry_after_segundos=meta.get("retry_after_segundos"),
    )


def _norm(nombre: str) -> str:
    s = nombre.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return " ".join(s.split())


def _limpiar_key(raw: str) -> str:
    key = str(raw).strip().replace("\ufeff", "").replace("\r", "").replace("\n", "")
    key = unquote(key)
    if (key.startswith('"') and key.endswith('"')) or (key.startswith("'") and key.endswith("'")):
        key = key[1:-1].strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    # Pegaron "apiKey=xxxx" o URL completa
    m = re.search(r"(?:api[_-]?key=)([^&\s]+)", key, flags=re.I)
    if m:
        key = unquote(m.group(1)).strip()
    return key.strip()


def fingerprint_key(key: str | None) -> str | None:
    if not key:
        return None
    k = key.strip()
    if len(k) <= 8:
        return f"*** (len={len(k)})"
    return f"{k[:4]}…{k[-4:]} (len={len(k)})"


_RE_APIKEY_QS = re.compile(r"(api[_-]?key=)([^&\s]+)", re.I)
_RE_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def redactar_secretos(texto: Any) -> str:
    """Quita API keys de mensajes/logs (el 429 de requests incluye la URL con apiKey=)."""
    s = str(texto or "")
    s = _RE_APIKEY_QS.sub(r"\1***", s)
    s = _RE_UUID.sub("********-****-****-****-************", s)
    return s


def _http_error_corto(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None):
        return f"HTTP {resp.status_code}"
    return redactar_secretos(exc)[:140]


def _retry_after_segundos(resp: Any) -> int | None:
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(1, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        return None


def _anotar_http(meta: dict[str, Any], resp: Any) -> None:
    if resp is None:
        return
    meta["http_status"] = getattr(resp, "status_code", None)
    ra = _retry_after_segundos(resp)
    if ra:
        meta["retry_after_segundos"] = ra


def _score_key(key: str) -> int:
    """Prioriza UUID completa / keys largas; penaliza pegados truncados."""
    if not key:
        return -1
    if key.upper() in ("YOUR_API_KEY", "APIKEY", "XXX", "CHANGEME"):
        return -1
    if _UUID_RE.match(key):
        return 100
    n = len(key)
    if n >= 32:
        return 80
    if n >= 20:
        return 50
    if n >= 12:
        return 20
    return 5  # probablemente truncada (ej. solo primer bloque de UUID)


def guardar_api_key(key: str) -> dict[str, Any]:
    """Guarda key en disco persistente (DATA_DIR) y limpia cache."""
    limpia = _limpiar_key(key)
    if _score_key(limpia) < 50:
        raise ValueError(
            f"Key demasiado corta o incompleta (len={len(limpia)}). "
            "Una key OddsPapi suele tener 36 caracteres (UUID con guiones)."
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE_DATA.write_text(limpia + "\n", encoding="utf-8")
    try:
        KEY_FILE.write_text(limpia + "\n", encoding="utf-8")
    except OSError:
        pass
    invalidar_cache_oddspapi()
    cerrar_circuito()

    # Aviso: si Render tiene ODDSPAPI_API_KEY distinta, antes tapaba la rotación.
    # Ahora el archivo DATA_DIR gana; igual conviene borrar la env vieja.
    env_raw = os.environ.get("ODDSPAPI_API_KEY") or os.environ.get("ODDS_PAPI_KEY")
    env_fp = None
    aviso_env = None
    if env_raw:
        env_limpia = _limpiar_key(str(env_raw))
        env_fp = fingerprint_key(env_limpia)
        if env_limpia and env_limpia != limpia:
            aviso_env = (
                "Hay otra key en variable de entorno Render. "
                "Se usará la del disco (esta). Borra ODDSPAPI_API_KEY en Render "
                "para no confundirte."
            )

    return {
        "ok": True,
        "key_fingerprint": fingerprint_key(limpia),
        "key_length": len(limpia),
        "path": str(KEY_FILE_DATA),
        "circuito_cerrado": True,
        "prioridad": "DATA_DIR",
        "env_fingerprint": env_fp,
        "aviso_env": aviso_env,
    }


def cargar_api_key(cfg: dict) -> str | None:
    """Keys de OddsPapi únicamente. NO usa ODDS_API_KEY (era The Odds API y tapa la buena).

    Prioridad: disco DATA_DIR (rotación vía Action/API) > env > resto.
    Así rotar la key no queda tapada por una ODDSPAPI_API_KEY vieja en Render.
    """
    lineas = cfg.get("lineas") or {}
    candidates = (
        (
            "oddspapi_api_key.txt (DATA_DIR)",
            KEY_FILE_DATA.read_text(encoding="utf-8") if KEY_FILE_DATA.exists() else None,
        ),
        ("ODDSPAPI_API_KEY", os.environ.get("ODDSPAPI_API_KEY")),
        ("ODDS_PAPI_KEY", os.environ.get("ODDS_PAPI_KEY")),
        ("lineas.api_key", lineas.get("api_key")),
        (
            "oddspapi_api_key.txt",
            KEY_FILE.read_text(encoding="utf-8") if KEY_FILE.exists() else None,
        ),
    )
    # (score, orden_preferencia, source, key) — a igual score gana el primero de la lista
    scored: list[tuple[int, int, str, str]] = []
    for idx, (source, candidate) in enumerate(candidates):
        if candidate is None:
            continue
        key = _limpiar_key(str(candidate))
        sc = _score_key(key)
        if sc < 0:
            continue
        scored.append((sc, idx, source, key))

    if not scored:
        cargar_api_key.last_source = None  # type: ignore[attr-defined]
        cargar_api_key.last_score = None  # type: ignore[attr-defined]
        return None

    scored.sort(key=lambda x: (-x[0], x[1]))
    best_score, _idx, source, key = scored[0]
    cargar_api_key.last_source = source  # type: ignore[attr-defined]
    cargar_api_key.last_score = best_score  # type: ignore[attr-defined]
    return key


cargar_api_key.last_source = None  # type: ignore[attr-defined]
cargar_api_key.last_score = None  # type: ignore[attr-defined]


def _parse_api_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "")[:160]
    if not isinstance(data, dict):
        return str(data)[:160]
    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code") or err.get("reason") or ""
        msg = err.get("message") or err.get("details") or ""
        return f"{code}: {msg}".strip(": ")[:160]
    code = data.get("code") or data.get("reason") or ""
    msg = data.get("message") or ""
    if code or msg:
        return f"{code}: {msg}".strip(": ")[:160]
    return str(data)[:160]


def _book_keys(cfg: dict) -> list[str]:
    lineas_cfg = cfg.get("lineas") or {}
    books = lineas_cfg.get("bookmakers") or lineas_cfg.get("casa") or "pinnacle,draftkings"
    if isinstance(books, list):
        return [str(b).strip().lower() for b in books if str(b).strip()]
    return [b.strip().lower() for b in str(books).split(",") if b.strip()]


def _extraer_ml_libro_v4(bookie: dict) -> tuple[float | None, float | None]:
    markets = bookie.get("markets") or {}
    market = markets.get(str(MARKET_MONEYLINE)) or markets.get(MARKET_MONEYLINE)
    if not market:
        # Fallback: primer market 2-way con home/away
        for mid, m in markets.items():
            if not isinstance(m, dict):
                continue
            h, a = _outcomes_home_away_v4(m)
            if h and a:
                return h, a
        return None, None
    return _outcomes_home_away_v4(market)


def _outcomes_home_away_v4(market: dict) -> tuple[float | None, float | None]:
    home_px = away_px = None
    for outcome in (market.get("outcomes") or {}).values():
        if not isinstance(outcome, dict):
            continue
        for player in (outcome.get("players") or {}).values():
            if not isinstance(player, dict) or not player.get("active", True):
                continue
            boid = str(player.get("bookmakerOutcomeId") or "").lower()
            price = player.get("price")
            if price is None:
                continue
            try:
                dec = float(price)
            except (TypeError, ValueError):
                continue
            if dec <= 1.0:
                continue
            if boid in OUTCOME_HOME or "home" in boid:
                home_px = dec
            elif boid in OUTCOME_AWAY or "away" in boid:
                away_px = dec
    return home_px, away_px


def _extraer_ml_libro_v5(odds_map: dict) -> tuple[float | None, float | None]:
    """odds_map: {oddsKey: OddQuote}."""
    home_px = away_px = None
    # 1) market moneyline 131/132
    for quote in (odds_map or {}).values():
        if not isinstance(quote, dict) or quote.get("active") is False:
            continue
        price = quote.get("price")
        try:
            dec = float(price)
        except (TypeError, ValueError):
            continue
        if dec <= 1.0:
            continue
        oid = quote.get("outcomeId")
        boid = str(quote.get("bookmakerOutcomeId") or "").lower()
        mid = quote.get("marketId")
        if mid not in (None, MARKET_MONEYLINE, str(MARKET_MONEYLINE)) and oid not in (
            MARKET_MONEYLINE,
            MARKET_MONEYLINE + 1,
            str(MARKET_MONEYLINE),
            str(MARKET_MONEYLINE + 1),
        ):
            # Si no es moneyline conocido, aún puede mapear por boid
            if boid not in OUTCOME_HOME and boid not in OUTCOME_AWAY and boid not in ("1", "2"):
                continue
        if oid in OUTCOME_HOME or boid in OUTCOME_HOME or str(oid) == "1":
            home_px = dec
        elif oid in OUTCOME_AWAY or boid in OUTCOME_AWAY or str(oid) == "2":
            away_px = dec
    if home_px and away_px:
        return home_px, away_px

    # 2) Agrupar por marketId con exactamente 2 precios activos
    by_market: dict[Any, list[tuple[Any, float, str]]] = {}
    for quote in (odds_map or {}).values():
        if not isinstance(quote, dict) or quote.get("active") is False:
            continue
        try:
            dec = float(quote.get("price"))
        except (TypeError, ValueError):
            continue
        if dec <= 1.0:
            continue
        mid = quote.get("marketId")
        if mid is None:
            continue
        by_market.setdefault(mid, []).append(
            (quote.get("outcomeId"), dec, str(quote.get("bookmakerOutcomeId") or "").lower())
        )
    for mid, rows in by_market.items():
        if len(rows) != 2:
            continue
        # Prefer markets cuyo id empiece como baseball moneyline (13x)
        try:
            mid_i = int(mid)
        except (TypeError, ValueError):
            mid_i = 0
        if mid_i and not (130 <= mid_i <= 199) and mid_i != MARKET_MONEYLINE:
            continue
        h = a = None
        for oid, dec, boid in rows:
            if oid in OUTCOME_HOME or boid in OUTCOME_HOME or str(oid).endswith("1") or boid == "1":
                h = dec
            elif oid in OUTCOME_AWAY or boid in OUTCOME_AWAY or str(oid).endswith("2") or boid == "2":
                a = dec
        if h and a:
            return h, a
        # Orden canónico: menor outcomeId = home
        rows_sorted = sorted(rows, key=lambda r: (r[0] is None, r[0]))
        return rows_sorted[0][1], rows_sorted[1][1]
    return None, None


def _mejor_ml_fixture(bookmaker_odds: dict, book_keys: list[str], *, api: str = "v4") -> dict | None:
    """Mejor cuota decimal home/away entre books preferidos (o todos si no hay match)."""
    preferidos = [b.strip().lower() for b in book_keys if b.strip()]
    candidatos: list[tuple[str, float, float]] = []

    def _scan(solo_preferidos: bool) -> None:
        for slug, bookie in (bookmaker_odds or {}).items():
            if not isinstance(bookie, dict):
                continue
            if solo_preferidos and preferidos and slug.lower() not in preferidos:
                continue
            if api == "v5":
                h, a = _extraer_ml_libro_v5(bookie)
            else:
                h, a = _extraer_ml_libro_v4(bookie)
            if h and a:
                candidatos.append((slug, h, a))

    _scan(True)
    if not candidatos and preferidos:
        _scan(False)
    if not candidatos:
        return None

    best_home = max(candidatos, key=lambda x: x[1])
    best_away = max(candidatos, key=lambda x: x[2])
    libros = [
        {
            "casa": slug,
            "away": round(a, 3),
            "home": round(h, 3),
        }
        for slug, h, a in candidatos[:8]
    ]
    return {
        "home": {
            "decimal": round(best_home[1], 3),
            "american": decimal_a_american(best_home[1]),
            "casa": best_home[0],
        },
        "away": {
            "decimal": round(best_away[2], 3),
            "american": decimal_a_american(best_away[2]),
            "casa": best_away[0],
        },
        "libros": libros,
    }


def _resolver_tournament_mlb_v5(api_key: str, sport_id: int, preferred: int) -> tuple[int, str]:
    """Devuelve (tournament_id, nota). Si falla discovery, usa preferred."""
    try:
        r = requests.get(
            f"{BASE_URL_V5}/tournaments",
            params={"apiKey": api_key, "sportIds": str(sport_id)},
            timeout=20,
        )
        if r.status_code >= 400:
            return preferred, f"tournaments HTTP {r.status_code}"
        data = r.json()
        rows = data if isinstance(data, list) else (data.get("tournaments") or data.get("data") or [])
        mlb = []
        for t in rows or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("tournamentName") or t.get("name") or "")
            slug = str(t.get("tournamentSlug") or t.get("slug") or "")
            tid = t.get("tournamentId") or t.get("id")
            if tid is None:
                continue
            blob = f"{name} {slug}".upper()
            if "MLB" in blob or "MAJOR LEAGUE" in blob:
                mlb.append((int(tid), name or slug))
        if not mlb:
            return preferred, "sin MLB en /tournaments"
        # Prefer exact preferred id if listed
        for tid, name in mlb:
            if tid == preferred:
                return tid, f"{name} ({tid})"
        return mlb[0][0], f"{mlb[0][1]} ({mlb[0][0]})"
    except requests.RequestException as e:
        return preferred, f"tournaments error: {e}"


def _obtener_v5(cfg: dict, api_key: str, meta: dict) -> tuple[dict[tuple[str, str], dict], dict]:
    lineas_cfg = cfg.get("lineas") or {}
    sport_id = int(lineas_cfg.get("sport_id") or SPORT_ID_BASEBALL)
    preferred_tid = int(lineas_cfg.get("tournament_id") or TOURNAMENT_MLB)
    book_keys = _book_keys(cfg)
    casa = str(lineas_cfg.get("casa") or (book_keys[0] if book_keys else "pinnacle")).lower()

    tournament_id, tnote = _resolver_tournament_mlb_v5(api_key, sport_id, preferred_tid)
    meta["tournament_id"] = tournament_id
    meta["tournament_note"] = tnote

    params: dict[str, Any] = {
        "apiKey": api_key,
        "tournamentId": tournament_id,
    }
    # v5 acepta varios bookmakers; limitar a preferidos para respuesta más chica
    if book_keys:
        params["bookmakers"] = ",".join(book_keys[:4])

    r = requests.get(f"{BASE_URL_V5}/fixtures/odds/main", params=params, timeout=35)
    _anotar_http(meta, r)
    meta["api_version"] = "v5"
    if r.status_code in (401, 403):
        meta["mensaje"] = f"API key OddsPapi inválida o sin permiso (v5): {_parse_api_error(r)}"
        meta["error_api"] = _parse_api_error(r)
        return {}, meta
    if r.status_code == 429:
        meta["mensaje"] = "rate limit OddsPapi v5"
        return {}, meta
    if r.status_code >= 400:
        meta["mensaje"] = f"OddsPapi v5 HTTP {r.status_code}: {_parse_api_error(r)}"
        meta["error_api"] = _parse_api_error(r)
        return {}, meta

    rows = r.json()
    if not isinstance(rows, list):
        rows = rows.get("fixtures") or rows.get("data") or []

    mapa: dict[tuple[str, str], dict] = {}
    books_ok: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        parts = row.get("participants") or {}
        if isinstance(parts, dict):
            p1 = parts.get("participant1Name") or ""
            p2 = parts.get("participant2Name") or ""
        else:
            p1 = row.get("participant1Name") or ""
            p2 = row.get("participant2Name") or ""
        if not p1 or not p2:
            continue
        # v5: participant1 = home, participant2 = away
        odds = row.get("odds") or {}
        if not isinstance(odds, dict) or not odds:
            continue
        ml = _mejor_ml_fixture(odds, book_keys or [casa], api="v5")
        if not ml:
            continue
        n_home = normalizar_nombre_equipo(p1)
        n_away = normalizar_nombre_equipo(p2)
        mapa[(n_away, n_home)] = {
            "away": {**ml["away"], "lado": "away", "nombre": p2},
            "home": {**ml["home"], "lado": "home", "nombre": p1},
            "libros": list(ml.get("libros") or []),
        }
        for lado in ("away", "home"):
            casa_l = (ml.get(lado) or {}).get("casa")
            if casa_l:
                books_ok.add(str(casa_l))

    if not mapa:
        meta["mensaje"] = (
            f"OddsPapi v5 sin moneyline MLB (tournament={tournament_id}, "
            f"fixtures={len(rows or [])}, {tnote})"
        )
        meta["fixtures_mlb"] = len(rows or [])
        return {}, meta

    meta["ok"] = True
    meta["partidos"] = len(mapa)
    meta["fixtures_mlb"] = len(rows or [])
    meta["bookmakers"] = sorted(books_ok) or book_keys or [casa]
    meta["mensaje"] = (
        f"{len(mapa)} partidos OddsPapi MLB v5 · books {','.join(meta['bookmakers'])} "
        f"· tournament {tournament_id}"
    )
    return mapa, meta


def _obtener_v4(cfg: dict, api_key: str, meta: dict) -> tuple[dict[tuple[str, str], dict], dict]:
    """Legacy v4: /fixtures + /odds-by-tournaments (1 bookmaker por request)."""
    lineas_cfg = cfg.get("lineas") or {}
    sport_id = int(lineas_cfg.get("sport_id") or SPORT_ID_BASEBALL)
    tournament_id = int(lineas_cfg.get("tournament_id") or TOURNAMENT_MLB)
    book_keys = _book_keys(cfg)
    casa = str(lineas_cfg.get("casa") or (book_keys[0] if book_keys else "pinnacle")).lower()
    ahora = datetime.now()
    hoy = ahora.strftime("%Y-%m-%d")
    manana = (ahora + timedelta(days=1)).strftime("%Y-%m-%d")

    meta["api_version"] = "v4"
    r_fix = requests.get(
        f"{BASE_URL_V4}/fixtures",
        params={
            "apiKey": api_key,
            "sportId": sport_id,
            "from": hoy,
            "to": manana,
        },
        timeout=25,
    )
    _anotar_http(meta, r_fix)
    if r_fix.status_code in (401, 403):
        meta["mensaje"] = f"API key OddsPapi inválida o sin permiso (v4): {_parse_api_error(r_fix)}"
        meta["error_api"] = _parse_api_error(r_fix)
        return {}, meta
    if r_fix.status_code == 429:
        meta["mensaje"] = "OddsPapi v4 saturado (429)"
        return {}, meta
    if r_fix.status_code >= 400:
        meta["mensaje"] = f"OddsPapi v4 HTTP {r_fix.status_code}: {_parse_api_error(r_fix)}"
        meta["error_api"] = _parse_api_error(r_fix)
        return {}, meta
    fixtures = r_fix.json()
    if not isinstance(fixtures, list):
        fixtures = fixtures.get("fixtures") or fixtures.get("data") or []

    names_by_id: dict[str, tuple[str, str]] = {}
    for fx in fixtures or []:
        if not isinstance(fx, dict):
            continue
        tid = fx.get("tournamentId")
        tname = str(fx.get("tournamentName") or "")
        if tid not in (tournament_id, str(tournament_id)) and "MLB" not in tname.upper():
            continue
        fid = str(fx.get("fixtureId") or "")
        p1 = fx.get("participant1Name") or ""
        p2 = fx.get("participant2Name") or ""
        if fid and p1 and p2:
            names_by_id[fid] = (p1, p2)

    extras = [b for b in book_keys if b.lower() != casa]
    books_a_probar = [casa] + extras[:1]
    odds_by_fid: dict[str, dict] = {}
    books_ok: list[str] = []
    ultimo_error = ""

    for bi, book in enumerate(books_a_probar):
        if bi > 0:
            time.sleep(1.1)
        r_odds = requests.get(
            f"{BASE_URL_V4}/odds-by-tournaments",
            params={
                "apiKey": api_key,
                "tournamentIds": str(tournament_id),
                "oddsFormat": "decimal",
                "bookmaker": book,
            },
            timeout=35,
        )
        meta["http_status"] = r_odds.status_code
        if r_odds.status_code in (401, 403):
            meta["mensaje"] = f"API key OddsPapi inválida (v4 odds): {_parse_api_error(r_odds)}"
            meta["error_api"] = _parse_api_error(r_odds)
            return {}, meta
        if r_odds.status_code == 429:
            ultimo_error = "rate limit OddsPapi v4"
            time.sleep(1.5)
            continue
        if r_odds.status_code >= 400:
            ultimo_error = _parse_api_error(r_odds)
            continue
        odds_list = r_odds.json()
        if not isinstance(odds_list, list):
            odds_list = odds_list.get("fixtures") or odds_list.get("data") or []
        n_add = 0
        for row in odds_list or []:
            if not isinstance(row, dict):
                continue
            fid = str(row.get("fixtureId") or "")
            if not fid:
                continue
            prev = odds_by_fid.get(fid) or {"bookmakerOdds": {}}
            merged_books = dict(prev.get("bookmakerOdds") or {})
            merged_books.update(row.get("bookmakerOdds") or {})
            odds_by_fid[fid] = {**row, "bookmakerOdds": merged_books}
            n_add += 1
        if n_add:
            books_ok.append(book)
        if books_ok and bi == 0:
            break

    if not odds_by_fid:
        meta["mensaje"] = ultimo_error or "OddsPapi v4 sin cuotas MLB"
        return {}, meta

    mapa: dict[tuple[str, str], dict] = {}
    for fid, row in odds_by_fid.items():
        p1, p2 = names_by_id.get(fid, ("", ""))
        p1 = row.get("participant1Name") or p1
        p2 = row.get("participant2Name") or p2
        if not p1 or not p2:
            continue
        ml = _mejor_ml_fixture(row.get("bookmakerOdds") or {}, books_ok or book_keys, api="v4")
        if not ml:
            continue
        n_home = normalizar_nombre_equipo(p1)
        n_away = normalizar_nombre_equipo(p2)
        mapa[(n_away, n_home)] = {
            "away": {**ml["away"], "lado": "away", "nombre": p2},
            "home": {**ml["home"], "lado": "home", "nombre": p1},
        }

    if not mapa:
        meta["mensaje"] = "OddsPapi v4: fixtures OK pero sin moneyline parseable"
        meta["fixtures_mlb"] = len(names_by_id)
        return {}, meta

    meta["ok"] = True
    meta["partidos"] = len(mapa)
    meta["fixtures_mlb"] = len(names_by_id)
    meta["bookmakers"] = books_ok or [casa]
    meta["mensaje"] = (
        f"{len(mapa)} partidos OddsPapi MLB v4 · books {','.join(meta['bookmakers'])} "
        f"· fixtures {len(names_by_id)}"
    )
    return mapa, meta


def probar_conexion_oddspapi(cfg: dict) -> dict[str, Any]:
    """Llamada ligera (tournaments v5) para validar la key sin traer todas las cuotas."""
    meta: dict[str, Any] = {"ok": False, "fuente": "oddspapi", "probe": True}
    if circuito_abierto():
        st = estado_circuito()
        meta.update(
            {
                "circuito": True,
                "motivo": "circuito_abierto",
                "hasta_hora": st.get("hasta_hora"),
                "http_status": st.get("http_status"),
                "mensaje": st.get("mensaje"),
            }
        )
        return meta

    api_key = cargar_api_key(cfg)
    if not api_key:
        meta["mensaje"] = "sin key OddsPapi"
        return meta

    meta["key_fingerprint"] = fingerprint_key(api_key)
    try:
        r = requests.get(
            f"{BASE_URL_V5}/tournaments",
            params={"apiKey": api_key, "sportIds": str(SPORT_ID_BASEBALL)},
            timeout=15,
        )
    except requests.RequestException as e:
        meta["mensaje"] = _http_error_corto(e)
        return meta

    if r.status_code >= 400:
        err = _parse_api_error(r)
        meta.update({"http_status": r.status_code, "mensaje": err, "error_api": err})
        extra = registrar_fallo_circuito(meta)
        if extra and extra.get("abierto"):
            meta["circuito"] = True
            meta["circuito_hasta_hora"] = extra.get("hasta_hora")
        return meta

    cerrar_circuito()
    invalidar_cache_oddspapi()
    meta["ok"] = True
    meta["mensaje"] = "OddsPapi key OK (probe)"
    meta["circuito_cerrado"] = True
    return meta


def intentar_reabrir_oddspapi_si_expirado(cfg: dict) -> dict[str, Any]:
    """Tras vencer la pausa del circuito, prueba una vez si la key volvió a funcionar."""
    st = estado_circuito()
    if st.get("abierto"):
        return {"ok": False, "omitido": True, "motivo": "circuito_abierto", **st}
    if not st.get("expiro"):
        return {"ok": True, "omitido": True, "motivo": "sin_expiracion_reciente"}
    return probar_conexion_oddspapi(cfg)


def obtener_lineas_oddspapi(cfg: dict) -> tuple[dict[tuple[str, str], dict], dict]:
    """
    mapa: (away_norm, home_norm) -> {away: {...}, home: {...}}
    """
    global _cache, _cache_ts
    meta: dict[str, Any] = {
        "ok": False,
        "fuente": "oddspapi",
        "mensaje": "",
        "partidos": 0,
        "proveedor": "oddspapi",
    }
    circ = estado_circuito()
    if circ.get("abierto"):
        meta["circuito"] = True
        meta["circuito_hasta"] = circ.get("hasta")
        meta["circuito_hasta_hora"] = circ.get("hasta_hora")
        meta["http_status"] = circ.get("http_status")
        meta["mensaje"] = circ.get("mensaje") or "OddsPapi en pausa automática"
        return {}, meta

    api_key = cargar_api_key(cfg)
    if not api_key:
        meta["mensaje"] = "Falta ODDSPAPI_API_KEY en Render (oddspapi.io)"
        return {}, meta

    meta["key_fingerprint"] = fingerprint_key(api_key)
    meta["key_source"] = getattr(cargar_api_key, "last_source", None)
    meta["key_length"] = len(api_key)
    meta["key_score"] = getattr(cargar_api_key, "last_score", None)
    if (meta["key_score"] or 0) < 50:
        meta["mensaje"] = (
            f"ODDSPAPI_API_KEY incompleta (len={len(api_key)}). "
            "Debe ser la UUID completa (~36 caracteres). "
            "Usa GitHub Action 'Configurar OddsPapi' o pégala entera en Render."
        )
        meta["http_status"] = 400
        return {}, meta

    ahora = datetime.now()
    if _cache is not None and _cache_ts and ahora - _cache_ts < timedelta(minutes=CACHE_MINUTES):
        return _cache, {
            **meta,
            "ok": True,
            "partidos": len(_cache),
            "cache": True,
            "mensaje": f"{len(_cache)} partidos (cache)",
        }

    prefer = str((cfg.get("lineas") or {}).get("api_version") or "v5").lower()
    orden = ["v5", "v4"] if prefer != "v4" else ["v4", "v5"]
    errores: list[str] = []

    for ver in orden:
        try:
            if ver == "v5":
                mapa, meta_try = _obtener_v5(cfg, api_key, dict(meta))
            else:
                mapa, meta_try = _obtener_v4(cfg, api_key, dict(meta))
        except requests.RequestException as e:
            errores.append(f"{ver}: {_http_error_corto(e)}")
            resp = getattr(e, "response", None)
            if resp is not None:
                _anotar_http(meta, resp)
            continue
        if meta_try.get("ok") and mapa:
            _cache = mapa
            _cache_ts = ahora
            cerrar_circuito()
            return mapa, meta_try
        errores.append(f"{ver}: {redactar_secretos(meta_try.get('mensaje') or 'sin datos')}")
        meta.update({k: v for k, v in meta_try.items() if k not in ("ok",)})
        # Misma key: no martillar v4 tras 401/429 (quema el cupo y filtra la URL).
        if int(meta_try.get("http_status") or 0) in (401, 403, 429):
            break
        if "invalid" in str(meta_try.get("error_api") or meta_try.get("mensaje") or "").lower():
            break

    meta["ok"] = False
    meta["mensaje"] = redactar_secretos(" | ".join(errores)[:180] or "OddsPapi sin cuotas MLB")
    meta["intentos"] = [redactar_secretos(x) for x in errores]
    extra = registrar_fallo_circuito(meta)
    if extra and extra.get("abierto"):
        meta["circuito"] = True
        meta["circuito_hasta"] = extra.get("hasta")
        meta["circuito_hasta_hora"] = extra.get("hasta_hora")
        meta["mensaje"] = extra.get("mensaje") or meta["mensaje"]
    return {}, meta


def buscar_lineas_partido(
    mapa: dict[tuple[str, str], dict], visitante: str, home: str
) -> dict | None:
    ka, kh = normalizar_nombre_equipo(visitante), normalizar_nombre_equipo(home)
    if (ka, kh) in mapa:
        return mapa[(ka, kh)]
    if (kh, ka) in mapa:
        m = mapa[(kh, ka)]
        libros = m.get("libros") if isinstance(m.get("libros"), list) else []
        libros_flip = [
            {
                "casa": b.get("casa"),
                "away": b.get("home"),
                "home": b.get("away"),
            }
            for b in libros
            if isinstance(b, dict)
        ]
        return {
            "away": m.get("home"),
            "home": m.get("away"),
            "libros": libros_flip,
        }
    for (a, h), fila in mapa.items():
        if {a, h} == {ka, kh}:
            if a == ka:
                return fila
            libros = fila.get("libros") if isinstance(fila.get("libros"), list) else []
            return {
                "away": fila.get("home"),
                "home": fila.get("away"),
                "libros": [
                    {
                        "casa": b.get("casa"),
                        "away": b.get("home"),
                        "home": b.get("away"),
                    }
                    for b in libros
                    if isinstance(b, dict)
                ],
            }
    return None


def aplicar_lineas_oddspapi(juegos: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    if circuito_abierto():
        st = estado_circuito()
        return juegos, {
            "ok": False,
            "fuente": "oddspapi",
            "circuito": True,
            "circuito_hasta": st.get("hasta"),
            "circuito_hasta_hora": st.get("hasta_hora"),
            "http_status": st.get("http_status"),
            "mensaje": st.get("mensaje") or "OddsPapi en pausa automática",
            "partidos": 0,
        }
    mapa, meta = obtener_lineas_oddspapi(cfg)
    if meta.get("circuito") or not meta.get("ok"):
        # No borrar cuotas previas ni dejar todo en cero si vamos a ESPN.
        return juegos, meta
    for juego in juegos:
        lineas = buscar_lineas_partido(mapa, juego["visitante"], juego["home"])
        juego["lineas_betmgm"] = lineas
        juego["odds_away_american"] = None
        juego["odds_home_american"] = None
        juego["odds_away_decimal"] = None
        juego["odds_home_decimal"] = None
        juego["lineas_fuente"] = "modelo"

        if lineas:
            away_l = lineas.get("away")
            home_l = lineas.get("home")
            if away_l:
                juego["odds_away_american"] = away_l.get("american")
                juego["odds_away_decimal"] = away_l.get("decimal")
            if home_l:
                juego["odds_home_american"] = home_l.get("american")
                juego["odds_home_decimal"] = home_l.get("decimal")
            juego["lineas_fuente"] = (
                (away_l or {}).get("casa")
                or (home_l or {}).get("casa")
                or "oddspapi"
            )
            libros = lineas.get("libros") if isinstance(lineas.get("libros"), list) else []
            if libros:
                juego["lineas_libros"] = libros
            elif away_l and home_l and away_l.get("decimal") and home_l.get("decimal"):
                juego["lineas_libros"] = [
                    {
                        "casa": juego["lineas_fuente"],
                        "away": float(away_l["decimal"]),
                        "home": float(home_l["decimal"]),
                    }
                ]
    return juegos, meta
