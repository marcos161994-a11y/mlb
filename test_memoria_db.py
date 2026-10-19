"""Base de memoria: SQLite local, semilla JSON y el aviso de disco efímero."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import servidor_mlb as srv
from memoria_store import (
    SqliteStore,
    describir_persistencia,
    normalizar_database_url,
)
from migrar_memoria_db import (
    firma_historial,
    integrar_documento,
    main as migrar_main,
    problemas_perdida,
)


def _doc(fecha: str, game_id: str, *, estado: str = "liquidado", extra: dict | None = None) -> dict:
    memoria = {
        "capital": 92.5,
        "capital_inicial": 100,
        "dia_actual": 1,
        "stake_por_juego": 3.0,
        "campo_futuro_panel": {"odds_reales": True, "stake_fijo": 3},
        "dias": [
            {
                "fecha": fecha,
                "dia": 1,
                "predicciones": [
                    {
                        "game_id": game_id,
                        "pick": "NYY ML",
                        "odds": 1.91,
                        "estado": estado,
                        "resultado": "acierto",
                        "profit": 2.5,
                    }
                ],
                "apuestas": [
                    {
                        "game_id": game_id,
                        "estado": "ganada",
                        "profit": 2.5,
                        "stake": 3.0,
                        "odds": 1.91,
                    }
                ],
            }
        ],
        "lecciones": [{"id": f"lec-{game_id}", "patron": "edge", "game_id": game_id}],
    }
    if extra:
        memoria.update(extra)
    return memoria


def test_roundtrip_sqlite_conserva_claves_nuevas(tmp_path):
    store = SqliteStore(tmp_path / "memoria.sqlite")
    original = _doc("2026-08-01", "g1")
    rev = store.guardar(original)
    assert rev == 1
    cargado = store.cargar()
    assert cargado["campo_futuro_panel"]["stake_fijo"] == 3
    assert cargado["dias"][0]["predicciones"][0]["odds"] == 1.91
    assert cargado["dias"][0]["apuestas"][0]["stake"] == 3.0
    assert not problemas_perdida(original, cargado)
    assert firma_historial(original) == firma_historial(cargado)
    assert store.revision() == 1
    store.guardar(cargado)
    assert store.revision() == 2


def test_render_sin_database_url_no_es_durable(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("RENDER", "true")
    info = describir_persistencia(tmp_path)
    assert info["backend"] == "sqlite"
    assert info["durable"] is False
    assert info["conectado"] is True
    assert "efímero" in info["aviso"]


def test_sqlite_local_es_durable(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    info = describir_persistencia(tmp_path)
    assert info["backend"] == "sqlite"
    assert info["durable"] is True
    assert info["conectado"] is True


def test_database_url_anade_ssl_en_host_externo():
    neon = normalizar_database_url("postgres://user:secret@ep-abc.neon.tech/neondb")
    assert neon.startswith("postgresql://")
    assert "sslmode=require" in neon
    interno = normalizar_database_url("postgresql://user:secret@dpg-abc-a/mlb")
    assert "sslmode" not in interno
    ya = normalizar_database_url("postgresql://user:secret@db.supabase.co/postgres?sslmode=require")
    assert ya.count("sslmode=") == 1


def test_migrar_no_pisa_dias_de_la_base_ni_borra_el_json(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db = tmp_path / "memoria.sqlite"
    store = SqliteStore(db)
    store.guardar(_doc("2026-08-02", "vivo"))
    origen = tmp_path / "memoria_auditoria.json"
    semilla = _doc("2026-08-01", "repo")
    texto = json.dumps(semilla)
    origen.write_text(texto, encoding="utf-8")
    antes = origen.read_bytes()

    informe = integrar_documento(store, semilla)
    assert informe["ok"] is True
    assert informe["escrito"] is True
    assert origen.read_bytes() == antes
    cargado = store.cargar()
    fechas = {d["fecha"] for d in cargado["dias"]}
    assert fechas == {"2026-08-01", "2026-08-02"}
    assert not problemas_perdida(semilla, cargado)
    assert not problemas_perdida(_doc("2026-08-02", "vivo"), cargado)

    segundo = integrar_documento(store, semilla)
    assert segundo["ok"] is True
    assert segundo["escrito"] is False


def test_cli_sqlite_y_json_real_si_existe(tmp_path):
    origen = Path("memoria_auditoria.json")
    if not origen.is_file():
        pytest.skip("no está la semilla")
    antes = origen.read_bytes()
    db = tmp_path / "memoria.sqlite"
    assert migrar_main(["--json", str(origen), "--sqlite", str(db)]) == 0
    assert origen.read_bytes() == antes
    assert migrar_main(["--json", str(origen), "--sqlite", str(db)]) == 0
    original = json.loads(antes)
    cargado = SqliteStore(db).cargar()
    assert not problemas_perdida(original, cargado)
    assert set(original) <= set(cargado)
    assert firma_historial(original) == firma_historial(cargado)


def test_app_no_escribe_el_json_del_repo(tmp_path, monkeypatch):
    repo = Path("memoria_auditoria.json")
    antes = repo.read_bytes() if repo.exists() else None
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "memoria_auditoria.json")
    monkeypatch.setattr(srv, "MEMORIA_BACKUP_PATH", tmp_path / "memoria_auditoria_backup.json")
    monkeypatch.delenv("RENDER", raising=False)
    srv._invalidar_cache_memoria()
    srv.guardar_memoria(_doc("2026-09-01", "app"))
    assert not (tmp_path / "memoria_auditoria.json").exists()
    assert (tmp_path / "memoria_dashboard.js").exists()
    crudo = sqlite3.connect(tmp_path / "memoria.sqlite").execute(
        "SELECT documento FROM memoria_documento"
    ).fetchone()[0]
    assert "campo_futuro_panel" in crudo
    if antes is not None:
        assert repo.read_bytes() == antes


def test_semilla_no_reimporta_si_el_archivo_no_cambio(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MEMORIA_SQLITE_PATH", raising=False)
    origen = tmp_path / "memoria_auditoria.json"
    origen.write_text(json.dumps(_doc("2026-08-01", "g1")), encoding="utf-8")
    from migrar_memoria_db import sembrar_desde_archivos

    primero = sembrar_desde_archivos(data_dir=tmp_path, json_repo=origen)
    assert primero[0]["escrito"] is True
    segundo = sembrar_desde_archivos(data_dir=tmp_path, json_repo=origen)
    assert segundo[0]["motivo"] == "semilla ya aplicada"
    store = SqliteStore(tmp_path / "memoria.sqlite")
    assert store.cargar()["dias"][0]["fecha"] == "2026-08-01"


def test_health_informa_persistencia(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    cuerpo = srv.api_health()
    assert cuerpo["ok"] is True
    assert cuerpo["persistencia"]["backend"] == "sqlite"
    assert cuerpo["persistencia"]["conectado"] is True


def test_resolver_ipv4_pide_solo_registros_a(monkeypatch):
    import socket

    import memoria_store as store

    def fake(host, port, family, socktype, proto, flags):
        assert host == "ep-flat.neon.tech"
        assert port == 5432
        assert family == socket.AF_INET
        assert flags == 0
        return [
            (family, socktype, proto, "", ("18.189.49.143", 5432)),
            (family, socktype, proto, "", ("18.189.49.143", 5432)),
            (family, socktype, proto, "", ("3.18.239.121", 5432)),
        ]

    monkeypatch.setattr(store.socket, "getaddrinfo", fake)
    assert store.resolver_ipv4("ep-flat.neon.tech", 5432) == [
        "18.189.49.143",
        "3.18.239.121",
    ]


def test_conexion_usa_ipv4_y_conserva_el_host_para_tls():
    from psycopg.conninfo import conninfo_to_dict

    import memoria_store as store

    vistos: list[dict] = []

    def conectar(info, **kwargs):
        vistos.append({"info": info, **kwargs})
        return object()

    store.conectar_postgres(
        "postgres://user:se%20cret%40x@ep-abc-pooler.neon.tech/neondb",
        resolver=lambda host, port: ["18.189.49.143", "3.18.239.121"],
        conectar=conectar,
    )
    assert len(vistos) == 1
    params = conninfo_to_dict(vistos[0]["info"])
    assert params["host"] == "ep-abc-pooler.neon.tech"
    assert params["hostaddr"] == "18.189.49.143"
    assert params["sslmode"] == "require"
    assert params["password"] == "se cret@x"
    assert vistos[0]["prepare_threshold"] is None
    assert "2600:" not in params["hostaddr"]


def test_reintenta_si_neon_esta_despertando(monkeypatch):
    import psycopg

    import memoria_store as store

    pausas: list[float] = []
    llamadas = {"n": 0}

    def conectar(info, **kwargs):
        llamadas["n"] += 1
        if llamadas["n"] < 3:
            raise psycopg.OperationalError("the database system is starting up")
        return object()

    monkeypatch.setattr(store.time, "sleep", lambda s: pausas.append(s))
    store.conectar_postgres(
        "postgresql://user:secret@ep-abc-pooler.neon.tech/neondb?sslmode=require",
        resolver=lambda host, port: ["18.189.49.143"],
        conectar=conectar,
    )
    assert llamadas["n"] == 3
    assert pausas == [0.4, 1.2]


def test_clave_incorrecta_no_se_reintenta():
    import psycopg

    import memoria_store as store

    llamadas = {"n": 0}

    def conectar(info, **kwargs):
        llamadas["n"] += 1
        raise psycopg.OperationalError(
            'connection failed: ERROR: password authentication failed for user "u"'
        )

    with pytest.raises(psycopg.OperationalError):
        store.conectar_postgres(
            "postgresql://u:secret@ep-abc.neon.tech/neondb",
            resolver=lambda host, port: ["18.189.49.143", "3.18.239.121"],
            conectar=conectar,
            dormir=lambda s: (_ for _ in ()).throw(AssertionError("no debe esperar")),
        )
    assert llamadas["n"] == 1


def test_host_interno_no_resuelve_ipv4():
    import memoria_store as store

    def conectar(info, **kwargs):
        assert "hostaddr" not in info
        assert "sslmode" not in info
        return object()

    def resolver(host, port):
        raise AssertionError(host)

    store.conectar_postgres(
        "postgresql://user:secret@dpg-abc-a/mlb",
        resolver=resolver,
        conectar=conectar,
    )


def test_health_postgres_conectado_no_filtra_la_clave(tmp_path, monkeypatch):
    import memoria_store as store

    class Conn:
        def execute(self, *args, **kwargs):
            return self

        def close(self):
            pass

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:secret@ep-abc-pooler.neon.tech/neondb",
    )
    monkeypatch.setattr(store, "conectar_postgres", lambda url, **kwargs: Conn())
    info = describir_persistencia(tmp_path)
    assert info["backend"] == "postgres"
    assert info["conectado"] is True
    assert info["durable"] is True
    assert "secret" not in json.dumps(info)
    assert info["destino"] == "ep-abc-pooler.neon.tech/neondb"


def test_error_publico_muestra_la_ipv4_y_oculta_la_clave():
    import memoria_store as store

    exc = Exception(
        'connection is bad: connection to server at "2600:1f16::1", port 5432 failed: '
        "Network is unreachable\n"
        "\tIs the server running on that host and accepting TCP/IP connections?\n"
        "Multiple connection attempts failed. All failures were:\n"
        "- host: 'ep-x.neon.tech', port: None, hostaddr: '18.189.49.143': "
        'connection failed: ERROR: password authentication failed for user "u" '
        "password=secret\n"
        "postgresql://user:secret@ep-x.neon.tech/neondb"
    )
    texto = store.resumen_error_conexion(exc)
    assert "password authentication failed" in texto
    assert "secret" not in texto
    assert "2600:" not in texto
