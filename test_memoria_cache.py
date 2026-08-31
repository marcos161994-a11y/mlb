"""Tests para cache de memoria y optimizaciones anti-OOM."""

import json
import time

import servidor_mlb as srv


def test_cargar_memoria_usa_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "memoria_auditoria.json")
    monkeypatch.setattr(srv, "MEMORIA_BACKUP_PATH", tmp_path / "memoria_auditoria_backup.json")
    srv._invalidar_cache_memoria()

    mem = {"capital": 100.0, "dias": [{"dia": 1, "fecha": "2026-08-01", "predicciones": []}]}
    srv.MEMORIA_PATH.write_text(json.dumps(mem), encoding="utf-8")

    a = srv.cargar_memoria()
    b = srv.cargar_memoria()
    assert a is b

    mem["capital"] = 200.0
    srv.MEMORIA_PATH.write_text(json.dumps(mem), encoding="utf-8")
    c = srv.cargar_memoria()
    assert c is not b
    assert c["capital"] == 200.0


def test_guardar_memoria_actualiza_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "memoria_auditoria.json")
    monkeypatch.setattr(srv, "MEMORIA_BACKUP_PATH", tmp_path / "memoria_auditoria_backup.json")
    srv._invalidar_cache_memoria()

    mem = {
        "capital": 50.0,
        "dias": [],
        "dia_actual": 1,
        "capital_inicial": 50.0,
        "stake_por_juego": 3.0,
    }
    srv.guardar_memoria(mem)
    cached = srv.cargar_memoria()
    assert cached is mem or cached.get("capital") == 50.0

    mem["capital"] = 75.0
    srv.guardar_memoria(mem)
    assert srv.cargar_memoria()["capital"] == 75.0


def test_memoria_sin_secretos_es_copia_superficial():
    mem = {"capital": 1.0, "telegram": {"bot_token": "1234567890:ABCDEF", "chat_id": "1"}}
    out = srv._memoria_sin_secretos(mem)
    assert out is not mem
    assert "…" in out["telegram"]["bot_token"]
    assert mem["telegram"]["bot_token"] == "1234567890:ABCDEF"


def test_wipe_check_throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "DATA_DIR", tmp_path)
    monkeypatch.setattr(srv, "MEMORIA_PATH", tmp_path / "memoria_auditoria.json")
    monkeypatch.setattr(srv, "MEMORIA_BACKUP_PATH", tmp_path / "memoria_auditoria_backup.json")
    monkeypatch.setenv("RENDER", "true")
    srv._invalidar_cache_memoria()
    srv._wipe_check_ts = time.monotonic()

    assert srv._intentar_recuperar_wipe() is False
    assert srv._intentar_recuperar_wipe(force=True) in (True, False)


def test_cron_externo_habilitado(monkeypatch):
    monkeypatch.delenv("CRON_SECRET", raising=False)
    assert srv._cron_externo_habilitado() is False
    monkeypatch.setenv("CRON_SECRET", "test-secret")
    assert srv._cron_externo_habilitado() is True
