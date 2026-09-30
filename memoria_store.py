"""Persistencia de la memoria de auditoría.

La app sigue hablando con un documento JSON (el mismo dict que devuelven
`cargar_memoria` / `guardar_memoria`). Aquí solo cambia el sitio donde vive:

- `DATABASE_URL` → Postgres (Neon, Supabase o Render). Durable.
- Si no hay URL → SQLite en `DATA_DIR/memoria.sqlite`. Sirve en local y en tests.
  En Render el disco del plan free se borra al reiniciar: sin `DATABASE_URL`
  ese SQLite no es durable y `describir_persistencia` lo marca así.

No hay columnas por campo. Un PR que añada stake, cuotas o datos de panel
puede seguir guardando claves nuevas dentro del documento.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse


class MemoriaStoreError(RuntimeError):
    pass


_LOCK = threading.RLock()

_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS memoria_documento (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    documento TEXT NOT NULL,
    revision INTEGER NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memoria_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    documento TEXT NOT NULL,
    n_fechas INTEGER NOT NULL,
    creado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memoria_semilla (
    archivo TEXT PRIMARY KEY,
    tamano INTEGER NOT NULL,
    mtime_ns BIGINT NOT NULL
);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS memoria_documento (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    documento TEXT NOT NULL,
    revision INTEGER NOT NULL,
    actualizado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memoria_snapshots (
    id BIGSERIAL PRIMARY KEY,
    documento TEXT NOT NULL,
    n_fechas INTEGER NOT NULL,
    creado_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memoria_semilla (
    archivo TEXT PRIMARY KEY,
    tamano INTEGER NOT NULL,
    mtime_ns BIGINT NOT NULL
);
"""


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip()


def normalizar_database_url(url: str) -> str:
    """Acepta postgres:// y añade sslmode=require en hosts externos.

    El host interno de Render (dpg-… sin punto) no usa SSL. Neon, Supabase
    y la URL externa de Render sí.
    """
    limpia = url.strip()
    if limpia.startswith("postgres://"):
        limpia = "postgresql://" + limpia[len("postgres://") :]
    host = (urlparse(limpia).hostname or "").lower()
    local = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".internal")
    if host and not local and "." in host and "sslmode=" not in limpia:
        limpia += ("&" if "?" in limpia else "?") + "sslmode=require"
    return limpia


def destino_publico(url: str) -> str:
    """Host y base, sin usuario ni contraseña."""
    try:
        partes = urlparse(normalizar_database_url(url))
    except Exception:
        return "postgres"
    host = partes.hostname or "postgres"
    puerto = f":{partes.port}" if partes.port else ""
    base = partes.path or ""
    return f"{host}{puerto}{base}"


def ruta_sqlite(data_dir: Path) -> Path:
    override = os.environ.get("MEMORIA_SQLITE_PATH", "").strip()
    if override:
        return Path(override)
    return Path(data_dir) / "memoria.sqlite"


def describir_persistencia(data_dir: Path) -> dict[str, Any]:
    """No lanza: /api/health tiene que seguir respondiendo."""
    url = database_url()
    en_render = bool(os.environ.get("RENDER"))
    if url:
        info: dict[str, Any] = {
            "backend": "postgres",
            "durable": True,
            "destino": destino_publico(url),
            "aviso": None,
        }
    elif en_render:
        info = {
            "backend": "sqlite",
            "durable": False,
            "destino": str(ruta_sqlite(data_dir)),
            "aviso": (
                "RENDER sin DATABASE_URL: el disco del plan free es efímero. "
                "SQLite se pierde al reiniciar. Configura Postgres."
            ),
        }
    else:
        info = {
            "backend": "sqlite",
            "durable": True,
            "destino": str(ruta_sqlite(data_dir)),
            "aviso": None,
        }
    try:
        store = abrir(data_dir)
        with store._conexion() as conn:
            store._ejecutar(conn, "SELECT 1")
        info["conectado"] = True
    except Exception as exc:
        info["conectado"] = False
        info["durable"] = False
        info["error"] = str(exc)[:200]
    return info


