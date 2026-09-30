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
