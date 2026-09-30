"""La app informa si leyó ODDS_API_KEY, sin mostrar la clave ni gastar cuota."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
import requests
from fastapi.testclient import TestClient

import lineas_betmgm as lb
import servidor_mlb as srv

CLAVE = "Qk9zT3V0T2ZkczEyMzQ1Njc4OTA"


@asynccontextmanager
async def _sin_motor(_app):
    yield


class _Resp:
    def __init__(self, status: int, body=None, restantes: str | None = "87"):
        self.status_code = status
        self._body = [] if body is None else body
        self.headers = {}
        if restantes is not None:
            self.headers["x-requests-remaining"] = restantes

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


@pytest.fixture
def aislado(monkeypatch, tmp_path):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.setattr(lb, "KEY_FILE", tmp_path / "odds_api_key.txt")
    monkeypatch.setattr(srv.app.router, "lifespan_context", _sin_motor)
    lb.reset_estado_odds_api()
    lb._cache = None
    lb._cache_ts = None
    lb._cache_por_casa = None
    lb._cache_por_casa_ts = None
    yield
    lb.reset_estado_odds_api()


def _espn_ok():
    return {}, {"ok": True, "partidos": 2, "mensaje": "2 partidos ESPN"}


def _sin_red(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("esta ruta no debe llamar a The Odds API")

    monkeypatch.setattr(lb.requests, "get", boom)


def test_clave_ausente_en_todos_los_proveedores(aislado, monkeypatch):
    monkeypatch.setattr("lineas_espn.obtener_lineas_espn", lambda: _espn_ok())
    _sin_red(monkeypatch)
    with TestClient(srv.app) as client:
        odds = client.get("/api/odds-status").json()
        health = client.get("/api/health").json()
    assert odds["proveedor"] == "espn"
    assert odds["key_presente"] is False
    assert odds["key_preview"] is None
    assert odds["fuente"] is None
    assert odds["partidos"] == 2
    bloque = health["odds"]["odds_api"]
    assert bloque["key_presente"] is False
    assert bloque["fuente"] is None
    assert bloque["key_preview"] is None
    assert bloque["ok"] is None
    assert bloque["http_status"] is None
    assert bloque["requests_restantes"] is None
    assert "mensaje" not in bloque


def test_clave_presente_y_valida(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)
    monkeypatch.setattr("lineas_espn.obtener_lineas_espn", lambda: _espn_ok())
    urls: list[str] = []

    def fake_get(url, params=None, timeout=None):
        urls.append(url)
        assert (params or {}).get("apiKey") == CLAVE
        return _Resp(200, body=[{"key": "baseball_mlb"}], restantes="42")

    monkeypatch.setattr(lb.requests, "get", fake_get)
    lb.validar_clave_odds_api(srv.cargar_config(), forzar=True)
    assert urls == [lb.SPORTS_URL]
    with TestClient(srv.app) as client:
        odds = client.get("/api/odds-status").json()
        health = client.get("/api/health").json()
    assert urls == [lb.SPORTS_URL]
    assert odds["proveedor"] == "espn"
    assert odds["key_presente"] is True
    assert odds["fuente"] == "ODDS_API_KEY"
    assert odds["key_len"] == len(CLAVE)
    assert odds["key_preview"] != CLAVE
    api = health["odds"]["odds_api"]
    assert api["key_presente"] is True
    assert api["fuente"] == "ODDS_API_KEY"
    assert api["ok"] is True
    assert api["error"] is None
    assert api["http_status"] == 200
    assert api["requests_restantes"] == "42"
    assert api["timestamp"]
    assert api["key_preview"].startswith(CLAVE[:4])
    assert api["key_preview"].endswith(CLAVE[-4:])
    assert "mensaje" not in api
    texto = json.dumps({"odds": odds, "health": health}, ensure_ascii=False)
    assert CLAVE not in texto
    assert "apiKey=" not in texto


def test_clave_invalida_401(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)
    monkeypatch.setattr("lineas_espn.obtener_lineas_espn", lambda: _espn_ok())

    def fake_get(url, params=None, timeout=None):
        return _Resp(
            401,
            body={"error_code": "INVALID_KEY", "message": f"Invalid key {CLAVE}"},
            restantes="3",
        )

    monkeypatch.setattr(lb.requests, "get", fake_get)
    lb.validar_clave_odds_api({}, forzar=True)
    with TestClient(srv.app) as client:
        health = client.get("/api/health").json()
        odds = client.get("/api/odds-status").json()
    api = health["odds"]["odds_api"]
    assert api["key_presente"] is True
    assert api["ok"] is False
    assert api["error"] == "invalid_key"
    assert api["http_status"] == 401
    assert "inválida" in api["mensaje"]
    assert "401" in api["mensaje"]
    texto = json.dumps({"health": health, "odds": odds}, ensure_ascii=False)
    assert CLAVE not in texto


def test_sin_cuota_mensaje_en_espanol(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)

    def fake_get(url, params=None, timeout=None):
        return _Resp(
            401,
            body={"error_code": "OUT_OF_USAGE_CREDITS", "message": "Usage quota has been reached"},
            restantes="0",
        )

    monkeypatch.setattr(lb.requests, "get", fake_get)
    lb.validar_clave_odds_api({}, forzar=True)
    api = srv.api_health()["odds"]["odds_api"]
    assert api["ok"] is False
    assert api["error"] == "sin_cuota"
    assert api["http_status"] == 401
    assert "sin cuota" in api["mensaje"]
    assert CLAVE not in json.dumps(api, ensure_ascii=False)


def test_429_tambien_es_sin_cuota(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)

    def fake_get(url, params=None, timeout=None):
        return _Resp(429, body={"message": "rate limit"}, restantes="0")

    monkeypatch.setattr(lb.requests, "get", fake_get)
    _mapas, meta = lb.obtener_mapas_por_casa({"lineas": {"bookmakers": "draftkings"}})
    assert meta["http_status"] == 429
    api = srv.api_health()["odds"]["odds_api"]
    assert api["ok"] is False
    assert api["error"] == "sin_cuota"
    assert api["requests_restantes"] == "0"
    assert "sin cuota" in api["mensaje"]
    assert CLAVE not in json.dumps(api, ensure_ascii=False)
    assert CLAVE not in json.dumps(meta, ensure_ascii=False)


def test_health_no_dispara_llamada(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)
    _sin_red(monkeypatch)
    body = srv.api_health()
    bloque = body["odds"]["odds_api"]
    assert bloque["key_presente"] is True
    assert bloque["fuente"] == "ODDS_API_KEY"
    assert bloque["ok"] is None
    assert bloque["timestamp"] is None
    assert CLAVE not in json.dumps(body, ensure_ascii=False)


def test_la_cadena_cachea_el_llamado_y_health_no_repite(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)
    urls: list[str] = []

    def fake_get(url, params=None, timeout=None):
        urls.append(str(url))
        return _Resp(200, body=[], restantes="19")

    monkeypatch.setattr(lb.requests, "get", fake_get)
    monkeypatch.setattr("lineas_espn.obtener_lineas_espn", lambda: _espn_ok())
    _mapas, meta = lb.obtener_mapas_por_casa({"lineas": {"bookmakers": "draftkings"}})
    assert meta["requests_restantes"] == "19"
    assert urls == [lb.ODDS_URL]
    api = srv.api_health()["odds"]["odds_api"]
    srv.api_odds_status()
    assert urls == [lb.ODDS_URL]
    assert api["ok"] is True
    assert api["http_status"] == 200
    assert api["requests_restantes"] == "19"
    assert api["timestamp"]
    assert CLAVE not in json.dumps(api, ensure_ascii=False)


def test_fuente_config_archivo_y_entorno(aislado, monkeypatch, tmp_path):
    cfg = {"lineas": {"api_key": CLAVE}}
    assert lb.cargar_api_key(cfg) == CLAVE
    assert lb.origen_api_key(cfg) == "config"
    estado = lb.estado_odds_api(cfg)
    assert estado["fuente"] == "config"
    assert estado["key_presente"] is True
    assert CLAVE not in json.dumps(estado, ensure_ascii=False)

    cfg_vacio = {"lineas": {}}
    assert lb.cargar_api_key(cfg_vacio) is None
    archivo = tmp_path / "odds_api_key.txt"
    archivo.write_text(CLAVE + "\n", encoding="utf-8")
    assert lb.origen_api_key(cfg_vacio) == "archivo"
    assert lb.cargar_api_key(cfg_vacio) == CLAVE

    monkeypatch.setenv("ODDS_API_KEY", CLAVE + "ENV")
    assert lb.origen_api_key(cfg) == "ODDS_API_KEY"
    assert lb.cargar_api_key(cfg) == CLAVE + "ENV"


def test_validacion_barata_no_se_repite(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)
    n = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        n["n"] += 1
        assert url == lb.SPORTS_URL
        return _Resp(200, body=[], restantes="7")

    monkeypatch.setattr(lb.requests, "get", fake_get)
    lb.validar_clave_odds_api({}, forzar=True)
    lb.validar_clave_odds_api({}, forzar=False)
    assert n["n"] == 1


def test_proveedor_legacy_reporta_la_clave_y_la_oculta(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)
    monkeypatch.setattr(
        srv,
        "cargar_config",
        lambda: {
            "modo_solo_modelo": False,
            "lineas": {
                "proveedor": "betmgm",
                "bookmakers": "draftkings",
                "fallback_internet": False,
            },
            "estrategia": {
                "requiere_betmgm": True,
                "min_edge_pct": 6,
                "fallback_solo_modelo": False,
            },
        },
    )

    def fake_get(url, params=None, timeout=None):
        raise requests.RequestException(
            f"timeout {url}?apiKey={CLAVE}"
        )

    monkeypatch.setattr(lb.requests, "get", fake_get)
    body = srv.api_odds_status()
    assert body["key_presente"] is True
    assert body["fuente"] == "ODDS_API_KEY"
    assert body["ok"] is False
    texto = json.dumps(body, ensure_ascii=False)
    assert CLAVE not in texto


def test_clave_desactivada_401(aislado, monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", CLAVE)

    def fake_get(url, params=None, timeout=None):
        return _Resp(401, body={"error_code": "DEACTIVATED_KEY", "message": "deactivated"}, restantes="1")

    monkeypatch.setattr(lb.requests, "get", fake_get)
    lb.validar_clave_odds_api({}, forzar=True)
    api = lb.estado_odds_api({})
    assert api["http_status"] == 401
    assert api["ok"] is False
    assert "desactivada" in api["mensaje"]
    assert CLAVE not in json.dumps(api, ensure_ascii=False)
