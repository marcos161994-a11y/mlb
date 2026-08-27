"""Cache de juegos del panel (disco) — no requiere MLB en red."""
from __future__ import annotations

import json
import time
from pathlib import Path

import servidor_mlb as srv


def test_guardar_y_leer_juegos_panel_disk(tmp_path, monkeypatch):
    cache = tmp_path / "juegos_panel_cache.json"
    monkeypatch.setattr(srv, "_JUEGOS_PANEL_CACHE_PATH", cache)
    monkeypatch.setattr(srv, "_JUEGOS_PANEL_DISK_MAX_AGE_SEC", 60)

    juegos = [
        {
            "id": "1",
            "home": "Yankees",
            "visitante": "Red Sox",
            "estado": "PROGRAMADO",
            "pick": "Yankees ML",
            "probPick": 60.0,
            "apostable": False,
        }
    ]
    srv._guardar_juegos_panel_disk("2026-08-27", juegos)
    assert cache.exists()

    got = srv._leer_juegos_panel_disk("2026-08-27")
    assert got is not None
    assert got["ok"] is True
    assert got["fecha"] == "2026-08-27"
    assert got["n"] == 1
    assert got["games"][0]["home"] == "Yankees"
    assert got["cache"] == "disk"

    # Fecha distinta → None
    assert srv._leer_juegos_panel_disk("2026-08-01") is None


def test_leer_juegos_panel_disk_expira(tmp_path, monkeypatch):
    cache = tmp_path / "juegos_panel_cache.json"
    monkeypatch.setattr(srv, "_JUEGOS_PANEL_CACHE_PATH", cache)
    monkeypatch.setattr(srv, "_JUEGOS_PANEL_DISK_MAX_AGE_SEC", 1)

    payload = {
        "fecha": "2026-08-27",
        "ts": time.time() - 10,
        "games": [{"id": "9", "home": "A"}],
        "n": 1,
    }
    cache.write_text(json.dumps(payload), encoding="utf-8")
    assert srv._leer_juegos_panel_disk("2026-08-27") is None
    # max_age override permite stale
    stale = srv._leer_juegos_panel_disk("2026-08-27", max_age_sec=3600)
    assert stale is not None
    assert stale["games"][0]["id"] == "9"


def test_leer_juegos_panel_disk_vacio(tmp_path, monkeypatch):
    cache = tmp_path / "missing.json"
    monkeypatch.setattr(srv, "_JUEGOS_PANEL_CACHE_PATH", cache)
    assert srv._leer_juegos_panel_disk("2026-08-27") is None
