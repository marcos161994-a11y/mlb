"""CRON_SECRET fail-closed en mutaciones, export/import y CORS."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import servidor_mlb as srv


@asynccontextmanager
async def _sin_motor(_app):
    """El TestClient no debe arrancar el motor (liquidación / bloqueos reales)."""
    yield

RUTAS_PROTEGIDAS = [
    ("POST", "/api/reiniciar"),
    ("POST", "/api/bloquear-hoy"),
    ("POST", "/api/liquidar"),
    ("POST", "/api/avanzar-dia"),
    ("POST", "/api/procesar-experiencias"),
    ("GET", "/api/procesar-experiencias"),
    ("GET", "/api/exportar-memoria"),
    ("POST", "/api/subir-memoria"),
    ("POST", "/api/importar-aprendizaje"),
    ("POST", "/api/importar-aprendizaje-repo"),
    ("GET", "/api/importar-aprendizaje-repo"),
    ("POST", "/api/restaurar-backup"),
    ("GET", "/api/mente-errores/ciclo"),
    ("POST", "/api/mente-errores/ciclo"),
    ("GET", "/api/auto-bloqueo-externo"),
    ("POST", "/api/auto-bloqueo-externo"),
]

RUTAS_PUBLICAS = [
    ("GET", "/"),
    ("GET", "/api/health"),
    ("GET", "/api/state"),
    ("GET", "/api/panel-boot"),
    ("GET", "/api/resultados"),
    ("POST", "/api/mente-errores/cliente"),
]


def _rutas_con_dependencia() -> set[tuple[str, str]]:
    encontradas: set[tuple[str, str]] = set()
    for route in srv.app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dep in dependant.dependencies:
            if getattr(dep, "call", None) is srv.exigir_cron_secreto:
                for method in methods:
                    encontradas.add((method, path))
    return encontradas


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(srv.app.router, "lifespan_context", _sin_motor)
    with TestClient(srv.app) as http:
        yield http


def _pedir(client: TestClient, method: str, path: str, **kwargs):
    if method in ("POST", "PUT") and "json" not in kwargs and "content" not in kwargs:
        kwargs["json"] = {}
    return client.request(method, path, **kwargs)


def test_secreto_unset_rechaza(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        srv._verificar_cron_secreto(None)
    assert exc.value.status_code == 503
    monkeypatch.setenv("CRON_SECRET", "   ")
    with pytest.raises(HTTPException) as exc_blank:
        srv._verificar_cron_secreto("algo")
    assert exc_blank.value.status_code == 503


def test_secreto_incorrecto_o_vacio(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correcto")
    with pytest.raises(HTTPException) as vacio:
        srv._verificar_cron_secreto(None)
    assert vacio.value.status_code == 403
    with pytest.raises(HTTPException) as malo:
        srv._verificar_cron_secreto("no-es")
    assert malo.value.status_code == 403
    srv._verificar_cron_secreto("correcto")


def test_header_bearer_y_query():
    assert srv._secreto_recibido("  query  ", "header", "Bearer x") == "query"
    assert srv._secreto_recibido(None, "  header ", None) == "header"
    assert srv._secreto_recibido("", None, "Bearer tok") == "tok"
    assert srv._secreto_recibido(None, None, "Basic tok") is None
    assert srv._cron_autorizado(x_cron_secret="no") is False


def test_cron_autorizado_solo_si_coincide(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert srv._cron_autorizado(x_cron_secret="tok") is False
    monkeypatch.setenv("CRON_SECRET", "tok")
    assert srv._cron_autorizado() is False
    assert srv._cron_autorizado(secret="otro") is False
    assert srv._cron_autorizado(x_cron_secret="tok") is True
    assert srv._cron_autorizado(authorization="Bearer tok") is True


def test_rutas_mutantes_declaran_la_dependencia():
    found = _rutas_con_dependencia()
    faltan = [item for item in RUTAS_PROTEGIDAS if item not in found]
    assert faltan == []
    for item in RUTAS_PUBLICAS:
        assert item not in found


@pytest.mark.parametrize("method,path", RUTAS_PROTEGIDAS)
def test_mutacion_sin_env_es_503(client, method, path):
    response = _pedir(client, method, path)
    assert response.status_code == 503
    assert "CRON_SECRET" in response.json()["detail"]


@pytest.mark.parametrize("method,path", RUTAS_PROTEGIDAS)
def test_mutacion_sin_secreto_es_403(client, monkeypatch, method, path):
    monkeypatch.setenv("CRON_SECRET", "tok-test")
    response = _pedir(client, method, path)
    assert response.status_code == 403
    assert response.json()["detail"] == "Cron secret inválido"


def test_query_header_y_bearer_abren_avanzar_dia(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "tok-test")
    monkeypatch.setattr(
        srv, "sincronizar_experimento_a_hoy", lambda memoria=None: {"dia_actual": 4}
    )
    monkeypatch.setattr(srv, "programar_bloqueos_por_juego", lambda: None)
    por_query = client.post("/api/avanzar-dia", params={"secret": "tok-test"})
    assert por_query.status_code == 200
    assert por_query.json()["nuevo_dia"] == 4
    por_header = client.post("/api/avanzar-dia", headers={"X-Cron-Secret": "tok-test"})
    assert por_header.status_code == 200
    por_bearer = client.post(
        "/api/avanzar-dia", headers={"Authorization": "Bearer tok-test"}
    )
    assert por_bearer.status_code == 200
    malo = client.post("/api/avanzar-dia", headers={"X-Cron-Secret": "otro"})
    assert malo.status_code == 403


def test_state_publico_y_liquidar_protegido(client, monkeypatch):
    vistos: list[bool] = []

    def fake(*, liquidar: bool = False, ligero: bool = False):
        vistos.append(bool(liquidar))
        return {"memoria": {"capital": 1}, "liquidar": bool(liquidar)}

    monkeypatch.setattr(srv, "construir_estado_completo", fake)
    publico = client.get("/api/state")
    assert publico.status_code == 200
    assert publico.json()["liquidar"] is False

    bloqueado = client.get("/api/state", params={"liquidar": "1"})
    assert bloqueado.status_code == 503
    assert vistos == [False]

    monkeypatch.setenv("CRON_SECRET", "tok-test")
    sin_clave = client.get("/api/state", params={"liquidar": "1"})
    assert sin_clave.status_code == 403
    assert vistos == [False]

    con_clave = client.get(
        "/api/state",
        params={"liquidar": "1"},
        headers={"X-Cron-Secret": "tok-test"},
    )
    assert con_clave.status_code == 200
    assert con_clave.json()["liquidar"] is True
    assert vistos == [False, True]


def test_reiniciar_no_borra_sin_secreto(client, monkeypatch, tmp_path):
    monkeypatch.setenv("CRON_SECRET", "tok-test")
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    guardados = {"n": 0}

    def guardar(memoria, permitir_wipe=False):
        guardados["n"] += 1
        assert permitir_wipe is True

    monkeypatch.setattr(srv, "guardar_memoria", guardar)
    monkeypatch.setattr(srv, "cargar_memoria", lambda *a, **k: {"capital": 50, "dias": []})
    monkeypatch.setattr(
        srv,
        "cargar_config",
        lambda: {"capital_inicial": 100, "dias_totales": 10, "stake_por_juego": 3},
    )
    monkeypatch.setattr(srv, "_escribir_snapshot", lambda *a, **k: None)

    denegado = client.post("/api/reiniciar", params={"confirm": "BORRAR"})
    assert denegado.status_code == 403
    assert guardados["n"] == 0

    ok = client.post(
        "/api/reiniciar",
        params={"confirm": "BORRAR"},
        headers={"X-Cron-Secret": "tok-test"},
    )
    assert ok.status_code == 200
    assert guardados["n"] == 1
    assert ok.json()["ok"] is True


def test_exportar_exige_secreto_y_luego_entrega(client, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "tok-test")
    monkeypatch.setattr(
        srv, "cargar_memoria", lambda *a, **k: {"capital": 9.5, "dias": [{"fecha": "2026-09-01"}]}
    )
    denegado = client.get("/api/exportar-memoria")
    assert denegado.status_code == 403
    ok = client.get("/api/exportar-memoria", headers={"X-Cron-Secret": "tok-test"})
    assert ok.status_code == 200
    assert ok.json()["capital"] == 9.5


def test_ciclo_mente_no_se_salta_sin_secreto(client, monkeypatch):
    llamados = {"n": 0}

    def ciclo(*args, **kwargs):
        llamados["n"] += 1
        return {"ok": True, "hallazgos": []}

    monkeypatch.setattr(srv, "ejecutar_ciclo_mente_errores", ciclo)
    monkeypatch.setattr(srv, "cargar_config", lambda: {})
    monkeypatch.setattr(srv, "cargar_memoria", lambda *a, **k: {"dias": []})

    sin_env = client.get("/api/mente-errores/ciclo")
    assert sin_env.status_code == 503
    assert llamados["n"] == 0

    monkeypatch.setenv("CRON_SECRET", "tok-test")
    sin_clave = client.post("/api/mente-errores/ciclo")
    assert sin_clave.status_code == 403
    assert llamados["n"] == 0

    con_clave = client.get(
        "/api/mente-errores/ciclo", headers={"Authorization": "Bearer tok-test"}
    )
    assert con_clave.status_code == 200
    assert llamados["n"] == 1


def test_panel_boot_no_liquida_sin_secreto(client, monkeypatch):
    calls = {"n": 0}

    def liquidar(memoria, dia):
        calls["n"] += 1
        return 1

    mem = {
        "capital": 10.0,
        "capital_inicial": 100.0,
        "dia_actual": 1,
        "dias_totales": 10,
        "stake_por_juego": 3.0,
        "experimento_activo": True,
        "dias": [
            {
                "dia": 1,
                "fecha": "2026-09-29",
                "predicciones": [{"estado": "pendiente", "game_id": "g1"}],
                "apuestas": [],
            }
        ],
    }
    monkeypatch.setattr(srv, "liquidar_dia", liquidar)
    monkeypatch.setattr(srv, "_intentar_recuperar_wipe", lambda *a, **k: False)
    monkeypatch.setattr(srv, "_en_render", lambda: True)
    monkeypatch.setattr(srv, "obtener_juegos_fecha", lambda *a, **k: [])
    monkeypatch.setattr(srv, "vigilancia_t60", lambda *a, **k: {})
    monkeypatch.setattr(srv, "cargar_memoria", lambda *a, **k: mem)
    monkeypatch.setattr(srv, "fecha_str", lambda *a, **k: "2026-09-29")
    monkeypatch.setattr(srv, "_leer_juegos_panel_disk", lambda *a, **k: None)

    publico = client.get("/api/panel-boot")
    assert publico.status_code == 200
    assert "memoria" in publico.json()
    assert calls["n"] == 0

    monkeypatch.setenv("CRON_SECRET", "tok-test")
    con_clave = client.get("/api/panel-boot", headers={"X-Cron-Secret": "tok-test"})
    assert con_clave.status_code == 200
    assert calls["n"] == 1


def test_health_y_panel_siguen_publicos(client):
    panel = client.get("/")
    assert panel.status_code == 200
    assert "Quantum" in panel.text
    salud = client.get("/api/panel-health")
    assert salud.status_code == 200
    assert salud.json()["ok"] is True


def test_cors_allowlist_sin_credenciales(client):
    evil = client.get("/api/panel-health", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 200
    assert evil.headers.get("access-control-allow-origin") is None
    assert evil.headers.get("access-control-allow-credentials") is None

    propio = client.get(
        "/api/panel-health",
        headers={"Origin": "https://mlb-1-en7i.onrender.com"},
    )
    assert propio.headers.get("access-control-allow-origin") == "https://mlb-1-en7i.onrender.com"
    assert propio.headers.get("access-control-allow-credentials") is None

    local = client.get("/api/panel-health", headers={"Origin": "http://127.0.0.1:8000"})
    assert local.headers.get("access-control-allow-origin") == "http://127.0.0.1:8000"

    archivo = client.get("/", headers={"Origin": "null"})
    assert archivo.headers.get("access-control-allow-origin") == "null"
    assert archivo.headers.get("access-control-allow-credentials") is None

    preflight = client.options(
        "/api/liquidar",
        headers={
            "Origin": "http://localhost:8000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-cron-secret, content-type",
        },
    )
    assert preflight.status_code == 200
    allow = preflight.headers.get("access-control-allow-headers", "").lower()
    assert "x-cron-secret" in allow
    assert preflight.headers.get("access-control-allow-credentials") is None

    preflight_evil = client.options(
        "/api/liquidar",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-cron-secret",
        },
    )
    assert preflight_evil.status_code == 400


def test_panel_pide_clave_en_botones_mutantes():
    html = Path("QuantumMLB.html").read_text(encoding="utf-8")
    assert "qmlb_cron_secret" in html
    assert "X-Cron-Secret" in html
    assert "function fetchProtegido" in html
    assert "function configurarClave" in html
    assert "headers: headersCron()" in html
    assert "API + '/api/state'" in html
    for name in ("bloquearHoy", "liquidar", "procesarExperiencias", "reiniciar"):
        start = html.find("async function " + name)
        assert start != -1
        nxt = html.find("\nasync function ", start + 10)
        body = html[start:nxt]
        assert "fetchProtegido" in body
        assert "API + '/api/" not in body or "fetchProtegido" in body


def test_workflows_envian_secreto_por_header():
    root = Path(".github/workflows")
    for name in (
        "cloud-cron.yml",
        "backup-memoria.yml",
        "restore-memoria.yml",
        "importar-aprendizaje.yml",
    ):
        text = (root / name).read_text(encoding="utf-8")
        assert "secrets.CRON_SECRET" in text
        assert "X-Cron-Secret" in text
    backup = (root / "backup-memoria.yml").read_text(encoding="utf-8")
    assert "/api/state" not in backup
    assert "exit 1" in backup
    cron = (root / "cloud-cron.yml").read_text(encoding="utf-8")
    assert "::error::Falta CRON_SECRET" in cron
    assert "api/health" in cron
