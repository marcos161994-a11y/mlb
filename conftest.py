"""Aísla la suite: ningún test escribe la memoria, los modelos ni las cachés del repo."""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent
_SALTAR = {".git", ".venv", ".pytest_cache", "__pycache__", ".devdata"}
_VIGILADOS = (
    "diagrama/bitacora.json",
    "diagrama/reporte.json",
    "config_experimento.json",
)


def _huella() -> tuple:
    """Firma de la raíz y de archivos versionados que la app reescribe."""
    raiz = []
    for path in _REPO.iterdir():
        if path.name in _SALTAR or path.name.startswith("."):
            continue
        if path.is_file():
            st = path.stat()
            raiz.append((path.name, st.st_mtime_ns, st.st_size))
        else:
            raiz.append((path.name + "/", 0, 0))
    extra = []
    for rel in _VIGILADOS:
        path = _REPO / rel
        if path.exists():
            st = path.stat()
            extra.append((rel, st.st_mtime_ns, st.st_size))
        else:
            extra.append((rel, None, None))
    return tuple(sorted(raiz)), tuple(extra)


@pytest.fixture(autouse=True)
def _datos_aislados(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MEMORIA_SQLITE_PATH", raising=False)
    monkeypatch.setenv("CLIMA_CACHE_PATH", str(data / "clima_cache.json"))
    monkeypatch.setenv("BULLPEN_CACHE_PATH", str(data / "bullpen_cache.json"))

    import servidor_mlb as srv

    monkeypatch.setattr(srv, "DATA_DIR", data)
    monkeypatch.setattr(srv, "MEMORIA_PATH", data / "memoria_auditoria.json")
    monkeypatch.setattr(srv, "MEMORIA_BACKUP_PATH", data / "memoria_auditoria_backup.json")
    monkeypatch.setattr(srv, "_JUEGOS_PANEL_CACHE_PATH", data / "juegos_panel_cache.json")
    srv._invalidar_cache_memoria()
    srv._persistencia_cache["ts"] = 0.0
    srv._persistencia_cache["info"] = None

    import ml_predictor as ml

    # DATA_DIR y BASE_DIR siguen iguales (el repo): así el entreno no copia
    # los .pkl de vuelta a la raíz. Las rutas de escritura sí van a tmp.
    monkeypatch.setattr(ml, "_modelo_path", lambda: data / "modelo_rf_mlb.pkl")
    monkeypatch.setattr(ml, "_scaler_path", lambda: data / "scaler_rf_mlb.pkl")
    monkeypatch.setattr(ml, "_modelo_xgb_path", lambda: data / "modelo_xgb_mlb.pkl")

    import calibracion

    monkeypatch.setattr(calibracion, "DATA_DIR", data)
    monkeypatch.setattr(calibracion, "BASE_DIR", data)

    import clima
    import bullpen
    import mente_skills

    monkeypatch.setattr(clima, "CACHE_PATH", data / "clima_cache.json")
    monkeypatch.setattr(bullpen, "CACHE_PATH", data / "bullpen_cache.json")
    monkeypatch.setattr(mente_skills, "REPORTE_PATH", data / "reporte.json")

    antes = _huella()
    yield
    despues = _huella()
    if despues != antes:
        raise AssertionError(
            "El test escribió en el repo (memoria, modelos o cachés). "
            f"Antes: {antes[0]!r} / {antes[1]!r}. Después: {despues[0]!r} / {despues[1]!r}."
        )