def _dumps(documento: dict) -> str:
    return json.dumps(documento, ensure_ascii=False, separators=(",", ":"))


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemoriaStore:
    def __init__(self, *, backend: str, destino: str) -> None:
        self.backend = backend
        self.destino = destino

    def cargar(self) -> dict | None:
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                fila = self._ejecutar(
                    conn,
                    "SELECT documento FROM memoria_documento WHERE id = 1",
                ).fetchone()
        if not fila:
            return None
        data = json.loads(fila[0])
        return data if isinstance(data, dict) else None

    def revision(self) -> int | None:
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                fila = self._ejecutar(
                    conn,
                    "SELECT revision FROM memoria_documento WHERE id = 1",
                ).fetchone()
        if not fila:
            return None
        return int(fila[0])

    def guardar(self, documento: dict) -> int:
        if not isinstance(documento, dict):
            raise MemoriaStoreError("La memoria tiene que ser un objeto JSON")
        payload = _dumps(documento)
        ahora = _ahora()
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                self._ejecutar(
                    conn,
                    """
                    INSERT INTO memoria_documento (id, documento, revision, actualizado_en)
                    VALUES (1, %s, 1, %s)
                    ON CONFLICT(id) DO UPDATE SET
                        documento = excluded.documento,
                        revision = memoria_documento.revision + 1,
                        actualizado_en = excluded.actualizado_en
                    """,
                    (payload, ahora),
                )
                fila = self._ejecutar(
                    conn,
                    "SELECT revision FROM memoria_documento WHERE id = 1",
                ).fetchone()
                conn.commit()
        return int(fila[0]) if fila else 1

    def guardar_snapshot(self, documento: dict, *, n_fechas: int, keep: int) -> None:
        if not isinstance(documento, dict) or n_fechas <= 0 or keep <= 0:
            return
        payload = _dumps(documento)
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                self._ejecutar(
                    conn,
                    """
                    INSERT INTO memoria_snapshots (documento, n_fechas, creado_en)
                    VALUES (%s, %s, %s)
                    """,
                    (payload, int(n_fechas), _ahora()),
                )
                self._ejecutar(
                    conn,
                    """
                    DELETE FROM memoria_snapshots
                    WHERE id NOT IN (
                        SELECT id FROM memoria_snapshots ORDER BY id DESC LIMIT %s
                    )
                    """,
                    (int(keep),),
                )
                conn.commit()

    def ultimo_snapshot(self) -> dict | None:
        return self._snapshot(
            "SELECT documento FROM memoria_snapshots ORDER BY id DESC LIMIT 1"
        )

    def mejor_snapshot(self) -> dict | None:
        return self._snapshot(
            """
            SELECT documento FROM memoria_snapshots
            ORDER BY n_fechas DESC, id DESC
            LIMIT 1
            """
        )

    def contar_snapshots(self) -> int:
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                fila = self._ejecutar(
                    conn, "SELECT COUNT(*) FROM memoria_snapshots"
                ).fetchone()
        return int(fila[0]) if fila else 0

    def semilla_ya_aplicada(self, path: Path) -> bool:
        """True si este archivo ya se importó y no cambió de tamaño ni de fecha."""
        try:
            st = path.stat()
        except OSError:
            return False
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                fila = self._ejecutar(
                    conn,
                    "SELECT tamano, mtime_ns FROM memoria_semilla WHERE archivo = %s",
                    (str(path.resolve()),),
                ).fetchone()
        if not fila:
            return False
        return int(fila[0]) == int(st.st_size) and int(fila[1]) == int(st.st_mtime_ns)

    def marcar_semilla(self, path: Path) -> None:
        st = path.stat()
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                self._ejecutar(
                    conn,
                    """
                    INSERT INTO memoria_semilla (archivo, tamano, mtime_ns)
                    VALUES (%s, %s, %s)
                    ON CONFLICT(archivo) DO UPDATE SET
                        tamano = excluded.tamano,
                        mtime_ns = excluded.mtime_ns
                    """,
                    (str(path.resolve()), int(st.st_size), int(st.st_mtime_ns)),
                )
                conn.commit()

    def info_backup(self) -> dict[str, Any]:
        """Misma forma que el antiguo backup en disco, leída del último snapshot."""
        info: dict[str, Any] = {
            "backup_exists": False,
            "backup_mtime": None,
            "backup_fechas": 0,
        }
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                fila = self._ejecutar(
                    conn,
                    """
                    SELECT documento, n_fechas, creado_en
                    FROM memoria_snapshots
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                ).fetchone()
        if not fila:
            return info
        info["backup_exists"] = True
        info["backup_fechas"] = int(fila[1] or 0)
        try:
            info["backup_mtime"] = datetime.strptime(
                str(fila[2]), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            info["backup_mtime"] = None
        try:
            from memoria_fusion import contar_historial, fechas_con_historial

            data = json.loads(fila[0])
            if isinstance(data, dict):
                info["backup_fechas"] = len(fechas_con_historial(data))
                apuestas, preds = contar_historial(data)
                info["backup_apuestas"] = apuestas
                info["backup_preds"] = preds
        except Exception:
            pass
        return info

    def _snapshot(self, sql: str) -> dict | None:
        with _LOCK:
            with self._conexion() as conn:
                self._asegurar(conn)
                fila = self._ejecutar(conn, sql).fetchone()
        if not fila:
            return None
        data = json.loads(fila[0])
        return data if isinstance(data, dict) else None

    def _sql(self, sql: str) -> str:
        if self.backend == "sqlite":
            return sql.replace("%s", "?")
        return sql

    def _ejecutar(self, conn: Any, sql: str, params: tuple = ()):
        return conn.execute(self._sql(sql), params)

    def _asegurar(self, conn: Any) -> None:
        if self.backend == "sqlite":
            conn.executescript(_SCHEMA_SQLITE)
            return
        conn.execute(_SCHEMA_POSTGRES)

    def _conectar(self) -> Any:
        raise NotImplementedError

    @contextmanager
    def _conexion(self) -> Iterator[Any]:
        conn = self._conectar()
        try:
            yield conn
        finally:
            conn.close()


class SqliteStore(MemoriaStore):
    def __init__(self, path: Path) -> None:
        super().__init__(backend="sqlite", destino=str(path))
        self.path = Path(path)

    def _conectar(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


class PostgresStore(MemoriaStore):
    def __init__(self, url: str) -> None:
        self.url = normalizar_database_url(url)
        super().__init__(backend="postgres", destino=destino_publico(url))

    def _conectar(self) -> Any:
        try:
            import psycopg
        except ImportError as exc:
            raise MemoriaStoreError(
                "Falta psycopg. Instala requirements.txt (psycopg[binary])."
            ) from exc
        return psycopg.connect(self.url, connect_timeout=15)

    def _asegurar(self, conn: Any) -> None:
        # Varias sentencias: psycopg no tiene executescript.
        for sentencia in _SCHEMA_POSTGRES.split(";"):
            sql = sentencia.strip()
            if sql:
                conn.execute(sql)


def abrir(data_dir: Path) -> MemoriaStore:
    url = database_url()
    if url:
        return PostgresStore(url)
    return SqliteStore(ruta_sqlite(data_dir))
