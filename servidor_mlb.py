"""
Quantum MLB — Experimento de 10 días (paper trading con resultados reales MLB).

Cada juego se congela en T-90, T-60, T-30 o T-10 (la primera ventana que alcance
el servidor) y el stake se bloquea 1 hora ANTES del inicio (hora Puerto Rico),
solo si hay valor vs el mercado. No se congela después del primer lanzamiento.
Al finalizar se liquida P/L.
"""

from __future__ import annotations

import copy
import gc
import hashlib
import hmac
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import requests
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from lineas_betmgm import (
    aplicar_lineas_a_juegos,
    cargar_api_key,
    enmascarar_api_key,
    estado_odds_api,
    origen_api_key,
    redactar_secreto,
    validar_clave_odds_api,
)
from lineas_betmgm import normalizar_nombre_equipo as norm_nombre
from memoria_fusion import (
    backup_tiene_dias_que_el_disco_perdio as _backup_tiene_dias_que_el_disco_perdio,
    contar_historial as _contar_historial,
    escribir_snapshot as _escribir_snapshot,
    fechas_con_historial as _fechas_con_historial,
    fusionar_memoria as _fusionar_memoria,
    mejor_snapshot as _mejor_snapshot,
    memoria_parece_reinicio as _memoria_parece_reinicio,
    proteger_escritura as _proteger_escritura,
    resumen_sello as _resumen_sello,
)
from modelo_mlb import (
    evaluar_juegos,
    cuota_desde_prob,
    edge_pct,
    fuente_es_mercado,
    bloqueado_favorito_inflado,
    tiene_cuota_mercado,
    apostable_con_mercado,
    apostable_para_dinero,
    es_momio_estimado,
)
from cadena_momios import (
    action_network_activo,
    apuesta_fija_dolares,
    campos_precio_congelado,
    momio_del_pick,
    profit_moneyline_americano,
)
from aprendizaje_mlb import calcular_movimiento_linea, peso_muestra_aprendizaje, bloqueado_linea_en_contra
from clv_mlb import actualizar_clv_registro, resumen_clv_memoria
from ml_predictor import auto_entrenar_ml
from ia_groq import ia_veto_disponible, modelo_groq, probar_conexion_groq, veto_apuesta
from mente_mlb import (
    mente_conclusion,
    mente_disponible,
    generar_briefing_juego,
    veredicto_bloquea_dinero,
)
from mente_errores import (
    mente_errores_disponible,
    resumen_para_panel as resumen_mente_errores_panel,
    ejecutar_ciclo as ejecutar_ciclo_mente_errores,
    registrar_error_runtime,
    registrar_error_cliente,
)
from mente_integridad import verificar_panel_html

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
_lineas_meta_cache: dict = {"ok": False, "mensaje": "Sin cargar"}
CONFIG_PATH = BASE_DIR / "config_experimento.json"
MEMORIA_PATH = DATA_DIR / "memoria_auditoria.json"
MEMORIA_BACKUP_PATH = DATA_DIR / "memoria_auditoria_backup.json"
_memoria_lock = threading.RLock()
_memoria_cache: dict | None = None
_memoria_cache_digest: str | None = None
_memoria_cache_revision: int | None = None
_memoria_cache_origen: str | None = None
_persistencia_cache: dict = {"ts": 0.0, "info": None}
_wipe_check_ts: float = 0.0
_WIPE_CHECK_INTERVAL_SEC = 300.0
_ultimo_ml_train_ts: float = 0.0
_ML_TRAIN_MIN_INTERVAL_SEC = 3600.0
_import_auto_ok_ts: float = 0.0
_IMPORT_AUTO_TTL_SEC = 6 * 3600.0

MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
scheduler = BackgroundScheduler()
_cron_externo_lock = threading.Lock()
_cron_externo_activo = False
_juegos_ui_cache: dict = {"fecha": "", "ts": 0.0, "juegos": []}
_JUEGOS_UI_TTL_SEC = 90
_JUEGOS_PANEL_CACHE_PATH = DATA_DIR / "juegos_panel_cache.json"
_JUEGOS_PANEL_DISK_MAX_AGE_SEC = 20 * 60


def _en_render() -> bool:
    return bool(os.environ.get("RENDER"))


def _construir_mente_red_panel(
    cfg: dict,
    memoria: dict,
    lecciones_meta: dict | None = None,
    mente_stats_meta: dict | None = None,
) -> dict:
    try:
        from mente_red import construir_mente_red

        bitacora_meta = None
        try:
            from mente_bitacora import resumen_bitacora

            bitacora_meta = resumen_bitacora()
        except Exception:
            bitacora_meta = None
        return construir_mente_red(
            cfg,
            memoria,
            lecciones=lecciones_meta,
            mente_stats=mente_stats_meta,
            ml_meta=(memoria or {}).get("ml_meta") if isinstance(memoria, dict) else None,
            mente_errores=_resumen_mente_errores(cfg) if "_resumen_mente_errores" in globals() else None,
            bitacora=bitacora_meta,
        )
    except Exception as e:
        return {"ok": False, "mensaje": str(e)[:120], "nodos": [], "aristas": []}


def _mc_sims_health(cfg: dict) -> int:
    try:
        from inteligencia_mlb import mc_sims_efectivos

        intel = cfg.get("inteligencia") if isinstance(cfg.get("inteligencia"), dict) else {}
        return mc_sims_efectivos(intel)
    except Exception:
        return int((cfg.get("inteligencia") or {}).get("mc_sims") or 800)


def _json_dumps_memoria(obj: Any) -> str:
    """Compacto en Render: indent=2 duplica el pico al serializar ~9 MB."""
    indent = None if _en_render() else 2
    return json.dumps(obj, indent=indent, ensure_ascii=False)


def _escribir_json_atomico(path: Path, obj: Any) -> None:
    """Escribe JSON y reemplaza: un OOM a mitad no deja el archivo a 0 bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_json_dumps_memoria(obj), encoding="utf-8")
    tmp.replace(path)


def _invalidar_cache_memoria() -> None:
    global _memoria_cache, _memoria_cache_digest, _memoria_cache_revision, _memoria_cache_origen
    _memoria_cache = None
    _memoria_cache_digest = None
    _memoria_cache_revision = None
    _memoria_cache_origen = None


def _store():
    """Postgres si hay DATABASE_URL; si no, SQLite bajo DATA_DIR."""
    from memoria_store import abrir

    return abrir(DATA_DIR)


def _recordar_cache(data: dict, *, origen: str | None, revision: int | None, digest: str | None) -> dict:
    global _memoria_cache, _memoria_cache_digest, _memoria_cache_revision, _memoria_cache_origen
    _memoria_cache = data
    _memoria_cache_origen = origen
    _memoria_cache_revision = revision
    _memoria_cache_digest = digest
    return data


def _digest_memoria_archivo(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _cron_externo_habilitado() -> bool:
    """GitHub cron ya pega /api/auto-bloqueo-externo; evitar duplicar en APScheduler."""
    return bool(os.environ.get("CRON_SECRET", "").strip())


def _cargar_json_memoria(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _respaldar_snapshot(final: dict) -> None:
    """Copia en la base. En local también deja el snapshot de archivo."""
    n_fechas = len(_fechas_con_historial(final)) if isinstance(final, dict) else 0
    keep = 4 if _en_render() else 12
    try:
        _store().guardar_snapshot(final, n_fechas=n_fechas, keep=keep)
    except Exception as e:
        print(f"[GUARDAR] snapshot db: {e}")
    # Render free no conserva el disco. Con Postgres la copia ya está en la base.
    try:
        from memoria_store import database_url

        if _en_render() or database_url():
            return
    except Exception:
        if _en_render():
            return
    try:
        _escribir_snapshot(DATA_DIR, final, keep=keep)
    except Exception as e:
        print(f"[GUARDAR] snapshot: {e}")


def _info_memoria_backup() -> dict:
    try:
        return _store().info_backup()
    except Exception as e:
        print(f"[GUARDAR] backup: {e}")
        return {
            "backup_exists": False,
            "backup_mtime": None,
            "backup_fechas": 0,
        }


def _documento_en_vivo() -> dict | None:
    """Documento de la base, o el JSON legado si la base todavía no tiene fila."""
    try:
        store = _store()
        if store.revision() is not None:
            data = store.cargar()
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[MEMORIA] lectura: {e}")
    return _cargar_json_memoria(MEMORIA_PATH)


def _hay_memoria_guardada() -> bool:
    try:
        if _store().revision() is not None:
            return True
    except Exception:
        pass
    return MEMORIA_PATH.exists()


def _intentar_recuperar_wipe(*, force: bool = False) -> bool:
    """
    Recupera historial del JSON del repo / snapshots si la base perdió días.
    Escribe en la base, no en memoria_auditoria.json del repo.
    """
    global _wipe_check_ts
    if not force and os.environ.get("RENDER"):
        ahora = time.monotonic()
        if ahora - _wipe_check_ts < _WIPE_CHECK_INTERVAL_SEC:
            return False
        _wipe_check_ts = ahora

    disk = _documento_en_vivo()
    if isinstance(disk, dict) and disk.get("reinicio_manual"):
        return False

    candidatos: list[dict] = []
    try:
        snap_db = _store().mejor_snapshot()
    except Exception:
        snap_db = None
    if isinstance(snap_db, dict) and _fechas_con_historial(snap_db):
        candidatos.append(snap_db)
    backup = _cargar_json_memoria(MEMORIA_BACKUP_PATH)
    if isinstance(backup, dict) and _fechas_con_historial(backup):
        candidatos.append(backup)
    origen = BASE_DIR / "memoria_auditoria.json"
    if origen.exists():
        try:
            bundled = json.loads(origen.read_text(encoding="utf-8"))
            if isinstance(bundled, dict):
                b_ap, b_pr = _contar_historial(bundled)
                if (b_ap + b_pr) > 0:
                    candidatos.append(bundled)
        except Exception:
            pass
    snap = _mejor_snapshot(DATA_DIR)
    if isinstance(snap, dict):
        candidatos.append(snap)

    if not candidatos:
        return False

    candidatos.sort(key=lambda m: len(_fechas_con_historial(m)), reverse=True)
    merged = copy.deepcopy(candidatos[0])
    for c in candidatos[1:]:
        merged = _fusionar_memoria(merged, c)
    if isinstance(disk, dict):
        merged = _fusionar_memoria(merged, disk)
        wipe_clasico = _memoria_parece_reinicio(disk)
        dias_perdidos = _backup_tiene_dias_que_el_disco_perdio(merged, disk)
        if not wipe_clasico and not dias_perdidos:
            return False
        if _fechas_con_historial(merged) <= _fechas_con_historial(disk) and not wipe_clasico:
            return False
    elif not _hay_memoria_guardada() or disk is None:
        wipe_clasico = True
        dias_perdidos = True
    else:
        return False

    try:
        revision = _store().guardar(merged)
    except Exception as e:
        print(f"[CLOUD] No se pudo guardar la memoria recuperada: {e}")
        return False
    _respaldar_snapshot(merged)
    b_ap, b_pr = _contar_historial(merged)
    print(
        f"[CLOUD] Memoria recuperada en la base "
        f"(merged {b_ap} apuestas / {b_pr} preds · "
        f"wipe={wipe_clasico} dias_perdidos={dias_perdidos} "
        f"fuentes={len(candidatos)})"
    )
    _recordar_cache(merged, origen="db", revision=revision, digest=None)
    return True


def _inicializar_datos_persistencia() -> None:
    """Siembra la base desde el JSON del repo. No reescribe ese archivo."""
    try:
        from migrar_memoria_db import sembrar_desde_archivos

        legacy = MEMORIA_PATH
        repo_json = BASE_DIR / "memoria_auditoria.json"
        if legacy.resolve() == repo_json.resolve():
            legacy = None
        sembrar_desde_archivos(
            data_dir=DATA_DIR,
            json_repo=repo_json,
            json_legacy=legacy,
        )
    except Exception as e:
        print(f"[CLOUD] No se pudo sembrar la base: {e}")
    try:
        from memoria_store import describir_persistencia

        info = describir_persistencia(DATA_DIR)
        print(
            f"[PERSISTENCIA] backend={info.get('backend')} "
            f"durable={info.get('durable')} destino={info.get('destino')} "
            f"{info.get('aviso') or ''}"
        )
    except Exception as e:
        print(f"[PERSISTENCIA] {e}")
    _invalidar_cache_memoria()
    if DATA_DIR.resolve() != BASE_DIR.resolve():
        # La semilla JSON ya entró arriba. El wipe solo hace falta si quedó
        # un backup de archivo de la versión anterior (el disco de Render no dura).
        legado = MEMORIA_BACKUP_PATH.exists() or bool(_mejor_snapshot(DATA_DIR))
        if legado:
            _intentar_recuperar_wipe(force=True)
        for nombre in ("modelo_rf_mlb.pkl", "scaler_rf_mlb.pkl"):
            src = BASE_DIR / nombre
            dst = DATA_DIR / nombre
            if src.exists() and not dst.exists():
                dst.write_bytes(src.read_bytes())
                print(f"[CLOUD] Modelo ML copiado a {dst}")


def _cron_secret_configurado() -> str:
    return os.environ.get("CRON_SECRET", "").strip()


def _secreto_recibido(
    secret: str | None = None,
    x_cron_secret: str | None = None,
    authorization: str | None = None,
) -> str | None:
    """Query ?secret=, header X-Cron-Secret o Authorization: Bearer."""
    for candidato in (secret, x_cron_secret):
        if candidato and str(candidato).strip():
            return str(candidato).strip()
    auth = (authorization or "").strip()
    if len(auth) >= 7 and auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
        if token:
            return token
    return None


def _verificar_cron_secreto(secret: str | None) -> None:
    """Fail-closed: sin CRON_SECRET en el entorno, rechaza. No compara en claro."""
    esperado = _cron_secret_configurado()
    if not esperado:
        raise HTTPException(
            status_code=503,
            detail="CRON_SECRET no configurado; operación rechazada",
        )
    recibido = (secret or "").strip()
    if not hmac.compare_digest(recibido, esperado):
        raise HTTPException(status_code=403, detail="Cron secret inválido")


def exigir_cron_secreto(
    secret: str | None = None,
    x_cron_secret: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Dependencia de las rutas que mutan banca/memoria o exportan/importan."""
    _verificar_cron_secreto(_secreto_recibido(secret, x_cron_secret, authorization))


def _cron_autorizado(
    secret: str | None = None,
    x_cron_secret: str | None = None,
    authorization: str | None = None,
) -> bool:
    """True solo con CRON_SECRET configurado y un secreto que coincide."""
    esperado = _cron_secret_configurado()
    if not esperado:
        return False
    recibido = _secreto_recibido(secret, x_cron_secret, authorization) or ""
    return hmac.compare_digest(recibido, esperado)


def _auth_cron() -> list:
    """Lista nueva por ruta: no reutilizar el mismo Depends() en varios endpoints."""
    return [Depends(exigir_cron_secreto)]


def cargar_config() -> dict:
    if not CONFIG_PATH.exists():
        # Crear una configuración por defecto si no existe para evitar el cierre
        print(f"[ERROR] No se encontró {CONFIG_PATH.name}. Creando uno básico...")
        cfg_base = {"capital_inicial": 100.0, "dias_totales": 10, "stake_por_juego": 5.0, "timezone": "America/Puerto_Rico", "temporada_mlb": 2026, "lineas": {"api_key": ""}, "estrategia": {"min_edge_pct": 5.0, "max_apuestas_dia": 5, "min_prob_modelo": 52.0}}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg_base, f, indent=2)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    try:
        from mente_errores import aplicar_overrides_config

        cfg = aplicar_overrides_config(cfg)
    except Exception as e:
        print(f"[MENTE-ERRORES] aviso al aplicar overrides: {e}")
    return cfg


def _hidratar_auditoria_momios(data: dict) -> None:
    """El conteo de momios vive en el documento, no en RAM suelta."""
    try:
        from cadena_momios import importar_auditoria_momios

        importar_auditoria_momios(data.get("auditoria_momios"))
    except Exception as e:
        print(f"[MOMIO] No se pudo hidratar la auditoría: {e}")


def cargar_memoria(*, force: bool = False) -> dict:
    """Carga el documento desde la base (o el JSON legado si la base está vacía).

    La cache se invalida con la revisión de la base, no reescribiendo el JSON
    del repositorio. Otras piezas (stake, cuotas, panel) siguen recibiendo el dict.
    """
    try:
        store = _store()
        rev = store.revision()
    except Exception as e:
        print(f"[ERROR] No se pudo abrir la base de memoria: {e}")
        if _memoria_cache is not None:
            return _memoria_cache
        rev = None
        store = None

    if (
        not force
        and store is not None
        and _memoria_cache is not None
        and _memoria_cache_origen == "db"
        and rev is not None
        and rev == _memoria_cache_revision
    ):
        return _memoria_cache

    if rev is not None and store is not None:
        try:
            data = store.cargar()
        except Exception as e:
            print(f"[ERROR] Error inesperado cargando memoria: {e}")
            if _memoria_cache is not None:
                return _memoria_cache
            data = None
        if isinstance(data, dict):
            _hidratar_auditoria_momios(data)
            return _recordar_cache(data, origen="db", revision=rev, digest=None)

    if MEMORIA_PATH.exists():
        digest = _digest_memoria_archivo(MEMORIA_PATH)
        if (
            not force
            and _memoria_cache is not None
            and _memoria_cache_origen == "archivo"
            and digest is not None
            and digest == _memoria_cache_digest
        ):
            return _memoria_cache
        data = _cargar_json_memoria(MEMORIA_PATH)
        if isinstance(data, dict):
            _hidratar_auditoria_momios(data)
            return _recordar_cache(data, origen="archivo", revision=None, digest=digest)
        print(f"[ERROR] {MEMORIA_PATH.name} está corrupto. Se iniciará una nueva memoria.")

    cfg = cargar_config()
    nueva = {
        "modo": "simulacion",
        "capital": cfg["capital_inicial"],
        "capital_inicial": cfg["capital_inicial"],
        "dia_actual": 1,
        "dias_totales": cfg["dias_totales"],
        "stake_por_juego": cfg["stake_por_juego"],
        "experimento_activo": True,
        "ultimo_bloqueo": None,
        "dias": [],
    }
    return _recordar_cache(nueva, origen=None, revision=None, digest=None)


def _memoria_sin_secretos(memoria: dict) -> dict:
    """Copia superficial segura para panel/API (sin token de Telegram)."""
    out = dict(memoria)
    tg = out.get("telegram")
    if isinstance(tg, dict) and tg.get("bot_token"):
        tok = str(tg["bot_token"])
        tg = dict(tg)
        tg["bot_token"] = (tok[:6] + "…" + tok[-4:]) if len(tok) > 12 else "***"
        tg["token_guardado"] = True
        out["telegram"] = tg
    return out


_PRED_PANEL_KEYS = (
    "game_id",
    "visitante",
    "home",
    "pick",
    "odds",
    "odds_american",
    "probPick",
    "resultado",
    "estado",
    "profit",
    "marcador_final",
    "con_dinero",
    "invalida_tarde",
    "valida_stats",
    "confianza_baja",
    "retroactivo",
    "stake_virtual",
    "predicho_en",
    "liquidado_en",
    "lineas_fuente",
    "fuente_momio",
    "casa_momio",
    "paso_momio",
    "origen_momio",
    "estado_registro",
    "sin_momio_real",
    "momio_stale",
    "momio_fallos",
    "linea_movimiento_pct",
    "cuota_retry",
    "clv_pct",
    "clv_entrada_pct",
)
_APUESTA_PANEL_KEYS = (
    "game_id",
    "visitante",
    "home",
    "pick",
    "odds",
    "odds_american",
    "fuente_momio",
    "casa",
    "paso_momio",
    "origen_momio",
    "estado_registro",
    "sin_momio_real",
    "payout_si_gana",
    "momio_fallos",
    "probPick",
    "estado",
    "profit",
    "stake",
    "marcador_final",
    "liquidado_en",
    "clv_pct",
    "clv_entrada_pct",
)
_JUEGO_PANEL_KEYS = (
    "id",
    "visitante",
    "home",
    "estado",
    "estado_apuesta",
    "pick",
    "probPick",
    "odds",
    "odds_american",
    "edge",
    "apostable",
    "motivo_apuesta",
    "hora_inicio_txt",
    "hora_bloqueo_txt",
    "inicio_juego",
    "scoreAway",
    "scoreHome",
    "ganador",
    "profit",
    "stake",
    "solo_papel",
    "resultado_papel",
    "invalida_tarde",
    "confianza_baja",
    "logoAway",
    "logoHome",
    "pitcherAway",
    "pitcherHome",
    "lineas_fuente",
    "fuente_momio",
    "casa_momio",
    "paso_momio",
    "origen_momio",
    "estado_registro",
    "sin_momio_real",
    "momio_stale",
    "payout_si_gana",
    "momio_fallos",
    "pick_congelado",
    "linea_movimiento_pct",
    "bullpen_dia",
    "clv_pct",
)


def _recortar_dict(src: dict, keys: tuple[str, ...]) -> dict:
    return {k: src[k] for k in keys if k in src}


def _memoria_para_panel(memoria: dict) -> dict:
    """Memoria liviana para el panel (sin IA/clima/lesiones anidados ~1MB)."""
    base = _memoria_sin_secretos(memoria)
    dias_out = []
    for dia in base.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        d = {
            "dia": dia.get("dia"),
            "fecha": dia.get("fecha"),
            "bloqueado_en": dia.get("bloqueado_en"),
            "resumen": dia.get("resumen"),
            "predicciones": [
                _recortar_dict(p, _PRED_PANEL_KEYS)
                for p in (dia.get("predicciones") or [])
                if isinstance(p, dict)
            ],
            "apuestas": [
                _recortar_dict(a, _APUESTA_PANEL_KEYS)
                for a in (dia.get("apuestas") or [])
                if isinstance(a, dict)
            ],
        }
        dias_out.append(d)
    base["dias"] = dias_out
    # Lecciones: solo lo que pinta el panel
    lecs = []
    for lec in base.get("lecciones") or []:
        if not isinstance(lec, dict):
            continue
        lecs.append(
            {
                k: lec.get(k)
                for k in (
                    "id",
                    "patron",
                    "titulo",
                    "resumen",
                    "detalle",
                    "game_id",
                    "fecha",
                    "creado_en",
                )
                if k in lec
            }
        )
    if lecs:
        base["lecciones"] = lecs
    return base


def _juegos_para_panel(juegos: list) -> list:
    out = []
    for j in juegos or []:
        if not isinstance(j, dict):
            continue
        row = _recortar_dict(j, _JUEGO_PANEL_KEYS)
        # Mantener un peinado corto de mente si existe
        im = j.get("ia_mente") if isinstance(j.get("ia_mente"), dict) else None
        if im:
            row["ia_mente"] = {
                k: im.get(k)
                for k in ("ok", "decision", "motivo", "confianza", "fuente")
                if k in im
            }
        out.append(row)
    return out


def guardar_memoria(memoria: dict, *, permitir_wipe: bool = False) -> None:
    """Persiste memoria con candado anti-wipe + snapshot rotativo.

    Si 'memoria' borraría días que ya están en disco, se fusiona en vez de pisar
    (salvo permitir_wipe=True en reinicio confirmado).
    """
    with _memoria_lock:
        actual: dict | None = None
        try:
            store = _store()
            if store.revision() is not None:
                actual = store.cargar()
        except Exception:
            store = _store()
            actual = None
        if actual is None and MEMORIA_PATH.exists():
            actual = _cargar_json_memoria(MEMORIA_PATH)
        final, meta = _proteger_escritura(
            actual, memoria, permitir_wipe=permitir_wipe
        )
        if isinstance(actual, dict) and not isinstance(final.get("auditoria_momios"), dict):
            previa = actual.get("auditoria_momios")
            if isinstance(previa, dict):
                final["auditoria_momios"] = copy.deepcopy(previa)
        try:
            from cadena_momios import volcar_auditoria_en

            volcar_auditoria_en(final)
        except Exception as e:
            print(f"[MOMIO] No se pudo volcar la auditoría: {e}")
        if meta.get("protegido"):
            print(
                f"[GUARDAR] Candado anti-wipe: se salvaron fechas "
                f"{meta.get('fechas_salvadas')}"
            )
        print(
            f"[GUARDAR] Guardando memoria en {store.backend}. Capital: {float(final.get('capital') or 0):.2f}, "
            f"Día: {final.get('dia_actual')} · "
            f"fechas={sorted(_fechas_con_historial(final))}"
        )
        revision = store.guardar(final)
        _respaldar_snapshot(final)
        # En Render el panel usa /api/panel-boot; el .js duplica 1–2 MB de RAM.
        # No se reescribe memoria_auditoria.json: ese archivo queda como semilla.
        if not _en_render():
            js_path = DATA_DIR / "memoria_dashboard.js"
            js_path.write_text(
                f"const datosMemoria = {json.dumps(_memoria_para_panel(final), ensure_ascii=False)};",
                encoding="utf-8",
            )
        _recordar_cache(final, origen="db", revision=revision, digest=None)
        if final is not memoria:
            memoria.clear()
            memoria.update(final)
        if _en_render():
            gc.collect()


def tz_experimento() -> ZoneInfo:
    return ZoneInfo(cargar_config()["timezone"])


def ahora_simulado() -> datetime:
    cfg = cargar_config()
    ahora = datetime.now(tz_experimento())
    if ahora.year != cfg["temporada_mlb"]:
        return ahora.replace(year=cfg["temporada_mlb"])
    return ahora


def hoy_local() -> date:
    """Fecha calendario real (Puerto Rico / temporada MLB). No se congela en memoria."""
    return ahora_simulado().date()


def fecha_inicio_experimento(memoria: dict) -> date | None:
    if not memoria.get("dias"):
        return None
    try:
        return datetime.strptime(memoria["dias"][0]["fecha"], "%Y-%m-%d").date()
    except Exception:
        return None


def numero_dia_para_fecha(memoria: dict, fecha: date | None = None) -> int:
    """Día del experimento (1-based) correspondiente a una fecha calendario."""
    fecha = fecha or hoy_local()
    f_inicio = fecha_inicio_experimento(memoria)
    if not f_inicio:
        return int(memoria.get("dia_actual") or 1)
    return max(1, (fecha - f_inicio).days + 1)


def fecha_str(d: date | None = None) -> str:
    d = d or hoy_local()
    return d.strftime("%Y-%m-%d")


def fecha_mlb_api(d: date | None = None) -> str:
    """Formato que acepta statsapi.mlb.com: MM/DD/YYYY."""
    d = d or hoy_local()
    return d.strftime("%m/%d/%Y")


def dia_operativo(memoria: dict) -> dict | None:
    for d in memoria["dias"]:
        if d["dia"] == memoria["dia_actual"]:
            return d
    return None


def dia_por_fecha(memoria: dict, fecha: str) -> dict | None:
    for d in memoria.get("dias", []):
        if d.get("fecha") == fecha:
            return d
    return None


def resumen_dia(dia: dict) -> dict:
    apuestas = dia.get("apuestas", [])
    ganadas = sum(1 for a in apuestas if a["estado"] == "ganada")
    perdidas = sum(1 for a in apuestas if a["estado"] == "perdida")
    pendientes = sum(1 for a in apuestas if a["estado"] == "pendiente")
    profit = round(sum(a.get("profit", 0) or 0 for a in apuestas if a.get("profit") is not None), 2)
    arriesgado = round(
        sum(a["stake"] for a in apuestas if a["estado"] == "pendiente"), 2
    )
    apostado = round(sum(a["stake"] for a in apuestas), 2)
    return {
        "jugadas": len(apuestas),
        "ganadas": ganadas,
        "perdidas": perdidas,
        "pendientes": pendientes,
        "profit_dia": profit,
        "capital_arriesgado": arriesgado,
        "total_apostado": apostado,
    }


def resumen_banca(memoria: dict) -> dict:
    dia = dia_operativo(memoria)
    res = resumen_dia(dia) if dia else {}
    en_juego = res.get("capital_arriesgado", 0)
    return {
        "capital": memoria["capital"],
        "capital_inicial": memoria["capital_inicial"],
        # capital = disponible + en_juego (no se resta stake al abrir).
        "capital_bruto": memoria["capital"],
        "en_juego_hoy": en_juego,
        "disponible": round(memoria["capital"] - en_juego, 2),
        "stake_por_juego": memoria["stake_por_juego"],
        "apuesta_fija": apuesta_fija_dolares(cargar_config()),
    }


def actualizar_resumen(memoria: dict) -> None:
    for d in memoria["dias"]:
        d["resumen"] = resumen_dia(d)


def nombre_equipo_en_pick(pick: str) -> str:
    return pick.replace(" ML", "").strip()


def parse_inicio_juego(game_date: str) -> datetime:
    """gameDate de MLB viene en UTC (ej. 2026-05-19T20:10:00Z)."""
    dt = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
    return dt.astimezone(tz_experimento())


def fecha_oficial_juego(juego_api: dict, inicio: datetime) -> str:
    """Día del partido en el calendario MLB, no el día UTC de gameDate.

    Un juego de las 8pm en la isla sale como el día siguiente en UTC.
    officialDate es el día que publicó MLB.
    """
    oficial = str((juego_api or {}).get("officialDate") or "").strip()
    if len(oficial) == 10 and oficial[4] == "-" and oficial[7] == "-":
        return oficial
    return inicio.date().isoformat()


def hora_bloqueo_para_inicio(inicio: datetime) -> datetime:
    mins = int(cargar_config().get("minutos_antes_juego", 60))
    return inicio - timedelta(minutes=mins)


# Cualquiera de estas abre el congelado si el pick aún no está fijo.
# El catch-up (despertar o cron) congela en cuanto la más temprana ya pasó,
# sin esperar a la siguiente, mientras el primer lanzamiento no haya ocurrido.
VENTANAS_CONGELACION_DEFAULT = (90, 60, 30, 10)


def ventanas_congelacion(cfg: dict | None = None) -> list[int]:
    """Minutos antes del inicio en los que se puede congelar (de más temprano a más tarde)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    raw = cfg.get("ventanas_congelacion")
    if raw is None:
        raw = list(VENTANAS_CONGELACION_DEFAULT)
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return list(VENTANAS_CONGELACION_DEFAULT)
    out: set[int] = set()
    for x in raw:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.add(n)
    if not out:
        return list(VENTANAS_CONGELACION_DEFAULT)
    return sorted(out, reverse=True)


def horizonte_congelacion_min(cfg: dict | None = None) -> int:
    ventanas = ventanas_congelacion(cfg)
    return ventanas[0] if ventanas else 90


def minutos_hasta_inicio(juego: dict, ahora: datetime | None = None) -> float | None:
    """Minutos que faltan para el primer lanzamiento. Negativo si ya pasó."""
    inicio = _parse_iso_dt(juego.get("inicio_juego"))
    if inicio is None:
        return None
    ahora = ahora or ahora_simulado()
    if ahora.tzinfo is None:
        ahora = ahora.replace(tzinfo=inicio.tzinfo or tz_experimento())
    return (inicio - ahora).total_seconds() / 60.0


def ventana_congelacion_abierta(mins_hasta: float, cfg: dict | None = None) -> int | None:
    """Ventana más ajustada ya abierta (T-30 si faltan 25 min). None si aún no es T-90."""
    if mins_hasta <= 0:
        return None
    abierta: int | None = None
    for w in ventanas_congelacion(cfg):
        if mins_hasta <= w:
            abierta = w
        else:
            break
    return abierta


def juego_se_puede_congelar(
    juego: dict,
    cfg: dict | None = None,
    ahora: datetime | None = None,
) -> tuple[bool, str]:
    """
    True solo si el partido sigue PROGRAMADO, falta el primer pitch
    y ya abrió alguna ventana (T-90 o más cerca).
    """
    cfg = cfg or {}
    estado = str(juego.get("estado") or "")
    if estado in ("EN VIVO", "FINALIZADO", "POSPUESTO"):
        return False, f"no se congela en {estado}"
    if estado != "PROGRAMADO":
        return False, f"estado {estado or 'desconocido'}"
    mins = minutos_hasta_inicio(juego, ahora)
    if mins is None:
        return False, "sin hora de inicio"
    if mins <= 0:
        return False, "primer pitch ya pasó"
    abierta = ventana_congelacion_abierta(mins, cfg)
    if abierta is None:
        return False, f"aún no abre T-{horizonte_congelacion_min(cfg)}"
    return True, f"T-{abierta}"


def _ruta_alertas_congelacion() -> Path:
    return DATA_DIR / "ventanas_congelacion.json"


def _alertas_congelacion_vacias() -> dict:
    return {
        "ventanas_perdidas": 0,
        "ventanas_recuperadas": 0,
        "ultima_alerta": None,
        "perdidas": {},
        "recuperadas": {},
    }


def _leer_alertas_congelacion() -> dict:
    path = _ruta_alertas_congelacion()
    base = _alertas_congelacion_vacias()
    if not path.exists():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return base
    if not isinstance(data, dict):
        return base
    perdidas = data.get("perdidas")
    recuperadas = data.get("recuperadas")
    base["perdidas"] = perdidas if isinstance(perdidas, dict) else {}
    base["recuperadas"] = recuperadas if isinstance(recuperadas, dict) else {}
    base["ultima_alerta"] = data.get("ultima_alerta")
    base["ventanas_perdidas"] = len(base["perdidas"])
    base["ventanas_recuperadas"] = len(base["recuperadas"])
    return base


def _guardar_alertas_congelacion(data: dict) -> None:
    data["ventanas_perdidas"] = len(data.get("perdidas") or {})
    data["ventanas_recuperadas"] = len(data.get("recuperadas") or {})
    path = _ruta_alertas_congelacion()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def resumen_congelacion_health(cfg: dict | None = None) -> dict:
    """Conteo para /api/health. No llama a MLB."""
    data = _leer_alertas_congelacion()
    recientes = []
    for gid, info in list((data.get("perdidas") or {}).items())[-8:]:
        if not isinstance(info, dict):
            continue
        recientes.append(
            {
                "id": gid,
                "visitante": info.get("visitante"),
                "home": info.get("home"),
                "motivo": info.get("motivo"),
                "detectado_en": info.get("detectado_en"),
            }
        )
    try:
        ventanas = ventanas_congelacion(cfg or cargar_config())
    except Exception:
        ventanas = list(VENTANAS_CONGELACION_DEFAULT)
    return {
        "ventanas_min": ventanas,
        "ventanas_perdidas": int(data.get("ventanas_perdidas") or 0),
        "ventanas_recuperadas": int(data.get("ventanas_recuperadas") or 0),
        "ultima_alerta": data.get("ultima_alerta"),
        "recientes": recientes,
    }


def anotar_ventanas_perdidas(
    juegos: list[dict],
    ya_congelados: set[str],
    cfg: dict | None = None,
    ahora: datetime | None = None,
) -> dict:
    """
    Cuenta partidos que ya empezaron o terminaron sin pick congelado.
    No inventa el pick: solo deja el aviso para el watchdog.
    """
    try:
        data = _leer_alertas_congelacion()
    except Exception as e:
        print(f"[CONGELAR] no se pudieron leer alertas: {e}")
        return resumen_congelacion_health(cfg)

    ahora = ahora or ahora_simulado()
    perdidas = data.setdefault("perdidas", {})
    nuevas = 0
    for j in juegos or []:
        gid = str(j.get("id") or "")
        if not gid or gid in ya_congelados or gid in perdidas:
            continue
        estado = str(j.get("estado") or "")
        mins = minutos_hasta_inicio(j, ahora)
        if estado == "FINALIZADO":
            motivo = "FINAL sin predicción (ventana perdida; no se congela después del primer pitch)"
        elif estado == "EN VIVO":
            motivo = "EN VIVO sin congelar (primer pitch ya pasó)"
        elif estado == "PROGRAMADO" and mins is not None and mins <= 0:
            motivo = "primer pitch sin pick congelado"
        else:
            continue
        perdidas[gid] = {
            "visitante": j.get("visitante"),
            "home": j.get("home"),
            "estado": estado,
            "motivo": motivo,
            "detectado_en": ahora.isoformat(),
        }
        nuevas += 1
        print(
            f"[CONGELAR] ventana perdida game_id={gid} "
            f"{j.get('visitante')} @ {j.get('home')} · {motivo}"
        )
    if nuevas:
        data["ultima_alerta"] = ahora.isoformat()
        try:
            _guardar_alertas_congelacion(data)
        except Exception as e:
            print(f"[CONGELAR] no se pudo guardar alerta: {e}")
    return resumen_congelacion_health(cfg)


def anotar_congelacion_recuperada(
    juego: dict,
    etiqueta: str,
    cfg: dict | None = None,
    ahora: datetime | None = None,
) -> None:
    """El pick se fijó en una ventana posterior (T-60/T-30/T-10), no en la primera."""
    ventanas = ventanas_congelacion(cfg)
    if not ventanas:
        return
    try:
        num = int(str(etiqueta).removeprefix("T-"))
    except (TypeError, ValueError):
        return
    if num >= ventanas[0]:
        return
    gid = str(juego.get("id") or "")
    if not gid:
        return
    try:
        data = _leer_alertas_congelacion()
    except Exception as e:
        print(f"[CONGELAR] recuperación no leída: {e}")
        return
    rec = data.setdefault("recuperadas", {})
    if gid in rec:
        return
    ahora = ahora or ahora_simulado()
    rec[gid] = {
        "visitante": juego.get("visitante"),
        "home": juego.get("home"),
        "ventana": etiqueta,
        "detectado_en": ahora.isoformat(),
    }
    print(
        f"[CONGELAR] ventana anterior perdida, pick recuperado antes del primer pitch "
        f"game_id={gid} {juego.get('visitante')} @ {juego.get('home')} · {etiqueta}"
    )
    try:
        _guardar_alertas_congelacion(data)
    except Exception as e:
        print(f"[CONGELAR] no se pudo guardar recuperación: {e}")


def _minutos_retry_cuotas(cfg: dict | None = None) -> list[int]:
    """Minutos antes del inicio para reintentar cuotas (ej. T-45, T-30)."""
    cfg = cfg or cargar_config()
    raw = (cfg.get("lineas") or {}).get("minutos_retry_cuotas")
    if raw is None:
        raw = cfg.get("minutos_retry_cuotas")
    if raw is None:
        raw = [45, 30]
    if isinstance(raw, str):
        raw = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    if not isinstance(raw, list):
        return [45, 30]
    limite = int(cfg.get("minutos_antes_juego", 60))
    out = sorted(
        {int(x) for x in raw if 0 < int(x) < limite},
        reverse=True,
    )
    return out or [45, 30]


def _apostable_por_valor(
    registro: dict,
    cfg: dict,
    dec_f: float,
    fuente: str,
) -> tuple[bool, float]:
    """(apostable, edge).

    Con el filtro de valor en sombra se anota el veredicto y manda el edge
    viejo (min_edge / min_prob). Solo si el filtro decide de verdad, el
    margen calibrado aprueba o bloquea.
    """
    from filtro_valor import evaluar_valor, filtro_activo, filtro_valor_decide

    prob = float(registro.get("probPick") or 0)
    edge = edge_pct(prob, dec_f)
    estr = cfg.get("estrategia") or {}
    min_edge = float(estr.get("min_edge_pct", 6.0))
    min_prob = float(estr.get("min_prob_modelo", 58.0))
    legacy = prob >= min_prob and edge >= min_edge
    edge_out = edge if edge > -900 else 0.0
    if not filtro_activo(cfg):
        return legacy, edge_out
    ev = evaluar_valor(
        {
            **registro,
            "odds": dec_f,
            "probPick": prob,
            "lineas_fuente": fuente,
            "fuente_momio": registro.get("fuente_momio"),
        },
        cfg,
    )
    registro["filtro_valor"] = ev
    if not filtro_valor_decide(cfg):
        return legacy, edge_out
    edge_ev = ev.get("edge")
    if edge_ev is None or edge_ev <= -900:
        edge_ev = edge_out
    return bool(ev.get("apostar")), float(edge_ev)


def _aplicar_tipo_sobre(registro: dict, cfg: dict, juego: dict | None = None) -> dict:
    """Reclasifica y aplica el filtro de tipo. El valor no pisa esta decisión."""
    from filtro_valor import aplicar_decision_tipo, filtro_tipo_activo

    if not isinstance(registro, dict) or not filtro_tipo_activo(cfg):
        return {}
    if isinstance(juego, dict):
        for campo in ("scratch_lineup", "lesiones", "visitante", "home", "fuente_momio", "cuota_real_decimal"):
            if registro.get(campo) is None and juego.get(campo) is not None:
                registro[campo] = juego.get(campo)
    try:
        from inteligencia_mlb import clasificar_tipo_pick

        registro["tipo_pick"] = clasificar_tipo_pick(
            registro,
            prob=registro.get("probPick"),
            odds=registro.get("odds"),
        )
    except Exception:
        registro.setdefault("tipo_pick", registro.get("tipo_pick") or "limpio")
    ev = aplicar_decision_tipo(registro, cfg)
    if isinstance(juego, dict):
        juego["tipo_pick"] = registro.get("tipo_pick")
        juego["filtro_tipo"] = registro.get("filtro_tipo")
        juego["apostable"] = bool(registro.get("apostable"))
        if ev.get("decision") in ("cortar", "apostar", "sin_cuota"):
            juego["motivo_apuesta"] = registro.get("motivo_apuesta")
    if ev.get("decision") == "cortar":
        print(f"[FILTRO TIPO] {registro.get('pick')}: underdog cortado")
    elif ev.get("decision") == "apostar":
        print(f"[FILTRO TIPO] {registro.get('pick')}: scratch se apuesta")
    return ev


def _anotar_filtro_valor_bloqueo(juego: dict, pred_existente: dict | None, cfg: dict) -> None:
    """Guarda el veredicto de valor. En sombra no cambia apostable."""
    from filtro_valor import evaluar_valor, filtro_activo, filtro_valor_decide

    if not filtro_activo(cfg) or not isinstance(juego, dict):
        return
    reg_valor = dict(juego)
    if isinstance(pred_existente, dict):
        for campo in ("fuente_momio", "cuota_real_decimal", "precio_congelado", "tipo_pick", "probPick", "odds"):
            if pred_existente.get(campo) is not None and reg_valor.get(campo) is None:
                reg_valor[campo] = pred_existente.get(campo)
    ev = evaluar_valor(reg_valor, cfg)
    juego["filtro_valor"] = ev
    if isinstance(pred_existente, dict):
        pred_existente["filtro_valor"] = ev
    if filtro_valor_decide(cfg) and not ev.get("apostar"):
        motivo = ev.get("motivo") or "Sin valor vs cuota real"
        juego["apostable"] = False
        juego["motivo_apuesta"] = motivo
        if isinstance(pred_existente, dict):
            pred_existente["apostable"] = False
            if ev.get("edge") is not None:
                pred_existente["edge"] = ev.get("edge")
            pred_existente["motivo_apuesta"] = (
                f"{pred_existente.get('motivo_apuesta') or ''} · {motivo}"
            ).strip(" ·")
        return
    if ev.get("sombra"):
        print(
            f"[FILTRO VALOR sombra] {juego.get('pick')}: "
            f"{ev.get('veredicto')} edge={ev.get('edge')} ({ev.get('motivo')})"
        )


def actualizar_mercado_en_prediccion(
    existente: dict,
    juego: dict,
    cfg: dict | None = None,
) -> bool:
    """Si el pick congelado era solo papel y ahora hay cuota real, actualiza odds/edge."""
    if not isinstance(existente, dict) or not isinstance(juego, dict):
        return False

    pick = (existente.get("pick") or "").strip()
    if not pick:
        return False

    cfg = cfg or cargar_config()
    visitante = juego.get("visitante") or existente.get("visitante") or ""
    home = juego.get("home") or existente.get("home") or ""

    if visitante and visitante in pick:
        dec = juego.get("odds_away_decimal")
        amer = juego.get("odds_away_american")
    elif home and home in pick:
        dec = juego.get("odds_home_decimal")
        amer = juego.get("odds_home_american")
    else:
        dec = juego.get("odds")
        amer = juego.get("odds_american")

    try:
        dec_f = float(dec or 0)
    except (TypeError, ValueError):
        return False
    if dec_f <= 1.0:
        return False

    ya_mercado = fuente_es_mercado(existente.get("lineas_fuente")) and tiene_cuota_mercado(existente)
    if ya_mercado:
        mov = calcular_movimiento_linea(existente, dec_f)
        if mov is None:
            return False
        existente["linea_movimiento_pct"] = mov
        existente["odds"] = dec_f
        if amer is not None:
            existente["odds_american"] = amer
        existente["cuota_retry"] = True
        prob = float(existente.get("probPick") or 0)
        fuente_ya = str(existente.get("lineas_fuente") or juego.get("lineas_fuente") or "mercado")
        apostable, edge = _apostable_por_valor(existente, cfg, dec_f, fuente_ya)
        bloqueado_fi, motivo_fi = bloqueado_favorito_inflado(
            {**juego, "probPick": prob, "edge": edge if edge > -900 else 0},
            cfg,
        )
        if bloqueado_fi:
            apostable = False
        bloqueado_le, motivo_le = bloqueado_linea_en_contra(
            {**existente, "edge": edge, "linea_movimiento_pct": mov},
            cfg,
        )
        if bloqueado_le:
            apostable = False
        existente["edge"] = edge if edge > -900 else 0
        existente["apostable"] = apostable
        if bloqueado_le:
            existente["motivo_apuesta"] = motivo_le
        elif bloqueado_fi:
            existente["motivo_apuesta"] = motivo_fi
        juego["linea_movimiento_pct"] = mov
        juego["odds"] = dec_f
        juego["edge"] = existente["edge"]
        juego["apostable"] = apostable
        juego["probPick"] = prob
        if motivo_le and bloqueado_le:
            juego["motivo_apuesta"] = motivo_le
        _aplicar_tipo_sobre(existente, cfg, juego)
        return True

    if not tiene_cuota_mercado(juego):
        return False

    prob = float(existente.get("probPick") or 0)
    fuente = juego.get("lineas_fuente") or "mercado"
    if not existente.get("odds_congelada"):
        existente["odds_congelada"] = existente.get("odds") or dec_f
    if str(existente.get("lineas_fuente") or "").lower() in ("modelo", "", "none"):
        existente["cuota_retry"] = True
        existente["lineas_fuente_inicial"] = existente.get("lineas_fuente") or "modelo"

    reg_valor = dict(existente)
    if juego.get("fuente_momio") and not reg_valor.get("fuente_momio"):
        reg_valor["fuente_momio"] = juego.get("fuente_momio")
    if juego.get("cuota_real_decimal") and not reg_valor.get("cuota_real_decimal"):
        reg_valor["cuota_real_decimal"] = juego.get("cuota_real_decimal")
    apostable, edge = _apostable_por_valor(reg_valor, cfg, dec_f, fuente)
    if isinstance(reg_valor.get("filtro_valor"), dict):
        existente["filtro_valor"] = reg_valor["filtro_valor"]
    bloqueado, motivo_fi = bloqueado_favorito_inflado(
        {**juego, "probPick": prob, "edge": edge if edge > -900 else 0},
        cfg,
    )
    if bloqueado:
        apostable = False

    mov = calcular_movimiento_linea(existente, dec_f)
    if mov is not None:
        existente["linea_movimiento_pct"] = mov

    bloqueado_le, motivo_le = bloqueado_linea_en_contra(
        {**existente, "edge": edge if edge > -900 else 0, "linea_movimiento_pct": mov},
        cfg,
    )
    if bloqueado_le:
        apostable = False

    existente["lineas_fuente"] = fuente
    existente["odds"] = dec_f
    if amer is not None:
        existente["odds_american"] = amer
    existente["edge"] = edge if edge > -900 else 0
    existente["apostable"] = apostable
    if apostable:
        existente["motivo_apuesta"] = f"Valor +{edge:.1f}% vs {fuente} (cuota actualizada)"
    elif bloqueado_le:
        existente["motivo_apuesta"] = motivo_le
    elif bloqueado:
        existente["motivo_apuesta"] = motivo_fi
    elif "sin cuota real" in (existente.get("motivo_apuesta") or "").lower():
        existente["motivo_apuesta"] = (
            f"Modelo {prob:.0f}% · cuota {fuente} sin valor (+{max(edge, 0):.1f}% edge)"
        )

    juego["lineas_fuente"] = fuente
    juego["odds"] = dec_f
    juego["odds_american"] = amer
    juego["edge"] = existente["edge"]
    juego["apostable"] = apostable
    juego["pick"] = pick
    juego["probPick"] = prob
    if mov is not None:
        juego["linea_movimiento_pct"] = mov
    if bloqueado_le:
        juego["motivo_apuesta"] = motivo_le
    _aplicar_tipo_sobre(existente, cfg, juego)
    try:
        actualizar_clv_registro(existente, juego, fase="cierre")
        if not existente.get("clv_odds_entrada"):
            actualizar_clv_registro(existente, juego, fase="entrada")
    except Exception as e:
        print(f"[CLV] aviso actualizar mercado: {e}")
    return True


def contar_apuestas_hoy(memoria: dict, fecha: str | None = None) -> int:
    fecha = fecha or fecha_str()
    dia = dia_operativo(memoria)
    if not dia or dia["fecha"] != fecha:
        return 0
    return len(dia.get("apuestas", []))


def asegurar_dia_operativo(memoria: dict, fecha: str | None = None) -> dict:
    fecha = fecha or fecha_str()
    existente = dia_por_fecha(memoria, fecha)
    if existente:
        return existente

    try:
        f = datetime.strptime(fecha, "%Y-%m-%d").date()
        num = numero_dia_para_fecha(memoria, f)
    except Exception:
        num = int(memoria.get("dia_actual") or 1)

    # Evitar duplicar el número de día si ya existe otra fecha con ese índice
    for d in memoria.get("dias", []):
        if d.get("dia") == num and d.get("fecha") != fecha:
            num = max(int(x.get("dia") or 0) for x in memoria["dias"]) + 1
            break

    dia = {
        "dia": num,
        "fecha": fecha,
        "bloqueado_en": None,
        "apuestas": [],
        "predicciones": [],
        "resumen": {},
    }
    memoria["dias"].append(dia)
    memoria["dias"].sort(key=lambda x: x.get("fecha") or "")
    return dia


def calcular_bias_aprendizaje(memoria: dict) -> float:
    """
    Auto-aprendizaje: WR solo con apuestas de DINERO REAL.
    El papel (cuotas estimadas) no mueve el bias — evita sobreconfianza.
    """
    from aprendizaje_mlb import PESO_DINERO

    muestras: list[tuple[bool, float]] = []
    for d in memoria.get("dias", []):
        for ap in d.get("apuestas", []):
            if ap.get("estado") not in ("ganada", "perdida"):
                continue
            muestras.append((ap["estado"] == "ganada", float(PESO_DINERO)))

    peso_total = sum(w for _, w in muestras)
    min_muestras = 5.0
    if peso_total < min_muestras:
        return 0.0

    win_rate = sum(w for ok, w in muestras if ok) / peso_total
    if win_rate < 0.45:
        print(f"[APRENDIZAJE] WR dinero bajo ({win_rate:.1%}, n≈{peso_total:.0f}). Bias cauteloso.")
        return -1.2
    if win_rate > 0.60:
        print(f"[APRENDIZAJE] WR dinero alto ({win_rate:.1%}). Modelo con confianza.")
        return 0.5
    return 0.0


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz_experimento())
        return dt
    except Exception:
        return None


def prediccion_valida_para_stats(pred: dict, gracia_min: float = 5.0) -> bool:
    """
    True solo si el pick se congeló ANTES (o casi al) inicio.
    Excluye EN VIVO / retroactivos / cambios a última hora (ej. Yankees mid-game).
    """
    if not isinstance(pred, dict):
        return False
    if pred.get("valida_stats") is False or pred.get("invalida_tarde"):
        return False
    if pred.get("retroactivo"):
        return False
    motivo = (pred.get("motivo_apuesta") or "").upper()
    if "EN VIVO" in motivo and "GRACIA" not in motivo:
        # Motivo explícito de freeze tardío
        return False
    predicho = _parse_iso_dt(pred.get("predicho_en"))
    inicio = _parse_iso_dt(pred.get("inicio_juego"))
    if predicho and inicio:
        mins = (predicho - inicio).total_seconds() / 60.0
        if mins > gracia_min:
            return False
    return True


def marcar_predicciones_tardias(memoria: dict, gracia_min: float = 5.0) -> int:
    """Marca en memoria los picks congelados después del inicio (no borra el marcador)."""
    n = 0
    for dia in memoria.get("dias", []):
        for p in dia.get("predicciones", []) or []:
            if p.get("invalida_tarde"):
                continue
            if prediccion_valida_para_stats(p, gracia_min=gracia_min):
                # Asegura flag positivo si faltaba
                if "valida_stats" not in p:
                    p["valida_stats"] = True
                continue
            p["invalida_tarde"] = True
            p["valida_stats"] = False
            n += 1
    return n


_TAG_LEAN = "Lean débil · se muestra quién gana, no cuenta en precisión"


def limpiar_predicciones_confianza_baja(memoria: dict) -> int:
    """Quita el filtro por %: todos los picks de quién gana vuelven a contar."""
    n = 0
    tag = _TAG_LEAN.lower()
    for dia in memoria.get("dias", []) or []:
        for p in dia.get("predicciones") or []:
            motivo = str(p.get("motivo_apuesta") or "")
            tenia = bool(p.get("confianza_baja")) or tag in motivo.lower()
            if not tenia:
                continue
            p["confianza_baja"] = False
            if not p.get("invalida_tarde") and not p.get("retroactivo"):
                p["valida_stats"] = True
            if tag in motivo.lower():
                limpio = motivo
                for sep in (f" · {_TAG_LEAN}", f"· {_TAG_LEAN}", _TAG_LEAN):
                    limpio = limpio.replace(sep, "")
                p["motivo_apuesta"] = " ".join(limpio.split()).strip(" ·")
            n += 1
    return n


def calcular_estadisticas_modelo(memoria: dict) -> dict:
    """
    Calcula aciertos/fallos del modelo.
    Si un juego tiene apuesta, no se cuenta también su predicción (evita doble conteo).
    Ignora picks congelados en vivo / después del inicio.
    """
    total_predicciones = 0
    aciertos = 0
    fallos = 0
    excluidas_tarde = 0
    
    for dia in memoria.get("dias", []):
        apostados = {
            str(a.get("game_id"))
            for a in dia.get("apuestas", [])
            if a.get("estado") in ("ganada", "perdida", "pendiente")
        }
        for apuesta in dia.get("apuestas", []):
            if apuesta["estado"] in ("ganada", "perdida"):
                total_predicciones += 1
                if apuesta["estado"] == "ganada":
                    aciertos += 1
                else:
                    fallos += 1

        for prediccion in dia.get("predicciones", []):
            if str(prediccion.get("game_id") or "") in apostados:
                continue
            if prediccion.get("estado") != "liquidado":
                continue
            if not prediccion_valida_para_stats(prediccion):
                excluidas_tarde += 1
                continue
            total_predicciones += 1
            if prediccion.get("resultado") == "acierto":
                aciertos += 1
            else:
                fallos += 1
    
    win_rate = (aciertos / total_predicciones * 100) if total_predicciones > 0 else 0
    
    return {
        "total_predicciones": total_predicciones,
        "aciertos": aciertos,
        "fallos": fallos,
        "win_rate": round(win_rate, 1),
        "excluidas_tarde": excluidas_tarde,
    }


# ---------------------------------------------------------------------------
# API MLB
# ---------------------------------------------------------------------------

def _score_equipo(linescore_side: dict, team_side: dict) -> int:
    """Lee carreras sin tratar 0 como vacío (bug de `x or y`)."""
    runs = linescore_side.get("runs")
    if runs is not None:
        return int(runs)
    score = team_side.get("score")
    if score is not None:
        return int(score)
    return 0


def _lineas_para_panel(cfg: dict | None = None) -> dict:
    """Meta de cuotas + config visible en el panel (bookmakers, retries)."""
    cfg = cfg or cargar_config()
    lineas_cfg = cfg.get("lineas") or {}
    out = dict(_lineas_meta_cache if isinstance(_lineas_meta_cache, dict) else {})
    out["bookmakers"] = lineas_cfg.get("bookmakers") or "draftkings"
    out["minutos_retry_cuotas"] = _minutos_retry_cuotas(cfg)
    return redactar_secreto(out, cargar_api_key(cfg))


def _mercado_requiere_cuotas(cfg: dict | None = None) -> bool:
    cfg = cfg or cargar_config()
    if cfg.get("modo_solo_modelo"):
        return False
    return bool((cfg.get("estrategia") or {}).get("requiere_betmgm", True))


def precalentar_cuotas_mercado(cfg: dict | None = None) -> dict:
    """Refresca cuotas ANTES del cron T-60 (ESPN/DraftKings)."""
    global _lineas_meta_cache
    cfg = cfg or cargar_config()
    if not _mercado_requiere_cuotas(cfg):
        return {"ok": True, "omitido": True, "motivo": "modo_papel"}
    hoy = fecha_str()
    juegos = obtener_juegos_fecha(hoy, solo_resultados=True)
    if not juegos:
        return {"ok": False, "motivo": "sin_juegos_hoy"}
    _, meta = aplicar_lineas_a_juegos(juegos, cfg)
    meta = meta if isinstance(meta, dict) else {}
    _lineas_meta_cache = meta
    if meta.get("ok"):
        print(
            f"[CUOTAS] Precalentado OK · {meta.get('partidos', '?')} partidos · "
            f"{meta.get('fuente', meta.get('mensaje', ''))[:60]}"
        )
        return {"ok": True, **meta}
    try:
        from mente_errores import aplicar_overrides_config

        cfg2 = aplicar_overrides_config(cfg)
        _, meta2 = aplicar_lineas_a_juegos(juegos, cfg2)
        meta2 = meta2 if isinstance(meta2, dict) else {}
        if meta2.get("ok"):
            _lineas_meta_cache = meta2
            print(f"[CUOTAS] Precalentado ESPN (override) · {meta2.get('mensaje', '')[:80]}")
            return {"ok": True, "forzado_espn": True, **meta2}
    except Exception as e:
        print(f"[CUOTAS] override ESPN: {e}")
    print(f"[CUOTAS] Precalentado falló: {meta.get('mensaje', '?')[:100]}")
    return {"ok": False, **meta}


def estado_desde_status_mlb(status_info: dict | None) -> str:
    """Traduce el status de MLB. Suspendido abierto no es push."""
    status_info = status_info or {}
    abs_state = str(status_info.get("abstractGameState") or "")
    coded = str(status_info.get("codedGameState") or status_info.get("statusCode") or "")
    detailed = str(status_info.get("detailedState") or "")
    if "Suspended" in detailed or coded in ("T", "U"):
        if abs_state == "Final" or "Final" in detailed:
            return "SUSPENDIDO_OFICIAL"
        return "SUSPENDIDO"
    if coded == "C" or "Cancelled" in detailed or "Canceled" in detailed:
        return "CANCELADO"
    if coded in ("D", "DR", "DI") or "Postponed" in detailed:
        return "POSPUESTO"
    if (
        abs_state == "Live"
        or coded in ("I", "IW", "IR")
        or "In Progress" in detailed
        or "Warmup" in detailed
        or "Manager Challenge" in detailed
    ):
        return "EN VIVO"
    if (
        abs_state == "Final"
        or coded in ("F", "O", "FT", "FR")
        or detailed in ("Final", "Game Over", "Completed Early")
    ):
        return "FINALIZADO"
    return "PROGRAMADO"


def obtener_juegos_fecha(fecha: str | None = None, solo_resultados: bool = False) -> list[dict]:
    memoria = cargar_memoria()
    params = {"sportId": 1, "hydrate": "probablePitcher,lineups,linescore,team,officials"}
    if fecha:
        m, d, y = fecha.split("-")[1], fecha.split("-")[2], fecha.split("-")[0]
        params["date"] = f"{m}/{d}/{y}"
    try:
        r = requests.get(MLB_SCHEDULE, params=params, timeout=12)
        r.raise_for_status()
        datos = r.json()
    except requests.RequestException as e:
        print(f"[MLB API] Error al solicitar juegos para {params.get('date', 'hoy')}: {e}")
        return []
    juegos = []
    if not datos.get("dates") or len(datos["dates"]) == 0:
        print(f"[MLB API] No se encontraron juegos en la respuesta para {fecha}")
        return juegos

    cfg = cargar_config()
    for date_entry in datos["dates"]:
        for juego in date_entry.get("games", []):
            status_info = juego.get("status", {})
            abs_state = status_info.get("abstractGameState", "")
            coded = (
                status_info.get("codedGameState")
                or status_info.get("statusCode")
                or ""
            )
            detailed = status_info.get("detailedState", "")

            # Solo FINALIZADO con códigos oficiales MLB. Nunca por marcador en vivo.
            # Postponed/Cancelled a veces vienen con abstractGameState=Final: no liquidar como ganada.
            # Suspended no se anula aquí: sigue pendiente hasta el final o un cierre oficial.
            estado = estado_desde_status_mlb(status_info)

            away = juego["teams"]["away"]
            home = juego["teams"]["home"]
            visitante = away["team"]["name"]
            home_name = home["team"]["name"]
            try:
                from lineup_scratch import parsear_lineups_juego

                lineups_parsed = parsear_lineups_juego(juego)
            except Exception:
                lineups_parsed = {"away": [], "home": [], "confirmado": False}
            lineup_confirmado = bool(lineups_parsed.get("confirmado"))
            pa = away.get("probablePitcher") or {}
            ph = home.get("probablePitcher") or {}
            ls = juego.get("linescore", {}).get("teams", {})
            s_away = _score_equipo(ls.get("away", {}), away)
            s_home = _score_equipo(ls.get("home", {}), home)
            inicio = parse_inicio_juego(juego["gameDate"])
            bloqueo = hora_bloqueo_para_inicio(inicio)
            # Ganador oficial solo al finalizar: prioriza isWinner de MLB.
            winner = None
            if estado == "FINALIZADO":
                if away.get("isWinner") is True:
                    winner = visitante
                elif home.get("isWinner") is True:
                    winner = home_name
                elif s_away > s_home:
                    winner = visitante
                elif s_home > s_away:
                    winner = home_name
            juegos.append({
                "id": str(juego["gamePk"]),
                "fecha": fecha_oficial_juego(juego, inicio),
                "estado": estado,
                "visitante": visitante,
                "away_id": away["team"]["id"],
                "home_id": home["team"]["id"],
                "away_abbr": (away.get("team") or {}).get("abbreviation"),
                "home_abbr": (home.get("team") or {}).get("abbreviation"),
                "pitcher_away_id": pa.get("id"),
                "pitcher_home_id": ph.get("id"),
                "pitcherAway": pa.get("fullName"),
                "pitcherHome": ph.get("fullName"),
                "scoreAway": s_away,
                "home": home_name,
                "scoreHome": s_home,
                "pick": "",
                "odds": 0,
                "lineup_confirmado": lineup_confirmado,
                "lineups": lineups_parsed,
                "apostable": False,
                "ganador": winner,
                "inicio_juego": inicio.isoformat(),
                "hora_bloqueo": bloqueo.isoformat(),
                "hora_inicio_txt": inicio.strftime("%I:%M %p"),
                "hora_bloqueo_txt": bloqueo.strftime("%I:%M %p"),
                "logoAway": f"https://www.mlbstatic.com/team-logos/{away['team']['id']}.svg",
                "logoHome": f"https://www.mlbstatic.com/team-logos/{home['team']['id']}.svg",
                "series_game_number": juego.get("seriesGameNumber"),
                "games_in_series": juego.get("gamesInSeries"),
                "day_night": juego.get("dayNight"),
                "officials": juego.get("officials") or [],
                "venue_id": (juego.get("venue") or {}).get("id"),
                "venue_name": (juego.get("venue") or {}).get("name"),
            })

    global _lineas_meta_cache
    print(f"[INFO] Se encontraron {len(juegos)} juegos. Procesando líneas...")
    
    if not solo_resultados:
        if cfg.get("modo_solo_modelo") or not cfg.get("estrategia", {}).get("requiere_betmgm", True):
            _lineas_meta_cache = {
                "ok": True,
                "fuente": "modelo",
                "mensaje": "Modo solo modelo (sin cuotas de mercado)",
                "partidos": len(juegos),
            }
            bias = calcular_bias_aprendizaje(memoria)
            juegos = evaluar_juegos(juegos, cfg, bias)
        else:
            juegos, _lineas_meta_cache = aplicar_lineas_a_juegos(juegos, cfg)
            bias = calcular_bias_aprendizaje(memoria)
            # Sin casa no se apaga el dinero: el modelo estima con vig -110
            # y esas apuestas quedan aparte del ROI de cuota real.
            if not (_lineas_meta_cache or {}).get("ok"):
                _lineas_meta_cache = {
                    **(_lineas_meta_cache or {}),
                    "fallback_estimado": True,
                    "mensaje": (
                        ((_lineas_meta_cache or {}).get("mensaje"))
                        or "Sin momio real, solo registrado."
                    ),
                }
            juegos = evaluar_juegos(juegos, cfg, bias)
    else:
        print(f"[INFO] Modo solo_resultados activo para {fecha or 'hoy'}. Saltando IA y Cuotas.")
        
    return juegos


def _juego_finalizado(juego: dict) -> bool:
    """Solo liquidar cuando MLB reporta el juego como final."""
    return juego.get("estado") == "FINALIZADO"


def _ganador_oficial(juego: dict) -> str:
    """Nombre normalizado del ganador oficial, o '' si aún no hay."""
    if not _juego_finalizado(juego):
        return ""
    ganador = juego.get("ganador") or ""
    if ganador:
        return norm_nombre(ganador)
    s_away = int(juego.get("scoreAway") or 0)
    s_home = int(juego.get("scoreHome") or 0)
    if s_away == s_home:
        return ""
    if s_away > s_home:
        return norm_nombre(juego["visitante"])
    return norm_nombre(juego["home"])


def _revertir_liquidacion_prematura(apuesta: dict, juego: dict) -> bool:
    """Si se liquidó por error con el juego aún no final, vuelve a pendiente."""
    if apuesta.get("estado") not in ("ganada", "perdida"):
        return False
    # No tocar liquidaciones si MLB ya marca Final (aunque falte isWinner un momento).
    if _juego_finalizado(juego):
        return False
    if juego.get("estado") not in (
        "EN VIVO",
        "PROGRAMADO",
        "POSPUESTO",
        "CANCELADO",
        "SUSPENDIDO",
        "SUSPENDIDO_OFICIAL",
    ):
        return False
    apuesta["estado"] = "pendiente"
    apuesta["profit"] = None
    apuesta.pop("marcador_final", None)
    apuesta.pop("liquidado_en", None)
    print(f"[LIQUIDACIÓN] Revertida liquidación prematura juego {juego.get('id')} (aún {juego.get('estado')})")
    return True


def _es_anulado(juego: dict) -> bool:
    """Postpuesto o cancelado devuelven el stake. Suspendido abierto no.

    Un suspendido solo es push si MLB ya lo cerró sin completarlo
    (SUSPENDIDO_OFICIAL). Si sigue abierto, la apuesta espera el final.
    """
    return juego.get("estado") in ("POSPUESTO", "CANCELADO", "SUSPENDIDO_OFICIAL")


def _es_empate_final(juego: dict) -> bool:
    if not _juego_finalizado(juego) or _ganador_oficial(juego):
        return False
    try:
        return int(juego.get("scoreAway") or 0) == int(juego.get("scoreHome") or 0)
    except (TypeError, ValueError):
        return False


def _profit_al_liquidar(apuesta: dict, stake: float, estado: str) -> float | None:
    """Apuestas nuevas cobran el momio americano congelado. Las viejas no se reescriben."""
    if apuesta.get("fuente_momio") is None:
        odds = float(apuesta.get("odds") or 0)
        if estado == "ganada":
            if odds <= 1.0:
                return None
            return round(stake * (odds - 1), 2)
        if estado == "perdida":
            return round(-stake, 2)
        if estado == "push":
            return 0.0
        return None
    american = apuesta.get("odds_american")
    try:
        momio = int(american)
    except (TypeError, ValueError):
        momio = 0
    if momio == 0:
        print(
            f"[LIQUIDACIÓN] {apuesta.get('pick')} sin momio americano congelado "
            f"(fuente {apuesta.get('fuente_momio')}). No se inventa el pago."
        )
        return None
    return profit_moneyline_americano(stake, momio, estado)


def liquidar_apuesta(apuesta: dict, juego: dict, stake: float) -> bool:
    """Liquida si el juego finalizó. Devuelve True si hubo cambio."""
    if _revertir_liquidacion_prematura(apuesta, juego):
        return True

    # Push/void solo en apuestas del régimen nuevo. El historial ya liquidado no se toca.
    if (
        apuesta.get("fuente_momio") is not None
        and juego.get("estado") == "SUSPENDIDO"
        and apuesta.get("estado") == "pendiente"
    ):
        print(
            f"[LIQUIDACIÓN] Juego {juego.get('id')} SUSPENDIDO: "
            f"{apuesta.get('pick')} sigue pendiente hasta el final oficial"
        )
        return False

    if apuesta.get("fuente_momio") is not None and (_es_anulado(juego) or _es_empate_final(juego)):
        if apuesta.get("estado") == "push" and apuesta.get("profit") == 0:
            return False
        stake_usada = float(apuesta.get("stake") or stake)
        apuesta["estado"] = "push"
        apuesta["profit"] = 0.0
        apuesta["cierre"] = "void" if _es_anulado(juego) else "push"
        apuesta["stake"] = stake_usada
        apuesta["marcador_final"] = (
            f"{juego.get('visitante')} {juego.get('scoreAway')} - "
            f"{juego.get('home')} {juego.get('scoreHome')}"
        )
        apuesta["liquidado_en"] = datetime.now(tz_experimento()).isoformat()
        print(
            f"[MOTOR] Juego {juego.get('id')} {apuesta['cierre'].upper()} "
            f"({apuesta.get('pick')}): stake devuelto"
        )
        return True

    if not _juego_finalizado(juego):
        print(f"[DEBUG LIQ] Juego {juego['id']} no terminado. Estado: {juego.get('estado')}")
        return False

    pick_norm = norm_nombre(nombre_equipo_en_pick(apuesta["pick"]))
    ganador_norm = _ganador_oficial(juego)

    if not ganador_norm:
        print(f"[DEBUG LIQ] Juego {juego['id']} FINALIZADO pero sin ganador oficial.")
        return False

    print(f"[LIQUIDACIÓN] Juego {juego['id']}: Comparando Pick '{pick_norm}' vs Ganador '{ganador_norm}'")

    nuevo_estado = "ganada" if pick_norm == ganador_norm else "perdida"
    nuevo_marcador = (
        f"{juego['visitante']} {juego['scoreAway']} - "
        f"{juego['home']} {juego['scoreHome']}"
    )

    if apuesta.get("estado") == nuevo_estado and apuesta.get("marcador_final") == nuevo_marcador:
        print(f"[DEBUG LIQ] Juego {juego['id']} ya liquidado con el mismo estado ({nuevo_estado}).")
        return False

    stake_usada = float(apuesta.get("stake") if apuesta.get("stake") is not None else stake)
    profit = _profit_al_liquidar(apuesta, stake_usada, nuevo_estado)
    if profit is None:
        return False

    apuesta["estado"] = nuevo_estado
    apuesta["stake"] = stake_usada
    apuesta["profit"] = profit

    apuesta["marcador_final"] = nuevo_marcador
    print(f"[MOTOR] Juego {juego['id']} actualizado automáticamente: {nuevo_estado.upper()} ({apuesta['profit']:+.2f})")

    apuesta["liquidado_en"] = datetime.now(tz_experimento()).isoformat()
    return True


def recalcular_capital(memoria: dict) -> None:
    cfg = cargar_config()
    capital_inicial = cfg["capital_inicial"]
    total_ganado = 0.0
    total_perdido = 0.0
    
    for dia in memoria["dias"]:
        for a in dia.get("apuestas", []):
            if a["estado"] in ("ganada", "perdida"):
                profit = float(a.get("profit") or 0)
                if profit > 0:
                    total_ganado += profit
                else:
                    total_perdido += abs(profit)
                    
    memoria["capital"] = round(capital_inicial + total_ganado - total_perdido, 2)
    
    print("=" * 45)
    print(f" AUDITORÍA DE BANCA ACUMULADA (Día 1-10)")
    print(f" (+) Ganancia Total:   ${total_ganado:>8.2f}")
    print(f" (-) Pérdida Total:    ${total_perdido:>8.2f}")
    print(f" (=) Capital Actual:   ${memoria['capital']:>8.2f}")
    print("=" * 45)
    
    # Guardar inmediatamente para que los cambios persistan en el disco
    guardar_memoria(memoria)


def revisar_apuestas_colgadas(memoria: dict, *, log: bool = False) -> dict[str, int]:
    """Cuenta pendientes de más de 24h y precios congelados ausentes o en cero."""
    from cadena_momios import parsear_momio_americano

    ahora = datetime.now(tz_experimento())
    pendientes_24h = 0
    sin_precio = 0
    for dia in memoria.get("dias") or []:
        for apuesta in dia.get("apuestas") or []:
            if apuesta.get("estado") != "pendiente":
                continue
            if apuesta.get("fuente_momio") is None:
                try:
                    odds = float(apuesta.get("odds") or 0)
                except (TypeError, ValueError):
                    odds = 0.0
                if odds <= 1.0:
                    sin_precio += 1
                    if log:
                        print(f"[ALERTA] {apuesta.get('pick')} sin precio decimal congelado")
            elif parsear_momio_americano(apuesta.get("odds_american"), registrar=False) is None:
                sin_precio += 1
                if log:
                    print(
                        f"[ALERTA] {apuesta.get('pick')} sin momio americano congelado "
                        f"(ausente o 0, fuente {apuesta.get('fuente_momio')})"
                    )
            marca = apuesta.get("bloqueado_en") or apuesta.get("inicio_juego")
            try:
                cuando = datetime.fromisoformat(str(marca)) if marca else None
            except ValueError:
                cuando = None
            if cuando is not None:
                if cuando.tzinfo is None:
                    cuando = cuando.replace(tzinfo=tz_experimento())
                if ahora - cuando > timedelta(hours=24):
                    pendientes_24h += 1
                    if log:
                        print(f"[ALERTA] {apuesta.get('pick')} pendiente más de 24h")
    return {"pendientes_24h": pendientes_24h, "sin_precio_congelado": sin_precio}


def _salud_momios(memoria: dict) -> dict:
    """Bloque de solo lectura para /api/health."""
    from cadena_momios import edad_ultimo_fetch_seg, resumen_fuentes, resumen_rechazos

    hoy = fecha_str()
    dia = next((d for d in memoria.get("dias") or [] if d.get("fecha") == hoy), None)
    preds = (dia or {}).get("predicciones") or []
    por_casa: dict[str, int] = {}
    reales = 0
    sin_real = 0
    for pred in preds:
        fuente = str(pred.get("fuente_momio") or pred.get("lineas_fuente") or "").strip().lower()
        if (
            fuente in ("sin_momio_real", "estimado")
            or pred.get("sin_momio_real")
            or pred.get("estado_registro") == "registrado sin apuesta"
        ):
            sin_real += 1
            continue
        if fuente and fuente not in ("", "modelo", "none", "null", "import"):
            reales += 1
            casa = str(pred.get("casa_momio") or pred.get("casa") or fuente)
            por_casa[casa] = por_casa.get(casa, 0) + 1
    total = reales + sin_real
    share = (sin_real / total) if total else 0.0
    cruce = 0
    for d in memoria.get("dias") or []:
        for apuesta in d.get("apuestas") or []:
            if apuesta.get("cruce_momio_alerta"):
                cruce += 1
    edad = edad_ultimo_fetch_seg()
    rech = resumen_rechazos()
    colgadas = revisar_apuestas_colgadas(memoria, log=False)
    return {
        "fecha": hoy,
        "reales": reales,
        "por_casa": por_casa,
        "fuentes": resumen_fuentes(),
        "sin_momio_real": sin_real,
        "rechazados": rech["total"],
        "rechazados_razones": rech["razones"],
        "ultimo_fetch_hace_seg": None if edad is None else round(edad, 1),
        "alerta_sin_momio_real": bool(total and share > 0.20),
        "sin_momio_real_pct": round(100 * share, 1) if total else 0.0,
        "cruce_espn_alertas": cruce,
        "pendientes_24h": colgadas["pendientes_24h"],
        "sin_precio_congelado": colgadas["sin_precio_congelado"],
    }


def _cruzar_momios_espn(dia: dict) -> bool:
    """Compara el momio guardado con el cierre ESPN. Nunca tumba la liquidación."""
    from lineas_espn import cruzar_momio_apuesta

    cambio = False
    ahora = datetime.now(tz_experimento())
    for apuesta in dia.get("apuestas") or []:
        if apuesta.get("odds_american") in (None, ""):
            continue
        previo = apuesta.get("cruce_espn") if isinstance(apuesta.get("cruce_espn"), dict) else None
        if previo and previo.get("ok"):
            continue
        if previo and previo.get("revisado_en"):
            try:
                visto = datetime.fromisoformat(str(previo["revisado_en"]))
                if visto.tzinfo is None:
                    visto = visto.replace(tzinfo=tz_experimento())
                if ahora - visto < timedelta(hours=6):
                    continue
            except ValueError:
                pass
        try:
            res = cruzar_momio_apuesta(apuesta, timeout=6.0)
        except Exception as e:
            print(f"[CRUCE] ESPN falló game {apuesta.get('game_id')}: {e}")
            res = {"ok": False, "motivo": str(e)[:180]}
        apuesta["cruce_espn"] = {**res, "revisado_en": ahora.isoformat()}
        if res.get("ok"):
            apuesta["cruce_momio_alerta"] = bool(res.get("alerta"))
        cambio = True
    return cambio


def liquidar_dia(memoria: dict, dia: dict) -> int:
    apuestas = dia.get("apuestas", [])
    preds = dia.get("predicciones", [])
    if not apuestas and not preds:
        return 0

    apuestas_pendientes = any(a["estado"] == "pendiente" for a in apuestas)
    predicciones_pendientes = any(p.get("estado") == "pendiente" for p in preds)
    puede_revertir = any(
        a["estado"] in ("ganada", "perdida") for a in apuestas
    ) or any(p.get("estado") == "liquidado" for p in preds)

    if not apuestas_pendientes and not predicciones_pendientes and not puede_revertir:
        return 0

    # Solo marcador/ganador MLB: no reevaluar modelo ni cuotas (evita timeouts en Render).
    juegos = obtener_juegos_fecha(dia["fecha"], solo_resultados=True)
    if not juegos:
        print(f"[DEBUG LIQ DIA] No se encontraron juegos para el día {dia['fecha']}. No se liquida.")
        return 0

    with _memoria_lock:
        return _liquidar_dia_con_juegos(memoria, dia, juegos)


def _liquidar_dia_con_juegos(memoria: dict, dia: dict, juegos: list) -> int:
    apuestas = dia.get("apuestas", [])
    preds = dia.get("predicciones", [])
    por_id = {str(g["id"]): g for g in juegos}
    cambios = 0

    # Elo: actualizar ratings con finales (idempotente por game_id)
    try:
        from elo_mlb import actualizar_elo_desde_juego

        cfg_elo = cargar_config()
        if cfg_elo.get("usar_elo", True):
            for juego in juegos:
                if not _juego_finalizado(juego):
                    continue
                if not _ganador_oficial(juego):
                    continue
                r = actualizar_elo_desde_juego(juego, cfg_elo)
                if r.get("ok") and not r.get("omitido"):
                    print(
                        f"[ELO] Actualizado {juego.get('visitante')}@{juego.get('home')}: "
                        f"{r.get('ganador')} away {r.get('away')} home {r.get('home')}"
                    )
    except Exception as e:
        print(f"[ELO] aviso liquidación: {e}")

    for apuesta in dia.get("apuestas", []):
        juego = por_id.get(str(apuesta.get("game_id") or ""))
        if not juego:
            continue
        if apuesta.get("estado") == "pendiente" or apuesta.get("estado") in ("ganada", "perdida"):
            if liquidar_apuesta(apuesta, juego, apuesta["stake"]):
                cambios += 1

    try:
        if _cruzar_momios_espn(dia):
            cambios += 1
    except Exception as e:
        print(f"[CRUCE] ESPN no bloquea la liquidación: {e}")
    try:
        revisar_apuestas_colgadas(memoria, log=True)
    except Exception as e:
        print(f"[ALERTA] {e}")
    
    # Liquidar también predicciones no apostadas (y corregir si se liquidaron mal)
    if "predicciones" in dia:
        for prediccion in dia["predicciones"]:
            if prediccion.get("estado") not in ("pendiente", "liquidado"):
                continue
            juego = por_id.get(str(prediccion.get("game_id") or ""))
            if not juego:
                continue

            if prediccion.get("estado") == "liquidado" and juego.get("estado") in (
                "EN VIVO", "PROGRAMADO", "POSPUESTO", "CANCELADO", "SUSPENDIDO", "SUSPENDIDO_OFICIAL"
            ):
                prediccion["estado"] = "pendiente"
                prediccion["resultado"] = None
                prediccion.pop("marcador_final", None)
                prediccion.pop("liquidado_en", None)
                cambios += 1
                print(f"[PREDICCIÓN] Revertida liquidación prematura {prediccion['pick']}")
                continue

            if not _juego_finalizado(juego):
                continue

            ganador = _ganador_oficial(juego)
            if not ganador:
                continue

            pick_norm = norm_nombre(nombre_equipo_en_pick(prediccion["pick"]))
            resultado = "acierto" if pick_norm == ganador else "fallo"
            marcador = (
                f"{juego['visitante']} {juego.get('scoreAway')} - "
                f"{juego['home']} {juego.get('scoreHome')}"
            )

            stake_v = float(
                prediccion.get("stake_virtual")
                or stake_virtual_prediccion(memoria)
            )
            odds = float(prediccion.get("odds") or 0)
            if odds <= 1.0:
                odds, amer = cuota_desde_prob(float(prediccion.get("probPick") or 50))
                prediccion["odds"] = odds
                prediccion["odds_american"] = amer
            if resultado == "acierto":
                profit_v = round(stake_v * (odds - 1), 2)
            else:
                profit_v = round(-stake_v, 2)

            if (
                prediccion.get("estado") == "liquidado"
                and prediccion.get("resultado") == resultado
                and prediccion.get("marcador_final") == marcador
                and prediccion.get("profit") == profit_v
            ):
                continue

            prediccion["estado"] = "liquidado"
            prediccion["resultado"] = resultado
            prediccion["marcador_final"] = marcador
            prediccion["stake_virtual"] = stake_v
            prediccion["profit"] = profit_v
            prediccion["liquidado_en"] = datetime.now(tz_experimento()).isoformat()
            # Marcar si ese juego también tuvo apuesta con dinero
            if any(str(a.get("game_id")) == str(prediccion.get("game_id")) for a in apuestas):
                prediccion["con_dinero"] = True
            cambios += 1
            print(
                f"[PREDICCIÓN] {prediccion['pick']} -> {resultado.upper()} "
                f"({marcador}) P/L papel {profit_v:+.2f}"
            )
            try:
                from ia_lecciones import registrar_experiencias_tras_liquidar

                registrar_experiencias_tras_liquidar(
                    memoria,
                    prediccion,
                    cfg=cargar_config(),
                    juego=juego,
                    cuando=dia.get("fecha"),
                )
            except Exception as e:
                print(f"[LECCIONES] aviso: {e}")
            if prediccion.get("resultado") == "fallo":
                try:
                    from mente_skills import reflexionar_fallo

                    reflexionar_fallo(memoria, prediccion)
                except Exception as e:
                    print(f"[SKILLS] aviso: {e}")
    
    if cambios:
        print(f"[DEBUG LIQ DIA] Se realizaron {cambios} cambios para el día {dia['fecha']}. Recalculando y guardando.")
        recalcular_capital(memoria)
        actualizar_resumen(memoria)
        global _ultimo_ml_train_ts
        ahora_ml = time.monotonic()
        if ahora_ml - _ultimo_ml_train_ts >= (
            6 * 3600.0 if _en_render() else _ML_TRAIN_MIN_INTERVAL_SEC
        ):
            auto_entrenar_ml(memoria)
            try:
                from calibracion import entrenar_calibrador

                memoria["calib_meta"] = entrenar_calibrador(memoria, min_muestras=30)
            except Exception as e:
                print(f"[CALIB] auto: {e}")
            _ultimo_ml_train_ts = ahora_ml
        else:
            print("[ML] Entrenamiento omitido (debounce 1h entre liquidaciones)")
        guardar_memoria(memoria)
    return cambios


def liquidar_todo(memoria: dict) -> int:
    """Revisa dias con pendientes o con predicciones/apuestas recientes."""
    total = 0
    hoy = ahora_simulado().date()
    for dia in memoria["dias"]:
        apuestas = dia.get("apuestas", [])
        preds = dia.get("predicciones", [])
        if not apuestas and not preds:
            continue
        hay_pendiente = any(a.get("estado") == "pendiente" for a in apuestas) or any(
            p.get("estado") == "pendiente" for p in preds
        )
        try:
            f_dia = datetime.strptime(dia["fecha"], "%Y-%m-%d").date()
            reciente = (hoy - f_dia).days <= 7
        except Exception:
            reciente = True
        if hay_pendiente or reciente:
            total += liquidar_dia(memoria, dia)
    return total


def sincronizar_experimento_a_hoy(memoria: dict | None = None) -> dict:
    """
    Alinea dia_actual y registros de días con la fecha real de Puerto Rico.
    Crea días vacíos para las fechas saltadas (sin inventar apuestas).
    """
    memoria = memoria if memoria is not None else cargar_memoria()
    if not memoria.get("experimento_activo") or not memoria.get("dias"):
        return memoria

    f_inicio = fecha_inicio_experimento(memoria)
    if not f_inicio:
        return memoria

    hoy = hoy_local()
    dias_totales = int(memoria.get("dias_totales") or 200)
    dia_objetivo = min(numero_dia_para_fecha(memoria, hoy), dias_totales)
    fecha_objetivo = f_inicio + timedelta(days=dia_objetivo - 1)

    # Rellenar huecos desde el día 1 hasta hoy
    hubo = False
    for n in range(1, dia_objetivo + 1):
        f = f_inicio + timedelta(days=n - 1)
        antes = len(memoria["dias"])
        asegurar_dia_operativo(memoria, f.strftime("%Y-%m-%d"))
        if len(memoria["dias"]) != antes:
            hubo = True

    if memoria.get("dia_actual") != dia_objetivo:
        print(
            f"[SISTEMA] Sincronizando experimento: dia {memoria.get('dia_actual')} -> "
            f"{dia_objetivo} ({fecha_objetivo})"
        )
        memoria["dia_actual"] = dia_objetivo
        hubo = True

    if hubo:
        actualizar_resumen(memoria)
        guardar_memoria(memoria)
    return memoria


def avanzar_dia_automatico() -> None:
    """Sincroniza el puntero del experimento con el calendario real."""
    try:
        antes = cargar_memoria().get("dia_actual")
        memoria = sincronizar_experimento_a_hoy()
        if memoria.get("experimento_activo") and memoria.get("dia_actual") != antes:
            try:
                programar_bloqueos_por_juego()
            except Exception as e:
                print(f"[SISTEMA] Aviso al reprogramar bloqueos: {e}")
    except Exception as e:
        print(f"[SISTEMA] Error al sincronizar el día automáticamente: {e}")


def stake_virtual_prediccion(memoria: dict | None = None) -> float:
    """Unidad de P/L en papel para TODAS las predicciones (no mueve la banca)."""
    memoria = memoria if memoria is not None else cargar_memoria()
    cfg = cargar_config()
    return float(memoria.get("stake_por_juego") or cfg.get("stake_por_juego") or 5.0)


def reparar_odds_papel(memoria: dict | None = None, *, persistir: bool = True) -> int:
    """Corrige predicciones con cuota fija 1.5/+150 (default roto) usando cuota_desde_prob.

    También recalcula profit virtual si ya estaban liquidadas, y restaura
    stake_por_juego al valor de config si quedó pisado por Kelly.
    """
    memoria = memoria if memoria is not None else cargar_memoria()
    cfg = cargar_config()
    cambios = 0

    stake_cfg = float(cfg.get("stake_por_juego") or 5.0)
    actual_stake = float(memoria.get("stake_por_juego") or stake_cfg)
    if abs(actual_stake - stake_cfg) > 0.01:
        memoria["stake_por_juego"] = stake_cfg
        cambios += 1

    for dia in memoria.get("dias", []):
        for pred in dia.get("predicciones", []):
            odds = float(pred.get("odds") or 0)
            amer = pred.get("odds_american")
            # Default histórico roto: decimal 1.5 + americano +150
            es_default_roto = abs(odds - 1.5) < 0.001 and (
                amer is None or int(amer) == 150
            )
            if not es_default_roto and odds > 1.0:
                continue
            prob = float(pred.get("probPick") or 50)
            nueva, amer_n = cuota_desde_prob(prob)
            if abs(nueva - odds) < 0.001 and amer is not None and int(amer) == int(amer_n):
                continue
            pred["odds"] = nueva
            pred["odds_american"] = amer_n
            if pred.get("estado") == "liquidado" and pred.get("resultado") in ("acierto", "fallo"):
                stake_v = float(pred.get("stake_virtual") or stake_virtual_prediccion(memoria))
                if pred["resultado"] == "acierto":
                    pred["profit"] = round(stake_v * (nueva - 1), 2)
                else:
                    pred["profit"] = round(-stake_v, 2)
            cambios += 1

    if cambios:
        actualizar_resumen(memoria)
        if persistir:
            guardar_memoria(memoria)
        print(f"[REPARAR] Corregidas {cambios} cuota(s)/stake de predicciones en papel.")
    return cambios


def omitir_congelar_papel(juego: dict, cfg: dict | None = None) -> tuple[bool, str]:
    """No congelar en papel un favorito inflado: el % alto sin edge extra
    ensucia el historial y no es candidato de dinero."""
    cfg = cfg or {}
    estr = cfg.get("estrategia") or {}
    if not bool(estr.get("papel_respeta_favorito_inflado", False)):
        return False, ""
    bloqueado, motivo = bloqueado_favorito_inflado(juego, cfg)
    if bloqueado:
        return True, motivo
    return False, ""


def guardar_prediccion(
    dia: dict,
    juego: dict,
    *,
    con_dinero: bool = False,
    stake_virtual: float | None = None,
    permitir_gracia: bool = False,
) -> bool:
    """Guarda/actualiza predicción de un juego. No mueve capital.

    permitir_gracia se conserva por compatibilidad y no abre congelado
    después del primer lanzamiento.
    """
    del permitir_gracia
    cfg = cargar_config()
    pick = (juego.get("pick") or "").strip()
    if not pick:
        return False
    if "predicciones" not in dia:
        dia["predicciones"] = []

    if not con_dinero:
        omitir, motivo_omit = omitir_congelar_papel(juego, cfg)
        if omitir:
            existente_prev = next(
                (p for p in dia["predicciones"] if str(p.get("game_id")) == str(juego.get("id"))),
                None,
            )
            if existente_prev is None:
                print(
                    f"[PREDICCIONES] No se congela favorito inflado "
                    f"({juego.get('visitante')}@{juego.get('home')}): {motivo_omit}"
                )
                return False

    stake_v = float(stake_virtual if stake_virtual is not None else stake_virtual_prediccion())
    ahora_dt = datetime.now(tz_experimento())
    ahora = ahora_dt.isoformat()
    existente = next(
        (p for p in dia["predicciones"] if str(p.get("game_id")) == str(juego["id"])),
        None,
    )
    if existente:
        actualizar_mercado_en_prediccion(existente, juego, cfg)
        # No cambiar pick ya congelado; solo marcar si hubo dinero
        if con_dinero:
            existente["con_dinero"] = True
        if existente.get("stake_virtual") is None:
            existente["stake_virtual"] = stake_v
        # Backfill features reales si el pick se congeló antes del fix
        if not existente.get("ml_features") and isinstance(juego.get("ml_features"), dict):
            existente["ml_features"] = juego["ml_features"]
        # Briefing T-60 interno si faltaba (no visible en panel)
        if not isinstance(existente.get("ia_briefing"), dict) or not existente["ia_briefing"].get("ok"):
            try:
                if isinstance(juego.get("ia_briefing"), dict) and juego["ia_briefing"].get("ok"):
                    existente["ia_briefing"] = juego["ia_briefing"]
                else:
                    existente["ia_briefing"] = generar_briefing_juego(
                        juego, cargar_memoria(), fase="t60"
                    )
            except Exception as e:
                print(f"[BRIEFING] backfill: {e}")
        return False

    # No inventar pick a posteriori cuando el partido ya terminó.
    estado = juego.get("estado")
    if estado in ("FINALIZADO", "POSPUESTO"):
        print(
            f"[PREDICCIONES] No se congela pick nuevo en estado {estado} "
            f"({juego.get('visitante')}@{juego.get('home')})"
        )
        return False

    inicio = _parse_iso_dt(juego.get("inicio_juego"))
    mins_despues = (
        (ahora_dt - inicio).total_seconds() / 60.0 if inicio else None
    )

    # Integridad: un pick nuevo solo existe antes del primer lanzamiento.
    # permitir_gracia se ignora: un partido empezado o final no entra en el conteo.
    if estado in ("EN VIVO", "FINALIZADO", "POSPUESTO"):
        print(
            f"[PREDICCIONES] No se congela pick nuevo en estado {estado} "
            f"({juego.get('visitante')}@{juego.get('home')})"
        )
        return False
    if mins_despues is not None and mins_despues >= 0:
        print(
            f"[PREDICCIONES] No se congela tras el primer pitch "
            f"({mins_despues:.0f} min) "
            f"({juego.get('visitante')}@{juego.get('home')})"
        )
        return False

    prob = float(juego.get("probPick") or 50)
    odds = juego.get("odds")
    odds_amer = juego.get("odds_american")
    if not odds or float(odds) <= 1.0:
        odds, odds_amer = cuota_desde_prob(prob)

    apostable_flag = apostable_para_dinero(juego)

    # Briefing T-60 interno (para la mente). No se muestra en el panel.
    briefing = None
    try:
        mem_tmp = cargar_memoria()
        if not isinstance(juego.get("ia_briefing"), dict) or not juego["ia_briefing"].get("ok"):
            briefing = generar_briefing_juego(juego, mem_tmp, fase="t60")
        else:
            briefing = juego.get("ia_briefing")
    except Exception as e:
        print(f"[BRIEFING] aviso T-60: {e}")

    motivo = juego.get("motivo_apuesta") or ""
    etiqueta_ventana = None
    if mins_despues is not None and mins_despues < 0:
        abierta = ventana_congelacion_abierta(-mins_despues, cfg)
        if abierta is not None:
            etiqueta_ventana = f"T-{abierta}"

    dia["predicciones"].append(
        {
            "game_id": str(juego["id"]),
            "visitante": juego["visitante"],
            "home": juego["home"],
            "pick": juego["pick"],
            "odds": float(odds),
            "odds_american": odds_amer,
            "edge": 0 if not tiene_cuota_mercado(juego) else juego.get("edge", 0),
            "probPick": prob,
            "prob_sin_calibrar": juego.get("prob_sin_calibrar"),
            "filtro_valor": juego.get("filtro_valor") if isinstance(juego.get("filtro_valor"), dict) else None,
            "filtro_tipo": juego.get("filtro_tipo") if isinstance(juego.get("filtro_tipo"), dict) else None,
            "apostable": apostable_flag,
            "lineas_fuente": juego.get("lineas_fuente") or "modelo",
            "fuente_momio": juego.get("fuente_momio"),
            "casa_momio": juego.get("casa_momio"),
            "paso_momio": juego.get("paso_momio"),
            "origen_momio": juego.get("origen_momio"),
            "estado_registro": juego.get("estado_registro"),
            "sin_momio_real": bool(juego.get("sin_momio_real")),
            "momio_stale": bool(juego.get("momio_stale")),
            "momio_fallos": juego.get("momio_fallos"),
            "odds_estimado_american": juego.get("odds_estimado_american"),
            "motivo_apuesta": motivo,
            "pitcherAway": juego.get("pitcherAway"),
            "pitcherHome": juego.get("pitcherHome"),
            "pitcher_away_id": juego.get("pitcher_away_id"),
            "pitcher_home_id": juego.get("pitcher_home_id"),
            "inicio_juego": juego.get("inicio_juego"),
            "estado": "pendiente",
            "resultado": None,
            "profit": None,
            "stake_virtual": stake_v,
            "con_dinero": bool(con_dinero),
            "predicho_en": ahora,
            "congelado_en_gracia": False,
            "ventana_congelacion": etiqueta_ventana,
            "valida_stats": True,
            "invalida_tarde": False,
            "confianza_baja": False,
            "clima": juego.get("clima") if isinstance(juego.get("clima"), dict) else None,
            "lesiones": juego.get("lesiones") if isinstance(juego.get("lesiones"), dict) else None,
            "scratch_lineup": juego.get("scratch_lineup") if isinstance(juego.get("scratch_lineup"), dict) else None,
            "factores_humanos": juego.get("factores_humanos")
            if isinstance(juego.get("factores_humanos"), dict)
            else None,
            "historico_oficial": juego.get("historico_oficial")
            if isinstance(juego.get("historico_oficial"), dict)
            else None,
            "ia_briefing": briefing if isinstance(briefing, dict) else None,
            "ia_mente": juego.get("ia_mente") if isinstance(juego.get("ia_mente"), dict) else None,
            "ml_features": juego.get("ml_features") if isinstance(juego.get("ml_features"), dict) else None,
            "tipo_pick": juego.get("tipo_pick"),
            "inteligencia": juego.get("inteligencia")
            if isinstance(juego.get("inteligencia"), dict)
            else None,
            "elo": juego.get("elo") if isinstance(juego.get("elo"), dict) else None,
            "mc_totales": juego.get("mc_totales")
            if isinstance(juego.get("mc_totales"), dict)
            else None,
            "preferir_f5": bool(juego.get("preferir_f5")),
            "total_linea": juego.get("total_linea"),
            "lineas_total": juego.get("lineas_total")
            if isinstance(juego.get("lineas_total"), dict)
            else None,
            "odds_congelada": float(odds),
            "lineas_fuente_inicial": juego.get("lineas_fuente") or "modelo",
        }
    )
    try:
        pred_n = dia["predicciones"][-1]
        actualizar_clv_registro(pred_n, juego, fase="entrada")
    except Exception as e:
        print(f"[CLV] aviso guardar predicción: {e}")
    return True


def registrar_predicciones_del_dia(forzar: bool = False) -> dict:
    """
    Congela el pick en papel si ya abrió T-90/T-60/T-30/T-10 y el partido
    no ha empezado. Idempotente: un pick ya fijo no se reescribe.

    forzar no salta el primer pitch ni congela un juego que todavía está
    fuera de la ventana más temprana (eso fijaría un precio demasiado viejo).
    FINAL / EN VIVO: no se inventa pick. Se anota la ventana perdida.
    """
    memoria = cargar_memoria()
    hoy = fecha_str()
    ahora = ahora_simulado()
    dia = asegurar_dia_operativo(memoria, hoy)
    juegos = obtener_juegos_fecha(hoy)
    stake_v = stake_virtual_prediccion(memoria)
    ya = {str(p.get("game_id")) for p in dia.get("predicciones", [])}
    nuevas = 0
    omitidas_vivo = 0
    cfg = cargar_config()

    for juego in juegos:
        estado = juego.get("estado")
        gid = str(juego.get("id") or "")
        if not (juego.get("pick") or "").strip():
            continue
        if gid in ya and not forzar:
            continue
        se_puede, etiqueta = juego_se_puede_congelar(juego, cfg, ahora)
        if not se_puede:
            if gid not in ya and estado in ("EN VIVO", "FINALIZADO"):
                omitidas_vivo += 1
            elif gid not in ya and estado == "PROGRAMADO":
                mins = minutos_hasta_inicio(juego, ahora)
                if mins is not None and mins <= 0:
                    omitidas_vivo += 1
            continue
        if not guardar_prediccion(
            dia,
            juego,
            con_dinero=False,
            stake_virtual=stake_v,
            permitir_gracia=False,
        ):
            continue
        pred = next(
            (p for p in dia["predicciones"] if str(p.get("game_id")) == gid),
            None,
        )
        if pred is None:
            print(f"[REGISTRO] Predicción no encontrada tras guardar game_id={gid}")
            continue
        pred["valida_stats"] = True
        pred["invalida_tarde"] = False
        pred["congelado_en_gracia"] = False
        pred["ventana_congelacion"] = etiqueta
        try:
            anotar_congelacion_recuperada(juego, etiqueta, cfg, ahora)
        except Exception as e:
            print(f"[CONGELAR] aviso recuperación: {e}")
        try:
            if cfg.get("usar_mente", True) and not isinstance(pred.get("ia_mente"), dict):
                mente_t60 = mente_conclusion(
                    juego, cfg, memoria, forzar=True, solo_local=True
                )
                pred["ia_mente"] = mente_t60
                juego["ia_mente"] = mente_t60
        except Exception as e:
            print(f"[MENTE] aviso congelación: {e}")
        print(
            f"[CONGELAR] pick congelado game_id={gid} ventana={etiqueta} "
            f"{juego.get('visitante')} @ {juego.get('home')}"
        )
        nuevas += 1
        ya.add(gid)

    if nuevas:
        guardar_memoria(memoria)
    if omitidas_vivo:
        print(f"[PREDICCIONES] Omitidos {omitidas_vivo} partidos ya empezados (no se congelan).")
    congelacion: dict = {}
    try:
        congelacion = anotar_ventanas_perdidas(juegos, ya, cfg, ahora)
    except Exception as e:
        print(f"[CONGELAR] aviso ventanas perdidas: {e}")
    return {
        "ok": True,
        "predicciones_nuevas": nuevas,
        "omitidas_en_vivo": omitidas_vivo,
        "fecha": hoy,
        "congelacion": congelacion,
    }


def vigilancia_t60(
    juegos: list[dict],
    memoria: dict | None = None,
    cfg: dict | None = None,
) -> dict:
    """
    Detecta juegos PROGRAMADOS cerca del T-60 / inicio sin pick congelado.
    También lista FINALIZADOS del día sin predicción (Render dormido).
    """
    cfg = cfg or {}
    memoria = memoria or {}
    horizonte = float(horizonte_congelacion_min(cfg))

    fecha = fecha_str()
    dia = dia_por_fecha(memoria, fecha) if memoria else None
    if not dia and memoria:
        try:
            dia = dia_operativo(memoria)
        except Exception:
            dia = None
    ya = {
        str(p.get("game_id"))
        for p in ((dia or {}).get("predicciones") or [])
        if (p.get("pick") or "").strip()
    }

    ahora = ahora_simulado()
    en_riesgo: list[dict] = []
    perdidos: list[dict] = []
    congelados = 0
    programados = 0

    for j in juegos or []:
        estado = str(j.get("estado") or "")
        gid = str(j.get("id") or "")
        if estado == "PROGRAMADO":
            programados += 1
        if gid in ya:
            if estado in ("PROGRAMADO", "EN VIVO"):
                congelados += 1
            continue

        mins_a_inicio = None
        raw_ini = j.get("inicio_juego")
        try:
            if raw_ini:
                ini = datetime.fromisoformat(str(raw_ini))
                if ini.tzinfo is None:
                    ini = ini.replace(tzinfo=tz_experimento())
                mins_a_inicio = (ini - ahora).total_seconds() / 60.0
        except Exception:
            mins_a_inicio = None

        # Ya terminó o ya empezó y nunca hubo pick → perdido (no se inventa).
        if estado == "FINALIZADO":
            perdidos.append(
                {
                    "id": gid,
                    "visitante": j.get("visitante"),
                    "home": j.get("home"),
                    "estado": estado,
                    "hora_inicio_txt": j.get("hora_inicio_txt"),
                    "mins_a_inicio": round(mins_a_inicio, 1) if mins_a_inicio is not None else None,
                    "motivo": "FINAL sin predicción (posible Render dormido / T-60 perdido)",
                }
            )
            continue

        if estado == "EN VIVO" or (
            estado == "PROGRAMADO" and mins_a_inicio is not None and mins_a_inicio <= 0
        ):
            perdidos.append(
                {
                    "id": gid,
                    "visitante": j.get("visitante"),
                    "home": j.get("home"),
                    "estado": estado,
                    "hora_inicio_txt": j.get("hora_inicio_txt"),
                    "mins_a_inicio": round(mins_a_inicio, 1) if mins_a_inicio is not None else None,
                    "motivo": "Primer pitch sin pick congelado (no se congela a posteriori)",
                }
            )
            continue

        if estado != "PROGRAMADO":
            continue

        # Antes se exigía pick en el objeto juego: si el motor no corrió, no alertaba.
        riesgo = False
        motivo = ""
        if mins_a_inicio is not None and 0 < mins_a_inicio <= horizonte:
            riesgo = True
            abierta = ventana_congelacion_abierta(mins_a_inicio, cfg)
            etiqueta = f"T-{abierta}" if abierta else f"T-{int(horizonte)}"
            motivo = (
                f"{etiqueta} abierta · faltan {mins_a_inicio:.0f} min al inicio · sin congelar"
            )

        if riesgo:
            en_riesgo.append(
                {
                    "id": gid,
                    "visitante": j.get("visitante"),
                    "home": j.get("home"),
                    "pick": j.get("pick"),
                    "estado": estado,
                    "hora_inicio_txt": j.get("hora_inicio_txt"),
                    "mins_a_inicio": round(mins_a_inicio, 1) if mins_a_inicio is not None else None,
                    "motivo": motivo,
                }
            )

    en_riesgo.sort(key=lambda x: (x.get("mins_a_inicio") is None, x.get("mins_a_inicio") or 0))
    n = len(en_riesgo)
    n_perd = len(perdidos)
    if n > 0:
        nivel = "alerta"
        if n == 1:
            g0 = en_riesgo[0]
            mensaje = (
                f"⚠ Sin pick fijo: {g0.get('visitante')} @ {g0.get('home')} "
                f"· {g0.get('motivo')}"
            )
        else:
            mensaje = f"⚠ {n} juegos sin pick congelado cerca del T-60 / inicio"
    elif n_perd > 0:
        nivel = "alerta"
        p0 = perdidos[0]
        if n_perd == 1:
            mensaje = (
                f"⚠ Pick perdido: {p0.get('visitante')} @ {p0.get('home')} "
                f"(sin predicción · posible sueño Render)"
            )
        else:
            mensaje = f"⚠ {n_perd} juegos del día sin predicción (posible sueño Render)"
    else:
        mensaje = "Vigilancia T-60 OK · sin juegos en riesgo ahora"
        nivel = "ok"

    try:
        anotar_ventanas_perdidas(juegos or [], ya, cfg, ahora)
    except Exception as e:
        print(f"[CONGELAR] vigilancia: {e}")

    return {
        "ok": n == 0 and n_perd == 0,
        "nivel": nivel,
        "mensaje": mensaje,
        "en_riesgo": en_riesgo[:8],
        "total_riesgo": n,
        "perdidos": perdidos[:12],
        "total_perdidos": n_perd,
        "congelados_activos": congelados,
        "programados": programados,
        "cron_cada_min": 5,
        "accion_sugerida": (
            "forzar_registro_t60" if n > 0 else ("cron_externo" if n_perd > 0 else None)
        ),
    }


def _resumen_mente_errores(cfg: dict | None = None) -> dict:
    try:
        return resumen_mente_errores_panel(cfg or cargar_config())
    except Exception as e:
        return {
            "activo": False,
            "nivel": "aviso",
            "mensaje": f"Mente errores no disponible: {e}"[:120],
            "overrides": {},
            "incidentes_recientes": [],
        }


def rellenar_predicciones_fecha(memoria: dict, fecha: str) -> int:
    """
    Ya NO inventa picks a posteriori.

    Antes rellenaba días pasados con el modelo actual + resultado ya conocido,
    lo que fabricaba "8✓/7✗" falsos (ej. día 25 rellenado el 2 ago a las 19:26).
    Esos picks contaminaban el historial del panel.
    """
    return 0


def rellenar_predicciones_recientes(memoria: dict, dias_atras: int = 7) -> int:
    """Rellena predicciones faltantes de dias ANTERIORES (no hoy).

    Hoy se registra con registrar_predicciones_del_dia (respeta T-60).
    Rellenar hoy congelaría picks demasiado temprano.
    """
    hoy = hoy_local()
    f_inicio = fecha_inicio_experimento(memoria)
    if not f_inicio:
        return 0

    total = 0
    # offset 1..N: solo días pasados
    for offset in range(1, dias_atras + 1):
        f = hoy - timedelta(days=offset)
        if f < f_inicio:
            continue
        total += rellenar_predicciones_fecha(memoria, f.strftime("%Y-%m-%d"))

    if total:
        guardar_memoria(memoria)
        print(f"[PREDICCIONES] Rellenadas {total} prediccion(es) de dias anteriores.")
    return total


def resumen_predicciones_y_dinero(memoria: dict) -> dict:
    """Totales separados: predicciones SOLO PAPEL vs apuestas con dinero + divergencia."""
    pred_aciertos = pred_fallos = 0
    pred_ganado = pred_perdido = 0.0
    pred_excluidas = 0
    pred_stake_total = 0.0
    din_ganadas = din_perdidas = 0
    din_ganado = din_perdido = 0.0
    din_stake_total = 0.0
    est_ganadas = est_perdidas = 0
    est_ganado = est_perdido = 0.0
    est_stake_total = 0.0
    mutado = False
    stake_v_default = float(stake_virtual_prediccion(memoria))

    for dia in memoria.get("dias", []):
        apostados = {
            str(a.get("game_id"))
            for a in dia.get("apuestas", [])
            if a.get("estado") in ("ganada", "perdida", "pendiente")
        }
        for p in dia.get("predicciones", []):
            if p.get("estado") != "liquidado":
                continue
            if str(p.get("game_id") or "") in apostados or p.get("con_dinero"):
                continue
            profit = p.get("profit")
            if profit is None and p.get("resultado") in ("acierto", "fallo"):
                stake_v = float(
                    p.get("stake_virtual") or stake_v_default
                )
                odds = float(p.get("odds") or 0)
                if odds <= 1.0:
                    odds, amer = cuota_desde_prob(float(p.get("probPick") or 50))
                    p["odds"] = odds
                    p["odds_american"] = amer
                profit = (
                    round(stake_v * (odds - 1), 2)
                    if p["resultado"] == "acierto"
                    else round(-stake_v, 2)
                )
                p["profit"] = profit
                p["stake_virtual"] = stake_v
                mutado = True
            profit = float(profit or 0)
            stake_v = float(p.get("stake_virtual") or stake_v_default)
            if not prediccion_valida_para_stats(p):
                pred_excluidas += 1
                continue
            pred_stake_total += stake_v
            if p.get("resultado") == "acierto":
                pred_aciertos += 1
                if profit > 0:
                    pred_ganado += profit
            elif p.get("resultado") == "fallo":
                pred_fallos += 1
                if profit < 0:
                    pred_perdido += abs(profit)

        for a in dia.get("apuestas", []):
            if a.get("estado") not in ("ganada", "perdida"):
                continue
            profit = float(a.get("profit") or 0)
            stake_a = float(a.get("stake") or 0)
            bucket_est = es_momio_estimado(a)
            if bucket_est:
                est_stake_total += stake_a
                if a["estado"] == "ganada":
                    est_ganadas += 1
                    est_ganado += max(profit, 0)
                else:
                    est_perdidas += 1
                    est_perdido += abs(min(profit, 0))
                continue
            din_stake_total += stake_a
            if a["estado"] == "ganada":
                din_ganadas += 1
                din_ganado += max(profit, 0)
            else:
                din_perdidas += 1
                din_perdido += abs(min(profit, 0))

    pred_total = pred_aciertos + pred_fallos
    din_total = din_ganadas + din_perdidas
    est_total = est_ganadas + est_perdidas
    pred_wr = round(100 * pred_aciertos / pred_total, 1) if pred_total else 0
    din_wr = round(100 * din_ganadas / din_total, 1) if din_total else 0
    est_wr = round(100 * est_ganadas / est_total, 1) if est_total else 0
    pred_neto = round(pred_ganado - pred_perdido, 2)
    din_neto = round(din_ganado - din_perdido, 2)
    est_neto = round(est_ganado - est_perdido, 2)
    pred_roi = round(100 * pred_neto / pred_stake_total, 1) if pred_stake_total else 0
    din_roi = round(100 * din_neto / din_stake_total, 1) if din_stake_total else 0
    est_roi = round(100 * est_neto / est_stake_total, 1) if est_stake_total else 0
    wr_diff = round(pred_wr - din_wr, 1) if pred_total and din_total else None

    divergencia = {"wr_diff": wr_diff, "alerta": False, "mensaje": ""}
    if wr_diff is not None and wr_diff >= 10:
        divergencia["alerta"] = True
        divergencia["mensaje"] = (
            f"Papel {pred_wr}% vs dinero {din_wr}% — el bias usa solo dinero real"
        )
    elif wr_diff is not None and pred_total >= 20 and din_total >= 3:
        divergencia["mensaje"] = f"Papel {pred_wr}% · Dinero {din_wr}%"

    return {
        "predicciones": {
            "total": pred_total,
            "aciertos": pred_aciertos,
            "fallos": pred_fallos,
            "win_rate": pred_wr,
            "ganado": round(pred_ganado, 2),
            "perdido": round(pred_perdido, 2),
            "neto": pred_neto,
            "roi_pct": pred_roi,
            "invertido": round(pred_stake_total, 2),
            "excluidas_tarde": pred_excluidas,
        },
        "dinero": {
            "total": din_total,
            "ganadas": din_ganadas,
            "perdidas": din_perdidas,
            "win_rate": din_wr,
            "ganado": round(din_ganado, 2),
            "perdido": round(din_perdido, 2),
            "neto": din_neto,
            "roi_pct": din_roi,
            "invertido": round(din_stake_total, 2),
        },
        "dinero_estimado": {
            "total": est_total,
            "ganadas": est_ganadas,
            "perdidas": est_perdidas,
            "win_rate": est_wr,
            "ganado": round(est_ganado, 2),
            "perdido": round(est_perdido, 2),
            "neto": est_neto,
            "roi_pct": est_roi,
            "invertido": round(est_stake_total, 2),
        },
        "divergencia": divergencia,
        "_mutado": mutado,
    }


def _minutos_desde_inicio(juego: dict) -> float | None:
    """Minutos desde el inicio programado; None si no se puede calcular."""
    raw = juego.get("inicio_juego")
    if not raw:
        return None
    try:
        inicio = datetime.fromisoformat(raw)
        if inicio.tzinfo is None:
            inicio = inicio.replace(tzinfo=tz_experimento())
        return (ahora_simulado() - inicio).total_seconds() / 60.0
    except Exception:
        return None


def _permite_bloqueo_dinero(juego: dict, *, forzar: bool = False) -> tuple[bool, str]:
    """Dinero solo en PROGRAMADO y antes del primer lanzamiento."""
    del forzar
    estado = juego.get("estado")
    mins = _minutos_desde_inicio(juego)
    if mins is not None and mins >= 0:
        return False, "Primer pitch ya pasó; no se apuesta a un partido empezado."
    if estado == "PROGRAMADO":
        return True, ""
    return False, f"El juego ya está {estado}; solo se apuesta antes del primer pitch."


def bloquear_juego(game_id: str, forzar: bool = False) -> dict:
    """1h antes: siempre registra predicción; si es apostable, también apuesta con dinero."""
    cfg = cargar_config()
    estr = cfg.get("estrategia", {})
    max_dia = int(estr.get("max_apuestas_dia", 5))
    hoy = fecha_str()

    print(f"[DEBUG BLOQUEO] Intentando bloquear juego {game_id} para el día {hoy}. Forzar: {forzar}")

    # Red fuera del lock
    juegos = obtener_juegos_fecha(hoy)
    juego = next((j for j in juegos if str(j["id"]) == str(game_id)), None)
    if not juego:
        print(f"[DEBUG BLOQUEO] Juego {game_id} no encontrado en la API para el día {hoy}.")
        return {"ok": False, "motivo": "Juego no encontrado en el calendario."}

    ok_estado, motivo_estado = _permite_bloqueo_dinero(juego, forzar=forzar)
    if not ok_estado:
        print(f"[DEBUG BLOQUEO] Juego {game_id} no bloqueable ({juego['estado']}). {motivo_estado}")
        return {
            "ok": False,
            "motivo": motivo_estado,
        }

    with _memoria_lock:
        return _bloquear_juego_locked(game_id, juego, forzar=forzar, max_dia=max_dia, hoy=hoy)


def _marcar_sin_momio_real(juego: dict, pred: dict | None, precio: dict, prob: float) -> None:
    """El pick queda registrado. El estimado no mueve la banca."""
    fallos = str(precio.get("momio_fallos") or juego.get("momio_fallos") or "").strip()
    motivo = "Sin momio real, solo registrado"
    if fallos:
        motivo = f"{motivo} · {fallos}"
    juego["fuente_momio"] = "sin_momio_real"
    juego["lineas_fuente"] = "sin_momio_real"
    juego["casa_momio"] = "sin_momio_real"
    juego["paso_momio"] = "sin_casa"
    juego["origen_momio"] = "sin_momio_real"
    juego["estado_registro"] = "registrado sin apuesta"
    juego["sin_momio_real"] = True
    juego["apostable"] = False
    juego["edge"] = 0
    juego["momio_fallos"] = fallos
    juego["motivo_apuesta"] = motivo
    juego["probPick"] = prob
    if precio.get("odds_american") is not None:
        juego["odds_estimado_american"] = precio.get("odds_american")
        juego["odds_american"] = precio.get("odds_american")
        juego["odds"] = precio.get("odds")
    if pred is None:
        return
    pred["fuente_momio"] = "sin_momio_real"
    pred["lineas_fuente"] = "sin_momio_real"
    pred["estado_registro"] = "registrado sin apuesta"
    pred["sin_momio_real"] = True
    pred["apostable"] = False
    pred["edge"] = 0
    pred["momio_fallos"] = fallos
    pred["motivo_apuesta"] = motivo
    pred["probPick"] = prob
    if precio.get("odds_american") is not None:
        pred["odds_estimado_american"] = precio.get("odds_american")


def _bloquear_juego_locked(
    game_id: str,
    juego: dict,
    *,
    forzar: bool,
    max_dia: int,
    hoy: str,
) -> dict:
    memoria = cargar_memoria()
    cfg = cargar_config()

    if not memoria.get("experimento_activo", True):
        return {"ok": False, "motivo": "Experimento finalizado."}

    dia = asegurar_dia_operativo(memoria, hoy)
    gid = str(game_id)
    if any(str(a.get("game_id")) == gid for a in dia["apuestas"]):
        return {"ok": False, "motivo": "Este juego ya fue bloqueado."}

    if contar_apuestas_hoy(memoria, hoy) >= max_dia and not forzar:
        return {
            "ok": False,
            "motivo": f"Ya tienes {max_dia} apuestas hoy (máximo del día).",
        }

    stake_v = stake_virtual_prediccion(memoria)
    # Precio de la cadena en este instante, antes de que el pick congelado lo pise.
    momio_vivo = {
        "visitante": juego.get("visitante"),
        "home": juego.get("home"),
        "away_id": juego.get("away_id"),
        "home_id": juego.get("home_id"),
        "away_abbr": juego.get("away_abbr"),
        "home_abbr": juego.get("home_abbr"),
        "pick_lado": juego.get("pick_lado"),
        "pick_team_id": juego.get("pick_team_id"),
        "pick_abbr": juego.get("pick_abbr"),
        "odds_away_american": juego.get("odds_away_american"),
        "odds_home_american": juego.get("odds_home_american"),
        "odds_away_decimal": juego.get("odds_away_decimal"),
        "odds_home_decimal": juego.get("odds_home_decimal"),
        "fuente_momio": juego.get("fuente_momio"),
        "lineas_fuente": juego.get("lineas_fuente"),
        "casa_momio": juego.get("casa_momio"),
        "paso_momio": juego.get("paso_momio"),
        "origen_momio": juego.get("origen_momio"),
        "momio_fallos": juego.get("momio_fallos") or "",
        "momio_intentos": list(juego.get("momio_intentos") or [])
        if isinstance(juego.get("momio_intentos"), list)
        else [],
        "momio_fetched_at": juego.get("momio_fetched_at"),
        "momio_stale": juego.get("momio_stale"),
        "momio_en_vivo": juego.get("momio_en_vivo"),
        "estado_cuota": juego.get("estado_cuota"),
        "estado": juego.get("estado"),
        "inicio_juego": juego.get("inicio_juego"),
        "fecha": juego.get("fecha"),
        "id": juego.get("id"),
        "espn_id": juego.get("espn_id"),
    }
    # #124: el pick de papel solo se crea si el partido sigue sin empezar.
    if juego.get("estado") == "PROGRAMADO":
        guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=stake_v)

    # Si ya había predicción congelada, la apuesta con dinero debe usar ESE pick
    pred_existente = next(
        (p for p in dia.get("predicciones", []) if str(p.get("game_id")) == gid),
        None,
    )
    if pred_existente and (pred_existente.get("pick") or "").strip():
        actualizar_mercado_en_prediccion(pred_existente, juego, cfg)
        juego["pick"] = pred_existente["pick"]
        if pred_existente.get("odds"):
            juego["odds"] = pred_existente["odds"]
        if pred_existente.get("odds_american") is not None:
            juego["odds_american"] = pred_existente["odds_american"]
        if pred_existente.get("probPick") is not None:
            juego["probPick"] = pred_existente["probPick"]
        if pred_existente.get("edge") is not None:
            juego["edge"] = pred_existente["edge"]
        if pred_existente.get("motivo_apuesta"):
            juego["motivo_apuesta"] = pred_existente["motivo_apuesta"]
        if pred_existente.get("lineas_fuente"):
            juego["lineas_fuente"] = pred_existente["lineas_fuente"]
        if pred_existente.get("odds_away_decimal"):
            juego["odds_away_decimal"] = pred_existente["odds_away_decimal"]
        if pred_existente.get("odds_home_decimal"):
            juego["odds_home_decimal"] = pred_existente["odds_home_decimal"]
        # Cuota real o momio estimado. Un % alto sin ninguno de los dos no basta.
        if apostable_para_dinero(pred_existente) or apostable_para_dinero(juego):
            juego["apostable"] = True
        else:
            juego["apostable"] = False
            juego["edge"] = 0
            if pred_existente.get("apostable"):
                pred_existente["apostable"] = False

        if pred_existente.get("linea_movimiento_pct") is not None:
            juego["linea_movimiento_pct"] = pred_existente["linea_movimiento_pct"]
        bloqueado_le, motivo_le = bloqueado_linea_en_contra(
            {**juego, **pred_existente},
            cfg,
        )
        if bloqueado_le:
            juego["apostable"] = False
            pred_existente["apostable"] = False
            pred_existente["motivo_apuesta"] = motivo_le
            juego["motivo_apuesta"] = motivo_le

    # Sombra de valor solo anota. El tipo puede devolver un scratch o cortar un underdog.
    if not cfg.get("modo_solo_modelo") and (cfg.get("estrategia") or {}).get("requiere_betmgm", True):
        _anotar_filtro_valor_bloqueo(juego, pred_existente, cfg)
    _aplicar_tipo_sobre(juego, cfg, pred_existente)

    if not juego.get("apostable"):
        print(f"[DEBUG BLOQUEO] Juego {game_id} no apostable. Motivo: {juego.get('motivo_apuesta', 'Desconocido')}")
        guardar_memoria(memoria)
        return {
            "ok": False,
            "motivo": juego.get("motivo_apuesta", "Sin valor vs BetMGM ahora."),
            "juego": juego["visitante"] + " vs " + juego["home"],
            "prediccion_guardada": True,
        }

    # Refrescar lesiones justo antes del veto (aunque el pick esté congelado)
    if cfg.get("usar_lesiones", True) and not (
        isinstance(juego.get("lesiones"), dict) and juego["lesiones"].get("ok")
    ):
        try:
            from lesiones import analizar_lesiones_juego

            juego["lesiones"] = analizar_lesiones_juego(
                juego.get("visitante") or "",
                juego.get("home") or "",
                juego.get("pitcherAway"),
                juego.get("pitcherHome"),
            )
        except Exception as e:
            print(f"[LESIONES] refresh bloqueo: {e}")

    # Si el pick congelado es el equipo del starter lesionado → no dinero
    les = juego.get("lesiones") if isinstance(juego.get("lesiones"), dict) else {}
    pick_now = (juego.get("pick") or "")
    if les.get("starter_riesgo"):
        if (les.get("starter_away_lesionado") and juego.get("visitante") in pick_now) or (
            les.get("starter_home_lesionado") and juego.get("home") in pick_now
        ):
            motivo = "Spot no apto para dinero ahora"
            if pred_existente is not None:
                pred_existente["apostable"] = False
                pred_existente["motivo_apuesta"] = (
                    f"{pred_existente.get('motivo_apuesta') or ''} · {motivo}"
                ).strip(" ·")
                pred_existente["lesiones"] = les
            guardar_memoria(memoria)
            return {
                "ok": False,
                "motivo": motivo,
                "juego": juego["visitante"] + " vs " + juego["home"],
                "prediccion_guardada": True,
                "lesiones": les,
            }

    # Scratch SP / estrellas fuera — re-chequeo al momento del dinero
    if cfg.get("usar_scratch_lineup", True):
        try:
            from lineup_scratch import analizar_scratch_lineup, pick_afectado_por_scratch

            pred_ref = dict(pred_existente or {})
            scratch = analizar_scratch_lineup(
                away_id=juego.get("away_id"),
                home_id=juego.get("home_id"),
                pitcher_away_id=juego.get("pitcher_away_id"),
                pitcher_home_id=juego.get("pitcher_home_id"),
                pitcher_away_nombre=juego.get("pitcherAway"),
                pitcher_home_nombre=juego.get("pitcherHome"),
                lineups=juego.get("lineups"),
                season=int(cfg.get("temporada_mlb") or 2026),
                pred_congelada=pred_ref,
                min_estrellas_fuera=int((cfg.get("estrategia") or {}).get("min_estrellas_fuera_lineup", 2)),
            )
            juego["scratch_lineup"] = scratch
            if pred_existente is not None:
                pred_existente["scratch_lineup"] = scratch
            from filtro_valor import penaliza_scratch

            scratch_del_pick = scratch.get("riesgo") and pick_afectado_por_scratch(
                pick_now, juego.get("visitante") or "", juego.get("home") or "", scratch
            )
            if scratch_del_pick and penaliza_scratch(cfg):
                motivo = "Spot no apto para dinero ahora"
                if pred_existente is not None:
                    pred_existente["apostable"] = False
                guardar_memoria(memoria)
                print(f"[SCRATCH] Dinero cancelado: {scratch.get('alerta')}")
                return {
                    "ok": False,
                    "motivo": motivo,
                    "juego": juego["visitante"] + " vs " + juego["home"],
                    "prediccion_guardada": True,
                    "scratch_lineup": scratch,
                }
            if scratch_del_pick:
                print(f"[SCRATCH] Registrado sin penalizar: {scratch.get('alerta')}")
        except Exception as e:
            print(f"[SCRATCH] refresh bloqueo: {e}")

    # Precio de la cadena (#121). Sin momio real fresco no hay dinero (#123 + auditoría).
    # El filtro de valor solo anota en sombra; el tipo vuelve a decidir tras el scratch.
    pick_final = (juego.get("pick") or "").strip()
    prob_final = float(juego.get("probPick") or 0)
    if pred_existente:
        for campo in ("pick_lado", "pick_team_id", "pick_abbr", "away_abbr", "home_abbr", "away_id", "home_id"):
            if pred_existente.get(campo) not in (None, ""):
                momio_vivo[campo] = pred_existente.get(campo)
                juego[campo] = pred_existente.get(campo)
    precio = momio_del_pick(dict(momio_vivo), pick_final, prob_final or 50.0)
    from cadena_momios import parsear_momio_americano

    american_ok = parsear_momio_americano(precio.get("odds_american"), registrar=False)
    if not precio.get("apto_para_apuesta") or american_ok is None:
        _marcar_sin_momio_real(juego, pred_existente, precio, prob_final)
        guardar_memoria(memoria)
        print(
            f"[MOMIO] {juego.get('pick')} sin momio real · "
            f"{precio.get('momio_fallos') or 'sin casa'} · registrado sin apuesta"
        )
        return {
            "ok": False,
            "motivo": "Sin momio real, solo registrado",
            "juego": juego["visitante"] + " vs " + juego["home"],
            "prediccion_guardada": True,
            "sin_momio_real": True,
            "fuente_momio": "sin_momio_real",
            "probPick": prob_final,
            "momio_fallos": precio.get("momio_fallos") or "",
        }
    precio = {**precio, "odds_american": american_ok}
    juego["odds"] = precio["odds"]
    juego["odds_american"] = american_ok
    juego["fuente_momio"] = precio["fuente_momio"]
    juego["lineas_fuente"] = precio["fuente_momio"]
    juego["casa_momio"] = precio["casa"]
    juego["paso_momio"] = precio.get("paso_momio") or ""
    juego["origen_momio"] = precio.get("origen_momio") or ""
    juego["momio_fallos"] = precio.get("momio_fallos") or ""
    juego["momio_fetched_at"] = precio.get("fetched_at")
    juego["edge"] = edge_pct(prob_final, float(precio["odds"]))

    if not cfg.get("modo_solo_modelo") and (cfg.get("estrategia") or {}).get("requiere_betmgm", True):
        from filtro_valor import filtro_activo

        if filtro_activo(cfg):
            _anotar_filtro_valor_bloqueo(juego, pred_existente, cfg)
        else:
            min_edge = float((cfg.get("estrategia") or {}).get("min_edge_pct", 6.0))
            edge_now = juego.get("edge")
            if edge_now is None or float(edge_now) < min_edge:
                motivo = "Sin valor vs mercado ahora"
                juego["apostable"] = False
                juego["motivo_apuesta"] = motivo
                if pred_existente is not None:
                    pred_existente["apostable"] = False
                    pred_existente["motivo_apuesta"] = motivo
        _aplicar_tipo_sobre(juego, cfg, pred_existente)
        if not juego.get("apostable"):
            motivo = juego.get("motivo_apuesta") or "Sin valor vs cuota real"
            guardar_memoria(memoria)
            return {
                "ok": False,
                "motivo": motivo,
                "juego": juego["visitante"] + " vs " + juego["home"],
                "prediccion_guardada": True,
                "filtro_valor": juego.get("filtro_valor"),
                "filtro_tipo": juego.get("filtro_tipo"),
            }

    # Modelo propone → MENTE concluye (APOSTAR/PASAR/ESPERAR) → solo entonces dinero.
    # Si mente off: cae al veto Groq legacy (con lecciones en memoria).
    mente = None
    veto = {"ok": False, "decision": "SKIP", "motivo": "", "confianza": 0}
    if cfg.get("usar_mente", True):
        # Congelar/actualizar briefing interno justo antes de decidir (fase bloqueo)
        try:
            if pred_existente and isinstance(pred_existente.get("ia_briefing"), dict):
                juego["ia_briefing"] = pred_existente["ia_briefing"]
            generar_briefing_juego(juego, memoria, fase="bloqueo")
            if pred_existente is not None:
                pred_existente["ia_briefing"] = juego.get("ia_briefing")
        except Exception as e:
            print(f"[BRIEFING] aviso bloqueo: {e}")
        mente = mente_conclusion(juego, cfg, memoria)
        juego["ia_mente"] = mente
        if pred_existente is not None:
            pred_existente["ia_mente"] = mente
        if veredicto_bloquea_dinero(mente, cfg):
            motivo_m = (
                f"MENTE {mente.get('decision')}: "
                + "; ".join(mente.get("razones") or [mente.get("decision") or "bloqueo"])
            )
            if pred_existente is not None:
                pred_existente["motivo_apuesta"] = (
                    f"{pred_existente.get('motivo_apuesta') or ''} · {motivo_m}"
                ).strip(" ·")
            guardar_memoria(memoria)
            print(f"[MENTE] Dinero cancelado para {juego.get('pick')}: {motivo_m}")
            return {
                "ok": False,
                "motivo": motivo_m,
                "juego": juego["visitante"] + " vs " + juego["home"],
                "prediccion_guardada": True,
                "ia_mente": mente,
            }
        if mente.get("shadow"):
            print(
                f"[MENTE] Sombra {juego.get('pick')}: {mente.get('decision')} "
                f"conf={mente.get('confianza')} (no aprueba ni bloquea)"
            )
            veto = {
                "ok": True,
                "decision": mente.get("decision"),
                "motivo": "sombra: veredicto registrado, sin gate",
                "confianza": mente.get("confianza"),
                "fuente": "mente_sombra",
            }
        else:
            # Compat: mapear a forma de veto para logs antiguos
            veto = {
                "ok": True,
                "decision": "APOSTAR",
                "motivo": "; ".join(mente.get("razones") or [])[:120],
                "confianza": mente.get("confianza"),
                "fuente": "mente",
            }
    else:
        veto = veto_apuesta(juego, cfg, memoria=memoria)
        if pred_existente is not None:
            pred_existente["ia_veto"] = veto
        if veto.get("ok") and veto.get("decision") == "PASAR":
            motivo_veto = f"IA PASAR: {veto.get('motivo') or 'veto contextual'}"
            if pred_existente is not None:
                pred_existente["motivo_apuesta"] = (
                    f"{pred_existente.get('motivo_apuesta') or ''} · {motivo_veto}"
                ).strip(" ·")
            guardar_memoria(memoria)
            print(f"[IA-VETO] Dinero cancelado para {juego.get('pick')}: {motivo_veto}")
            return {
                "ok": False,
                "motivo": motivo_veto,
                "juego": juego["visitante"] + " vs " + juego["home"],
                "prediccion_guardada": True,
                "ia_veto": veto,
            }

    stake = apuesta_fija_dolares(cfg)

    riesgo = sum(a["stake"] for a in dia["apuestas"] if a["estado"] == "pendiente")
    print(f"[DEBUG BLOQUEO] Juego {game_id} - Riesgo: {riesgo}, Stake: {stake}, Capital: {memoria['capital']}")
    if riesgo + stake > memoria["capital"]:
        guardar_memoria(memoria)
        return {
            "ok": False,
            "motivo": f"Banca insuficiente (${memoria['capital']:.2f}).",
            "prediccion_guardada": True,
        }

    ahora = datetime.now(tz_experimento())
    motivo_final = juego.get("motivo_apuesta") or ""
    if mente and mente.get("autoriza_dinero"):
        motivo_final = (
            f"{motivo_final} · MENTE APOSTAR: "
            + "; ".join(mente.get("razones") or [])
            + f" (conf {mente.get('confianza')})"
        ).strip(" ·")
    elif mente and mente.get("shadow"):
        motivo_final = (
            f"{motivo_final} · MENTE sombra {mente.get('decision')}: "
            + "; ".join(mente.get("razones") or [])
        ).strip(" ·")
    elif veto.get("ok") and veto.get("decision") == "APOSTAR":
        motivo_final = (
            f"{motivo_final} · IA APOSTAR: {veto.get('motivo')} "
            f"(conf {veto.get('confianza')})"
        ).strip(" ·")
    dia["apuestas"].append(
        {
            "game_id": str(juego["id"]),
            "visitante": juego["visitante"],
            "home": juego["home"],
            "pick": juego["pick"],
            "pick_lado": juego.get("pick_lado"),
            "pick_abbr": juego.get("pick_abbr"),
            "pick_team_id": juego.get("pick_team_id"),
            "away_id": juego.get("away_id"),
            "home_id": juego.get("home_id"),
            "away_abbr": juego.get("away_abbr"),
            "home_abbr": juego.get("home_abbr"),
            **campos_precio_congelado(precio, stake),
            "edge": juego.get("edge"),
            "probPick": juego.get("probPick"),
            "motivo_apuesta": motivo_final,
            "ia_veto": veto if veto.get("ok") else None,
            "ia_mente": mente,
            "ia_briefing": juego.get("ia_briefing")
            if isinstance(juego.get("ia_briefing"), dict)
            else None,
            "clima": juego.get("clima") if isinstance(juego.get("clima"), dict) else None,
            "lesiones": juego.get("lesiones") if isinstance(juego.get("lesiones"), dict) else None,
            "factores_humanos": juego.get("factores_humanos")
            if isinstance(juego.get("factores_humanos"), dict)
            else None,
            "historico_oficial": juego.get("historico_oficial")
            if isinstance(juego.get("historico_oficial"), dict)
            else None,
            "ml_features": juego.get("ml_features") if isinstance(juego.get("ml_features"), dict) else None,
            "tipo_pick": juego.get("tipo_pick"),
            "inteligencia": juego.get("inteligencia")
            if isinstance(juego.get("inteligencia"), dict)
            else None,
            "elo": juego.get("elo") if isinstance(juego.get("elo"), dict) else None,
            "pitcherAway": juego.get("pitcherAway"),
            "pitcherHome": juego.get("pitcherHome"),
            "inicio_juego": juego.get("inicio_juego"),
            "hora_bloqueo_plan": juego.get("hora_bloqueo"),
            "stake": stake,
            "estado": "pendiente",
            "profit": None,
            "bloqueado_en": ahora.isoformat(),
        }
    )
    apuesta_n = dia["apuestas"][-1]
    if pred_existente:
        for clv_k in (
            "clv_pct",
            "clv_entrada_pct",
            "clv_odds_entrada",
            "clv_pin_cierre_away",
            "clv_pin_cierre_home",
            "clv_cierre_en",
        ):
            if pred_existente.get(clv_k) is not None:
                apuesta_n[clv_k] = pred_existente[clv_k]
    try:
        actualizar_clv_registro(apuesta_n, juego, fase="cierre")
        if pred_existente is not None:
            actualizar_clv_registro(pred_existente, juego, fase="cierre")
    except Exception as e:
        print(f"[CLV] aviso bloqueo dinero: {e}")
    guardar_prediccion(dia, juego, con_dinero=True, stake_virtual=stake_v)
    if not dia.get("bloqueado_en"):
        dia["bloqueado_en"] = ahora.isoformat()

    actualizar_resumen(memoria)
    guardar_memoria(memoria)
    exportar_reporte(memoria, dia)

    print(
        f"[MOTOR] Apuesta bloqueada: {juego['pick']} | stake ${stake:.2f} | "
        f"capital ${memoria['capital']:.2f}"
    )
    return {
        "ok": True,
        "pick": juego["pick"],
        "stake": stake,
        "capital": memoria["capital"],
        "juego": juego["visitante"] + " vs " + juego["home"],
        "odds": juego.get("odds"),
        "edge": juego.get("edge"),
        "game_id": str(game_id),
    }


def bloquear_apuestas_del_dia(forzar: bool = False) -> dict:
    """Predicción en todos los juegos + apuesta con dinero solo en apostables."""
    programar_bloqueos_por_juego()
    pred_res = registrar_predicciones_del_dia(forzar=forzar)
    memoria = cargar_memoria()
    hoy = fecha_str()
    ahora = ahora_simulado()
    cfg = cargar_config()
    juegos = obtener_juegos_fecha(hoy)
    dia = asegurar_dia_operativo(memoria, hoy)
    preds_por_id = {
        str(p.get("game_id")): p for p in (dia.get("predicciones") or [])
    }
    nuevas = 0
    omitidos = []

    for juego in juegos:
        ok_estado, _motivo_est = _permite_bloqueo_dinero(juego, forzar=forzar)
        if not ok_estado:
            continue
        # PROGRAMADO: respetar hora de bloqueo T-60. EN VIVO en gracia: ya pasó.
        if juego["estado"] == "PROGRAMADO":
            hb = datetime.fromisoformat(juego["hora_bloqueo"])
            ya_pasó = hb <= ahora or forzar
            if not ya_pasó:
                continue
        gid = str(juego["id"])
        pred = preds_por_id.get(gid)
        apostable = apostable_para_dinero(juego)
        if not apostable and pred is not None:
            apostable = apostable_para_dinero(pred)
            if apostable:
                juego["apostable"] = True
        if not apostable:
            continue
        res = bloquear_juego(juego["id"], forzar=forzar)
        if res.get("ok"):
            nuevas += 1
            memoria = cargar_memoria()
        else:
            omitidos.append(f"{juego['visitante']} vs {juego['home']}: {res.get('motivo')}")

    return {
        "ok": True,
        "apuestas_nuevas": nuevas,
        "predicciones_nuevas": pred_res.get("predicciones_nuevas", 0),
        "omitidos": omitidos,
        "programados": sum(
            1 for j in juegos if j["estado"] != "FINALIZADO"
        ),
        "capital_actual": memoria["capital"],
        "congelacion": pred_res.get("congelacion"),
    }


def congelar_pick_si_toca(game_id: str) -> dict:
    """Una ventana T-90/T-60/T-30/T-10. Si ya está congelado, no cambia el pick."""
    try:
        res = registrar_predicciones_del_dia(forzar=False)
        res["game_id"] = str(game_id)
        return res
    except Exception as e:
        print(f"[CONGELAR] ventana game_id={game_id}: {e}")
        return {"ok": False, "game_id": str(game_id), "error": str(e)}


def programar_bloqueos_por_juego() -> None:
    """Programa congelado en cada ventana y el bloqueo de dinero en T-60."""
    cfg = cargar_config()
    tz = cfg["timezone"]
    ahora = ahora_simulado()
    retries = _minutos_retry_cuotas(cfg)
    ventanas = ventanas_congelacion(cfg)

    for job in scheduler.get_jobs():
        jid = job.id or ""
        if (
            jid.startswith("bloqueo_juego_")
            or jid.startswith("cuotas_retry_")
            or jid.startswith("congelar_juego_")
        ):
            scheduler.remove_job(job.id)

    juegos = obtener_juegos_fecha(fecha_str())
    for juego in juegos:
        if juego["estado"] == "FINALIZADO":
            continue
        inicio = datetime.fromisoformat(juego["inicio_juego"])
        hb = datetime.fromisoformat(juego["hora_bloqueo"])
        if hb <= ahora and juego["estado"] != "PROGRAMADO":
            continue
        gid = juego["id"]
        for mins in ventanas:
            run_at = inicio - timedelta(minutes=mins)
            if run_at <= ahora or run_at >= inicio:
                continue
            scheduler.add_job(
                lambda g=gid: congelar_pick_si_toca(g),
                DateTrigger(run_date=run_at, timezone=tz),
                id=f"congelar_juego_{gid}_{mins}",
                replace_existing=True,
            )
        if hb > ahora:
            scheduler.add_job(
                lambda g=gid: bloquear_juego(g),
                DateTrigger(run_date=hb, timezone=tz),
                id=f"bloqueo_juego_{gid}",
                replace_existing=True,
            )
        for mins in retries:
            run_at = inicio - timedelta(minutes=mins)
            if run_at <= ahora or run_at >= inicio:
                continue
            scheduler.add_job(
                lambda g=gid: refrescar_cuotas_juego(g),
                DateTrigger(run_date=run_at, timezone=tz),
                id=f"cuotas_retry_{gid}_{mins}",
                replace_existing=True,
            )
        retry_txt = ", ".join(f"T-{m}" for m in retries) if retries else "—"
        ventanas_txt = ", ".join(f"T-{m}" for m in ventanas)
        print(
            f"[PROGRAMADO] {juego['visitante']} vs {juego['home']} → "
            f"congelar {ventanas_txt} · bloqueo {juego['hora_bloqueo_txt']} · "
            f"retry cuotas {retry_txt} (juego {juego['hora_inicio_txt']})"
        )


def refrescar_cuotas_pendientes_hoy(cfg: dict | None = None) -> dict:
    """
    Upgrade masivo: predicciones con cuota estimada → cuota de casa cuando ESPN responde.
    Evita que el papel quede congelado en 📐 mientras el mercado ya está disponible.
    """
    cfg = cfg or cargar_config()
    if not _mercado_requiere_cuotas(cfg):
        return {"ok": True, "omitido": True, "motivo": "modo_papel"}

    hoy = fecha_str()
    juegos = obtener_juegos_fecha(hoy)
    por_id = {str(j["id"]): j for j in juegos}
    upgraded = 0
    apostables = 0
    bloqueos: list[str] = []

    with _memoria_lock:
        memoria = cargar_memoria()
        dia = asegurar_dia_operativo(memoria, hoy)
        for pred in dia.get("predicciones", []):
            gid = str(pred.get("game_id") or "")
            if not gid:
                continue
            if any(str(a.get("game_id")) == gid for a in dia.get("apuestas", [])):
                continue
            if tiene_cuota_mercado(pred):
                continue
            juego = por_id.get(gid)
            if not juego or not tiene_cuota_mercado(juego):
                continue
            if not actualizar_mercado_en_prediccion(pred, juego, cfg):
                continue
            upgraded += 1
            if pred.get("apostable"):
                apostables += 1
                bloqueos.append(gid)
        if upgraded:
            guardar_memoria(memoria)
            print(
                f"[CUOTAS] Upgrade papel→mercado: {upgraded} pred(s), "
                f"{apostables} apostable(s)"
            )

    for gid in bloqueos:
        try:
            bloquear_juego(gid)
        except Exception as e:
            print(f"[CUOTAS] bloqueo post-upgrade {gid}: {e}")

    return {
        "ok": True,
        "actualizados": upgraded,
        "apostables": apostables,
        "bloqueos_intentados": len(bloqueos),
    }


def refrescar_cuotas_juego(game_id: str) -> dict:
    """Reintento T-45/T-30: refresca cuotas y apuesta si aparece valor vs mercado."""
    cfg = cargar_config()
    if not _mercado_requiere_cuotas(cfg):
        return {"ok": True, "omitido": True, "motivo": "modo_papel"}

    try:
        from lineas_espn import invalidar_cache_espn

        invalidar_cache_espn()
    except Exception:
        pass

    hoy = fecha_str()
    juegos = obtener_juegos_fecha(hoy, solo_resultados=True)
    juego = next((j for j in juegos if str(j["id"]) == str(game_id)), None)
    if not juego:
        return {"ok": False, "motivo": "juego_no_encontrado"}
    if juego.get("estado") == "FINALIZADO":
        return {"ok": True, "omitido": True, "motivo": "finalizado"}

    try:
        from mente_errores import aplicar_overrides_config

        cfg_eff = aplicar_overrides_config(cfg)
    except Exception:
        cfg_eff = cfg
    juegos, _ = aplicar_lineas_a_juegos([juego], cfg_eff)
    juego = juegos[0]

    upgraded = False
    apostable_ahora = False
    with _memoria_lock:
        memoria = cargar_memoria()
        dia = asegurar_dia_operativo(memoria, hoy)
        pred = next(
            (p for p in dia.get("predicciones", []) if str(p.get("game_id")) == str(game_id)),
            None,
        )
        if not pred:
            return {"ok": True, "omitido": True, "motivo": "sin_prediccion_t60"}
        if any(str(a.get("game_id")) == str(game_id) for a in dia.get("apuestas", [])):
            return {"ok": True, "omitido": True, "motivo": "ya_apostado"}

        upgraded = actualizar_mercado_en_prediccion(pred, juego, cfg)
        apostable_ahora = bool(pred.get("apostable"))
        if upgraded:
            try:
                actualizar_clv_registro(pred, juego, fase="cierre")
            except Exception as e:
                print(f"[CLV] aviso retry cuotas: {e}")
            guardar_memoria(memoria)
            print(
                f"[CUOTAS] Retry {game_id} · {juego.get('visitante')}@{juego.get('home')} → "
                f"{pred.get('lineas_fuente')} edge={pred.get('edge')}% apostable={apostable_ahora}"
            )

    if upgraded and apostable_ahora:
        res = bloquear_juego(str(game_id))
        return {
            "ok": True,
            "actualizado": True,
            "apostable": apostable_ahora,
            "bloqueo": res,
        }
    return {
        "ok": True,
        "actualizado": upgraded,
        "apostable": apostable_ahora,
        "motivo": "sin_valor" if upgraded else "sin_cuota_nueva",
    }


def exportar_reporte(memoria: dict, dia: dict) -> None:
    res = dia.get("resumen", resumen_dia(dia))
    lines = [
        "=" * 90,
        f" QUANTUM MLB // DÍA {dia['dia']} DE {memoria['dias_totales']} — {dia['fecha']}",
        f" MODO: {memoria.get('modo', 'simulacion').upper()} | Solo picks con VALOR vs BetMGM",
        "=" * 90,
        "",
        f"{'PARTIDO':<38} {'PICK':<20} {'EDGE':>6} {'CUOTA':>6} {'STAKE':>6} {'ESTADO':<10} {'P/L':>8}",
        "-" * 90,
    ]
    for a in dia["apuestas"]:
        partido = f"{a['visitante']} vs {a['home']}"
        pl = (
            "PENDIENTE"
            if a["estado"] == "pendiente"
            else f"{a['profit']:+.2f}"
        )
        amer = a.get("odds_american")
        edge = a.get("edge")
        cuota_txt = f"{a['odds']:.2f}" + (f" ({amer:+d})" if amer is not None else "")
        edge_txt = f"+{edge:.1f}%" if edge is not None else "  —  "
        lines.append(
            f"{partido:<38} {a['pick']:<20} {edge_txt:>6} {cuota_txt:>8} "
            f"${a['stake']:>4.0f} {a['estado'].upper():<10} {pl:>8}"
        )
    lines.extend(
        [
            "",
            "=" * 90,
            f" Capital al inicio del experimento : ${memoria['capital_inicial']:.2f}",
            f" BANCA VIVA ACUMULADA              : ${memoria['capital']:.2f}",
            f" P/L del día                       : ${res['profit_dia']:+.2f}",
            f" Ganadas / Perdidas / Pendientes   : "
            f"{res['ganadas']} / {res['perdidas']} / {res['pendientes']}",
            "=" * 90,
        ]
    )
    txt = DATA_DIR / f"reporte_dia_{dia['dia']}.txt"
    txt.write_text("\n".join(lines), encoding="utf-8")


def fusionar_apuestas_con_juegos(juegos: list[dict], memoria: dict) -> list[dict]:
    """Congela el pick bloqueado/predicho para que no 'cambie' con el marcador en vivo."""
    # Preferir día de la fecha de los juegos (hoy), no solo dia_actual
    fecha = None
    if juegos:
        # inicio_juego ISO; la fecha operativa es fecha_str()
        fecha = fecha_str()
    dia = dia_por_fecha(memoria, fecha) if fecha else None
    if not dia:
        dia = dia_operativo(memoria)
    por_id = {}
    preds_por_id = {}
    if dia:
        por_id = {str(a["game_id"]): a for a in dia.get("apuestas", [])}
        preds_por_id = {str(p["game_id"]): p for p in dia.get("predicciones", [])}

    def _sync_linea_pred(copia: dict, pred: dict | None) -> None:
        if pred and pred.get("linea_movimiento_pct") is not None:
            copia["linea_movimiento_pct"] = pred.get("linea_movimiento_pct")

    cfg = {}
    try:
        cfg = cargar_config()
    except Exception:
        cfg = {}
    mente_on = bool(cfg.get("usar_mente", True))

    resultado = []
    for juego in juegos:
        copia = dict(juego)
        gid = str(juego.get("id") or "")
        ap = por_id.get(gid)
        pred = preds_por_id.get(gid)
        if ap:
            copia["stake"] = ap["stake"]
            copia["pick"] = ap["pick"]
            copia["odds"] = ap["odds"]
            copia["odds_american"] = ap.get("odds_american")
            copia["probPick"] = ap.get("probPick", copia.get("probPick"))
            copia["lineas_fuente"] = ap.get("lineas_fuente", "betmgm")
            if ap.get("fuente_momio"):
                copia["fuente_momio"] = ap.get("fuente_momio")
            if ap.get("casa"):
                copia["casa_momio"] = ap.get("casa")
            if ap.get("paso_momio"):
                copia["paso_momio"] = ap.get("paso_momio")
            if ap.get("origen_momio"):
                copia["origen_momio"] = ap.get("origen_momio")
            if ap.get("payout_si_gana") is not None:
                copia["payout_si_gana"] = ap.get("payout_si_gana")
            if ap.get("momio_fallos"):
                copia["momio_fallos"] = ap.get("momio_fallos")
            copia["estado_apuesta"] = ap["estado"]
            copia["profit"] = ap.get("profit")
            copia["edge"] = ap.get("edge", copia.get("edge"))
            copia["motivo_apuesta"] = ap.get("motivo_apuesta", copia.get("motivo_apuesta", ""))
            if ap.get("clv_pct") is not None:
                copia["clv_pct"] = ap.get("clv_pct")
            copia["pick_congelado"] = True
            if pred:
                _sync_linea_pred(copia, pred)
            # Ya hay dinero: no dejar que el modelo en vivo diga "NO APOSTAR"
            copia["apostable"] = True
        elif pred:
            # Mantener el pick original de la predicción (no el recalculado en vivo)
            copia["stake"] = memoria["stake_por_juego"]
            copia["pick"] = pred["pick"]
            copia["odds"] = pred.get("odds", copia.get("odds"))
            copia["odds_american"] = pred.get("odds_american", copia.get("odds_american"))
            copia["probPick"] = pred.get("probPick", copia.get("probPick"))
            copia["edge"] = pred.get("edge", copia.get("edge"))
            copia["motivo_apuesta"] = pred.get("motivo_apuesta", copia.get("motivo_apuesta", ""))
            if pred.get("lineas_fuente"):
                copia["lineas_fuente"] = pred.get("lineas_fuente")
            if pred.get("fuente_momio"):
                copia["fuente_momio"] = pred.get("fuente_momio")
            if pred.get("casa_momio"):
                copia["casa_momio"] = pred.get("casa_momio")
            if pred.get("paso_momio"):
                copia["paso_momio"] = pred.get("paso_momio")
            if pred.get("origen_momio"):
                copia["origen_momio"] = pred.get("origen_momio")
            if pred.get("clv_pct") is not None:
                copia["clv_pct"] = pred.get("clv_pct")
            copia["pick_congelado"] = True
            _sync_linea_pred(copia, pred)
            if not tiene_cuota_mercado(copia) and not tiene_cuota_mercado(pred):
                copia["apostable"] = False
                copia["edge"] = 0
            else:
                copia["apostable"] = apostable_con_mercado(pred) or apostable_con_mercado(copia)
            copia["resultado_papel"] = pred.get("resultado")
            copia["invalida_tarde"] = bool(pred.get("invalida_tarde"))
            if pred.get("estado") == "liquidado" and pred.get("resultado") in (
                "acierto",
                "fallo",
            ):
                # Para el panel: acierto/fallo en papel (no es banca real)
                if copia["invalida_tarde"]:
                    copia["estado_apuesta"] = "invalida_tarde"
                    copia["profit"] = pred.get("profit")
                    copia["solo_papel"] = True
                    copia["motivo_apuesta"] = (
                        (copia.get("motivo_apuesta") or "")
                        + " · Pick tardío (no cuenta en precisión)"
                    ).strip(" ·")
                else:
                    copia["estado_apuesta"] = (
                        "ganada" if pred["resultado"] == "acierto" else "perdida"
                    )
                    copia["profit"] = pred.get("profit")
                    copia["solo_papel"] = True
            else:
                copia["estado_apuesta"] = "pendiente"
                copia["profit"] = None
                copia["solo_papel"] = True
        else:
            copia["stake"] = memoria["stake_por_juego"]
            copia["estado_apuesta"] = "sin_bloquear"
            copia["profit"] = None
            copia["pick_congelado"] = False
            if copia.get("estado") == "EN VIVO":
                copia["motivo_apuesta"] = (
                    (copia.get("motivo_apuesta") or "")
                    + " · Pick en vivo (puede cambiar; no cuenta hasta T-60)"
                ).strip(" ·")
            if copia.get("estado") == "FINALIZADO":
                copia["motivo_apuesta"] = (
                    (copia.get("motivo_apuesta") or "")
                    + " · Final sin pick congelado (no cuenta en papel)"
                ).strip(" ·")
            # Sin pick congelado: el panel no debe tratar el pick vivo como resultado
            if copia.get("estado") in ("EN VIVO", "FINALIZADO"):
                copia["solo_orientativo"] = True
        copia["apostable"] = copia.get("apostable", False)
        if copia.get("apostable") and not ap and not tiene_cuota_mercado(copia):
            copia["apostable"] = False
            copia["edge"] = 0
        if not copia.get("motivo_apuesta"):
            copia["motivo_apuesta"] = ""
        mente_guardada = None
        if ap and isinstance(ap.get("ia_mente"), dict):
            mente_guardada = ap["ia_mente"]
        elif pred and isinstance(pred.get("ia_mente"), dict):
            mente_guardada = pred["ia_mente"]
        if mente_guardada:
            # Exponer decisión resumida; sin briefing interno ni texto largo
            copia["ia_mente"] = {
                "decision": mente_guardada.get("decision"),
                "confianza": mente_guardada.get("confianza"),
                "autoriza_dinero": mente_guardada.get("autoriza_dinero"),
                "razones": list(mente_guardada.get("razones") or [])[:2],
                "fuente": mente_guardada.get("fuente"),
                "modo": mente_guardada.get("modo"),
            }
        elif mente_on and copia.get("pick"):
            try:
                mloc = mente_conclusion(copia, cfg, memoria, solo_local=True)
                copia["ia_mente"] = {
                    "decision": mloc.get("decision"),
                    "confianza": mloc.get("confianza"),
                    "autoriza_dinero": mloc.get("autoriza_dinero"),
                    "razones": list(mloc.get("razones") or [])[:2],
                    "fuente": mloc.get("fuente"),
                    "modo": mloc.get("modo"),
                }
            except Exception:
                pass
        # Briefing T-60: solo memoria interna — nunca al panel
        copia.pop("ia_briefing", None)
        resultado.append(copia)
    return resultado


def _validar_odds_api_en_background() -> None:
    """Comprueba la clave sin consumir cuota. No imprime la excepción (puede traer la URL)."""
    try:
        validar_clave_odds_api(cargar_config())
    except Exception as e:
        print(f"[ODDS] validación periódica: {type(e).__name__}")


def programar_tareas_background() -> None:
    cfg = cargar_config()
    tz = cfg["timezone"]
    scheduler.add_job(
        avanzar_dia_automatico,
        CronTrigger(hour=0, minute=0, timezone=tz),
        id="cambio_dia_medianoche",
        replace_existing=True,
    )
    scheduler.add_job(
        programar_bloqueos_por_juego,
        CronTrigger(hour=6, minute=0, timezone=tz),
        id="refresh_calendario_am",
        replace_existing=True,
    )
    scheduler.add_job(
        programar_bloqueos_por_juego,
        CronTrigger(hour=12, minute=0, timezone=tz),
        id="refresh_calendario_mediodia",
        replace_existing=True,
    )
    # /v4/sports no consume cuota. Si la cadena ya llamó a The Odds API hace poco, no repite.
    scheduler.add_job(
        _validar_odds_api_en_background,
        CronTrigger(hour="*/6", minute=20, timezone=tz),
        id="validar_odds_api_key",
        replace_existing=True,
    )
    if _cron_externo_habilitado():
        print(
            "[MOTOR] CRON_SECRET activo: liquidación/bloqueo in-process omitidos "
            "(usa /api/auto-bloqueo-externo)"
        )
    else:
        scheduler.add_job(
            lambda: liquidar_todo(cargar_memoria()),
            CronTrigger(minute="2,12,22,32,42,52", timezone=tz),
            id="liquidacion_periodica",
            replace_existing=True,
        )
        scheduler.add_job(
            lambda: bloquear_apuestas_del_dia(forzar=False),
            CronTrigger(minute="7,17,27,37,47,57", timezone=tz),
            id="bloqueo_periodico",
            replace_existing=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranque rápido: Render exige puerto abierto; motor en background."""

    def _boot_completo() -> None:
        try:
            info = validar_clave_odds_api(cargar_config())
            print(
                "[ODDS] clave "
                + ("presente" if info.get("key_presente") else "ausente")
                + f" fuente={info.get('fuente')} http={info.get('http_status')}"
            )
        except Exception as e:
            print(f"[ODDS] validación de clave: {type(e).__name__}")
        try:
            programar_tareas_background()
            scheduler.start()
            _inicializar_datos_persistencia()
        except Exception as e:
            print(f"[MOTOR] Error arranque scheduler/persistencia: {e}")
        try:
            print("[MOTOR] Iniciando motor autónomo de sincronización en segundo plano...")
            avanzar_dia_automatico()
            try:
                catch = registrar_predicciones_del_dia(forzar=False)
                cong = catch.get("congelacion") or {}
                print(
                    "[CONGELAR] catch-up al despertar · "
                    f"nuevas={catch.get('predicciones_nuevas')} · "
                    f"ventanas_perdidas={cong.get('ventanas_perdidas')}"
                )
            except Exception as e:
                print(f"[CONGELAR] catch-up al despertar: {e}")
            mem_boot = cargar_memoria()
            try:
                from calibracion import entrenar_calibrador

                mem_boot["calib_meta"] = entrenar_calibrador(mem_boot, min_muestras=30)
            except Exception as e:
                print(f"[CALIB] aviso arranque: {e}")
            reparar_odds_papel(mem_boot)
            rellenar_predicciones_recientes(mem_boot, dias_atras=7)
            bloquear_apuestas_del_dia(forzar=False)
            programar_bloqueos_por_juego()
        except Exception as e:
            print(f"[MOTOR] Error programando bloqueos: {e}")
        try:
            liquidar_todo(cargar_memoria())
        except Exception as e:
            print(f"[MOTOR] Error en liquidación inicial: {e}")

    threading.Thread(target=_boot_completo, daemon=True, name="motor-boot").start()
    cfg_boot = cargar_config()
    if not cfg_boot.get("modo_solo_modelo") and (cfg_boot.get("estrategia") or {}).get(
        "requiere_betmgm", True
    ):
        stake = cfg_boot.get("stake_por_juego", 3)
        max_d = (cfg_boot.get("estrategia") or {}).get("max_apuestas_dia", 4)
        print(
            f"[BOOT] Mercado ACTIVO · stake=${stake} · max {max_d} apuestas/día · "
            f"proveedor={(cfg_boot.get('lineas') or {}).get('proveedor', 'espn')}"
        )
    else:
        print("[BOOT] Modo papel (sin mercado para dinero)")
    print("[BOOT] Puerto listo · motor en background")
    yield
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        pass


# Orígenes del panel (mismo host) y del HTML abierto en local.
# No usar "*" con credenciales: eso refleja cualquier origen.
_CORS_ORIGIN_REGEX = (
    r"https://[\w.-]+\.onrender\.com"
    r"|http://(localhost|127\.0\.0\.1)(:\d+)?"
)


def _cors_allow_origins() -> list[str]:
    """Lista explícita. `null` cubre QuantumMLB.html abierto como file://."""
    origins = ["null"]
    render_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if render_url:
        origins.append(render_url)
    extra = os.environ.get("CORS_ORIGINS") or ""
    for part in extra.split(","):
        origin = part.strip().rstrip("/")
        if origin and origin != "*" and origin not in origins:
            origins.append(origin)
    return origins


app = FastAPI(title="Quantum MLB", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_origin_regex=_CORS_ORIGIN_REGEX,
    allow_credentials=False,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Cron-Secret"],
)


@app.get("/")
def panel():
    # Sin cache: Safari iOS retiene HTML/JS viejo y el panel se queda en «Despertando…»
    return FileResponse(
        "QuantumMLB.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


_MENTE_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/mente")
@app.get("/mente/")
def panel_mente():
    """Red neuronal solo en la PC (no va en el panel del celular)."""
    return FileResponse(BASE_DIR / "mente" / "index.html", headers=_MENTE_NO_CACHE)


@app.get("/mente/Mente.url")
def mente_acceso_directo():
    """Acceso directo de Windows para arrastrar al escritorio."""
    return FileResponse(
        BASE_DIR / "mente" / "Mente.url",
        media_type="application/internet-shortcut",
        filename="Mente.url",
        headers=_MENTE_NO_CACHE,
    )


def obtener_juegos_para_panel(fecha: str, ligero: bool = False) -> list[dict]:
    """Cache corto para no recalcular ML en cada refresh del panel."""
    ahora = time.monotonic()
    if (
        ligero
        and _juegos_ui_cache["fecha"] == fecha
        and (ahora - _juegos_ui_cache["ts"]) < _JUEGOS_UI_TTL_SEC
    ):
        return _juegos_ui_cache["juegos"]
    juegos = obtener_juegos_fecha(fecha)
    if ligero:
        recortados = _juegos_para_panel(juegos)
        _juegos_ui_cache.update({"fecha": fecha, "ts": ahora, "juegos": recortados})
        return recortados
    return juegos


def _guardar_juegos_panel_disk(fecha: str, juegos: list[dict]) -> None:
    """Snapshot en disco para que el móvil pinte juegos aunque /api/state falle."""
    payload = {
        "fecha": fecha,
        "ts": time.time(),
        "games": _juegos_para_panel(juegos),
        "n": len(juegos or []),
    }
    _JUEGOS_PANEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _JUEGOS_PANEL_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_JUEGOS_PANEL_CACHE_PATH)


def _leer_juegos_panel_disk(fecha: str | None = None, max_age_sec: float | None = None) -> dict | None:
    path = _JUEGOS_PANEL_CACHE_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("games"):
        return None
    if fecha and data.get("fecha") and data.get("fecha") != fecha:
        return None
    age = time.time() - float(data.get("ts") or 0)
    limit = _JUEGOS_PANEL_DISK_MAX_AGE_SEC if max_age_sec is None else max_age_sec
    if age > limit:
        return None
    data["age_sec"] = round(age, 1)
    data["ok"] = True
    data["cache"] = "disk"
    return data


def construir_estado_completo(liquidar: bool = False, ligero: bool = False) -> dict:
    # Si Render/free borró el historial (o se pulsó reinicio por error), recuperar.
    try:
        if _intentar_recuperar_wipe():
            pass
    except Exception as e:
        print(f"[CLOUD] Aviso recuperación wipe: {e}")
    memoria = cargar_memoria()
    # Sincronizar el día del experimento con el tiempo real/simulado
    avanzar_dia_automatico()
    memoria = cargar_memoria()

    # Asegurar que el día actual existe en memoria para que los contadores no salgan en 0
    asegurar_dia_operativo(memoria)

    if not ligero:
        # Rellenar dias que se quedaron sin predicciones (servidor apagado)
        try:
            rellenar_predicciones_recientes(memoria, dias_atras=7)
        except Exception as e:
            print(f"Aviso relleno predicciones: {e}")

        # Registrar picks en papel de todos los juegos listos (sin mover banca)
        try:
            registrar_predicciones_del_dia(forzar=False)
        except Exception as e:
            print(f"Aviso predicciones: {e}")
        memoria = cargar_memoria()
    else:
        # Panel ligero: NO registrar aquí (MLB+ML ~10–15s → 502 en Render free / iPhone).
        # El cron /api/auto-bloqueo-externo hace el catch-up de predicciones.
        pass

    if liquidar:
        try:
            liquidar_todo(memoria)
        except Exception as e:
            print(f"Aviso liquidación: {e}")
        memoria = cargar_memoria()

    # Solo recalcular si hubo liquidación o cambios para evitar escrituras constantes en disco
    if liquidar:
        actualizar_resumen(memoria)
        recalcular_capital(memoria)

    # Sincronizar el stake visual con la configuración actual
    cfg = cargar_config()
    memoria["stake_por_juego"] = cfg.get("stake_por_juego", 3.0)
    # Día de HOY por fecha (no solo por dia_actual) y resumen siempre fresco
    fecha_hoy = fecha_str()
    dia = dia_por_fecha(memoria, fecha_hoy) or dia_operativo(memoria)
    if dia:
        dia["resumen"] = resumen_dia(dia)

    # Marcar historial tardío ANTES de fusionar el panel; deshacer filtro por %
    try:
        n_lean = limpiar_predicciones_confianza_baja(memoria)
        n_tarde = marcar_predicciones_tardias(memoria)
        if n_tarde or n_lean:
            guardar_memoria(memoria)
            if n_lean:
                print(f"[PREDICCIONES] Restaurado conteo de {n_lean} picks (sin filtro por %).")
            if n_tarde:
                print(f"[PREDICCIONES] Marcadas {n_tarde} como inválidas (congeladas tras el inicio).")
    except Exception as e:
        print(f"[PREDICCIONES] marcar tardías: {e}")

    juegos = []
    try:
        juegos = fusionar_apuestas_con_juegos(
            obtener_juegos_para_panel(fecha_hoy, ligero=ligero), memoria
        )
        try:
            _guardar_juegos_panel_disk(fecha_hoy, juegos)
        except Exception:
            pass
    except Exception as e:
        print(f"Error cargando juegos: {e}")

    # Calcular estadísticas del modelo
    stats_modelo = calcular_estadisticas_modelo(memoria)
    pl_split = resumen_predicciones_y_dinero(memoria)
    if pl_split.pop("_mutado", False):
        try:
            guardar_memoria(memoria)
        except Exception:
            pass

    # Resumen del día también con predicciones en papel (para el panel)
    if dia:
        if not dia.get("resumen"):
            try:
                dia["resumen"] = resumen_dia(dia)
            except Exception:
                pass
        try:
            resumen_hoy = dict(dia.get("resumen") or resumen_dia(dia))
        except Exception:
            resumen_hoy = {
                "jugadas": 0, "ganadas": 0, "perdidas": 0, "pendientes": 0,
                "profit_dia": 0.0, "capital_arriesgado": 0.0, "total_apostado": 0.0,
            }
    else:
        resumen_hoy = {
            "jugadas": 0, "ganadas": 0, "perdidas": 0, "pendientes": 0,
            "profit_dia": 0.0, "capital_arriesgado": 0.0, "total_apostado": 0.0,
        }
    preds_hoy = (dia or {}).get("predicciones") or []
    pred_aciertos = sum(
        1
        for p in preds_hoy
        if p.get("resultado") == "acierto" and prediccion_valida_para_stats(p)
    )
    pred_fallos = sum(
        1
        for p in preds_hoy
        if p.get("resultado") == "fallo" and prediccion_valida_para_stats(p)
    )
    pred_pend = sum(1 for p in preds_hoy if p.get("estado") == "pendiente")
    pred_excl = sum(1 for p in preds_hoy if not prediccion_valida_para_stats(p) and p.get("estado") == "liquidado")
    pred_neto = round(
        sum(
            float(p.get("profit") or 0)
            for p in preds_hoy
            if p.get("profit") is not None and prediccion_valida_para_stats(p)
        ),
        2,
    )
    resumen_hoy["pred_aciertos"] = pred_aciertos
    resumen_hoy["pred_fallos"] = pred_fallos
    resumen_hoy["pred_pendientes"] = pred_pend
    resumen_hoy["pred_excluidas_tarde"] = pred_excl
    resumen_hoy["pred_neto"] = pred_neto
    resumen_hoy["pred_total"] = len(preds_hoy)
    if dia:
        dia["resumen"] = resumen_hoy

    # Backfill heurístico: fallos + experiencias negativas (planes 2/5).
    lecciones_meta = {"total": 0, "por_patron": {}, "recientes": []}
    try:
        from ia_lecciones import (
            backfill_lecciones_si_vacio,
            backfill_negativas_si_falta,
            resumen_lecciones,
        )

        if not ligero:
            n_bf = backfill_lecciones_si_vacio(memoria)
            n_neg = backfill_negativas_si_falta(memoria)
            if n_bf or n_neg:
                guardar_memoria(memoria)
                print(f"[LECCIONES] Backfill: fallos={n_bf} total_scan={n_neg}")
        lecciones_meta = resumen_lecciones(memoria)
    except Exception as e:
        print(f"[LECCIONES] aviso estado: {e}")

    mente_stats_meta = {}
    try:
        from mente_aprendizaje import resumen_mente_stats, recomputar_stats_desde_historial

        if (not ligero) and not (memoria.get("mente_stats") or {}).get("actualizado_en"):
            n_ms = recomputar_stats_desde_historial(memoria)
            if n_ms:
                guardar_memoria(memoria)
                print(f"[MENTE-APRENDIZAJE] Backfill stats: {n_ms}")
        mente_stats_meta = resumen_mente_stats(memoria)
    except Exception as e:
        print(f"[MENTE-APRENDIZAJE] aviso estado: {e}")

    vigilancia = vigilancia_t60(juegos, memoria, cfg)
    cfg_ops = cfg
    # En panel ligero el cron ya corre mente/T-60; no bloquear la UI 10–20s
    if not ligero:
        try:
            ejecutar_ciclo_mente_errores(
                cfg_ops,
                vigilancia=vigilancia,
                lineas_meta=_lineas_meta_cache if isinstance(_lineas_meta_cache, dict) else None,
                memoria=memoria,
            )
        except Exception as e:
            print(f"[MENTE-ERRORES] aviso estado: {e}")
            try:
                registrar_error_runtime("api_state", str(e), codigo="mente_ciclo")
            except Exception:
                pass

    memoria_panel = _memoria_para_panel(memoria)
    dia_panel = None
    if dia:
        dia_panel = {
            "dia": dia.get("dia"),
            "fecha": dia.get("fecha"),
            "bloqueado_en": dia.get("bloqueado_en"),
            "resumen": dia.get("resumen"),
            "predicciones": [
                _recortar_dict(p, _PRED_PANEL_KEYS)
                for p in (dia.get("predicciones") or [])
                if isinstance(p, dict)
            ],
            "apuestas": [
                _recortar_dict(a, _APUESTA_PANEL_KEYS)
                for a in (dia.get("apuestas") or [])
                if isinstance(a, dict)
            ],
        }

    return {
        "memoria": memoria_panel,
        "banca": resumen_banca(memoria),
        "dia_hoy": dia_panel,
        "config": {
            k: cfg.get(k)
            for k in (
                "capital_inicial",
                "dias_totales",
                "stake_por_juego",
                "apuesta_fija",
                "minutos_antes_juego",
                "timezone",
                "modo_solo_modelo",
                "usar_ia_veto",
                "usar_mente",
            )
            if k in cfg
        },
        "lineas": _lineas_para_panel(cfg),
        "estrategia": cfg.get("estrategia", {}),
        "total_juegos_bloqueados": len(dia["apuestas"]) if dia else 0,
        "oportunidades_valor_hoy": sum(1 for j in juegos if j.get("apostable")),
        "favorables_hoy": sum(1 for j in juegos if j.get("apostable")),
        "minutos_antes_juego": cfg.get("minutos_antes_juego", 60),
        "fecha_hoy": fecha_hoy,
        "games": _juegos_para_panel(juegos),
        "stats_modelo": stats_modelo,
        "pl_split": pl_split,
        "clv_meta": resumen_clv_memoria(memoria),
        "ml_meta": memoria.get("ml_meta"),
        "calib_meta": memoria.get("calib_meta"),
        "lecciones": lecciones_meta,
        "ia_veto": {
            "activo": bool(cfg.get("usar_ia_veto")),
            "listo": ia_veto_disponible(cfg),
            "modelo": modelo_groq(cfg),
            "lecciones": lecciones_meta.get("total", 0),
        },
        "mente": {
            "activo": bool(cfg.get("usar_mente", True)),
            "listo": mente_disponible(cfg),
            "modo": ((cfg.get("mente") or {}).get("modo") or "normal"),
            "min_confianza": int((cfg.get("mente") or {}).get("min_confianza") or 3),
            "shadow": bool((cfg.get("mente") or {}).get("shadow", False)),
            "stats": mente_stats_meta,
        },
        "vigilancia": vigilancia,
        "perdidos_hoy": list((vigilancia or {}).get("perdidos") or [])[:8],
        "mente_errores": _resumen_mente_errores(cfg_ops),
        "historial_sello": _resumen_sello(memoria),
    }


@app.get("/api/historial-status")
def api_historial_status():
    """Sello rápido: ¿el historial está sano?"""
    try:
        _intentar_recuperar_wipe()
    except Exception:
        pass
    mem = cargar_memoria()
    sello = _resumen_sello(mem)
    snaps = 0
    try:
        snaps = _store().contar_snapshots()
    except Exception:
        pass
    try:
        from memoria_fusion import listar_snapshots

        snaps += len(listar_snapshots(DATA_DIR))
    except Exception:
        pass
    return {
        "ok": True,
        **sello,
        "snapshots_locales": snaps,
        **_info_memoria_backup(),
        "capital": mem.get("capital"),
        "dia_actual": mem.get("dia_actual"),
    }


@app.get("/api/panel-boot")
def api_panel_boot(
    secret: str | None = None,
    x_cron_secret: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
):
    """Arranque del panel en <1s: historial+capital sin ML ni juegos.

    Evita que Safari iOS se quede en «Despertando…» mientras /api/state
    tarda 10–60s (cold start / registrar predicciones / ESPN).
    """
    try:
        _intentar_recuperar_wipe()
    except Exception:
        pass
    # En Render el import 9 MB + ML en boot pisa el cron y dispara OOM.
    if not _en_render() and not _cron_externo_activo:
        try:
            threading.Thread(
                target=_intentar_import_aprendizaje_repo_automatico,
                daemon=True,
                name="import-aprendizaje-boot",
            ).start()
        except Exception as e:
            print(f"[IMPORT-AUTO] thread: {e}")
    memoria = cargar_memoria()
    fecha_hoy = fecha_str()
    dia = dia_por_fecha(memoria, fecha_hoy) or dia_operativo(memoria)
    # Liquidar al abrir solo si el navegador ya guardó CRON_SECRET.
    # Sin secreto el panel sigue siendo de lectura: el cron liquida.
    perdidos_hoy: list[dict] = []
    if (
        _cron_autorizado(secret, x_cron_secret, authorization)
        and dia
        and any(p.get("estado") == "pendiente" for p in (dia.get("predicciones") or []))
    ):
        try:
            n = liquidar_dia(memoria, dia)
            if n:
                memoria = cargar_memoria()
                dia = dia_por_fecha(memoria, fecha_hoy) or dia
                print(f"[BOOT] Liquidados {n} resultado(s) papel/dinero hoy")
        except Exception as e:
            print(f"[BOOT] liquidar hoy: {e}")
    try:
        juegos_res = obtener_juegos_fecha(fecha_hoy, solo_resultados=True)
        cfg_boot = cargar_config()
        vig = vigilancia_t60(juegos_res, memoria, cfg_boot)
        perdidos_hoy = list(vig.get("perdidos") or [])[:8]
    except Exception as e:
        print(f"[BOOT] vigilancia: {e}")
    pl_split = resumen_predicciones_y_dinero(memoria)
    pl_split.pop("_mutado", None)
    dia_panel = None
    if dia:
        if not dia.get("resumen"):
            try:
                dia["resumen"] = resumen_dia(dia)
            except Exception:
                pass
        dia_panel = {
            "dia": dia.get("dia"),
            "fecha": dia.get("fecha"),
            "bloqueado_en": dia.get("bloqueado_en"),
            "resumen": dia.get("resumen"),
            "predicciones": [
                _recortar_dict(p, _PRED_PANEL_KEYS)
                for p in (dia.get("predicciones") or [])
                if isinstance(p, dict)
            ],
            "apuestas": [
                _recortar_dict(a, _APUESTA_PANEL_KEYS)
                for a in (dia.get("apuestas") or [])
                if isinstance(a, dict)
            ],
        }
    cfg = cargar_config()
    # Si hay snapshot reciente de juegos, adjuntarlo (0 coste) para el móvil
    games_boot: list = []
    try:
        cached = _leer_juegos_panel_disk(fecha_hoy)
        if cached:
            games_boot = cached.get("games") or []
    except Exception:
        pass
    return {
        "ok": True,
        "boot": True,
        "memoria": _memoria_para_panel(memoria),
        "banca": resumen_banca(memoria),
        "dia_hoy": dia_panel,
        "pl_split": pl_split,
        "historial_sello": _resumen_sello(memoria),
        "fecha_hoy": fecha_hoy,
        "config": {
            k: cfg.get(k)
            for k in (
                "capital_inicial",
                "dias_totales",
                "stake_por_juego",
                "apuesta_fija",
                "minutos_antes_juego",
                "timezone",
                "modo_solo_modelo",
                "usar_ia_veto",
                "usar_mente",
            )
            if k in cfg
        },
        "estrategia": cfg.get("estrategia", {}),
        "games": games_boot,
        "minutos_antes_juego": cfg.get("minutos_antes_juego", 60),
        "perdidos_hoy": perdidos_hoy,
        "mente": {
            "activo": bool(cfg.get("usar_mente", True)),
            "modo": ((cfg.get("mente") or {}).get("modo") or "normal"),
            "shadow": bool((cfg.get("mente") or {}).get("shadow", False)),
        },
    }


def _warm_juegos_full_async(fecha: str) -> None:
    """Recalcula MLB+ML en background para llenar cache (no bloquea el panel)."""

    def _run() -> None:
        try:
            mem = cargar_memoria()
            juegos = fusionar_apuestas_con_juegos(
                obtener_juegos_para_panel(fecha, ligero=True), mem
            )
            _guardar_juegos_panel_disk(fecha, juegos)
            print(f"[JUEGOS-CACHE] warm ok fecha={fecha} n={len(juegos)}")
        except Exception as e:
            print(f"[JUEGOS-CACHE] warm fail: {e}")

    threading.Thread(target=_run, daemon=True, name="warm-juegos").start()


@app.get("/api/juegos-hoy")
def api_juegos_hoy(fresh: bool = False):
    """Juegos del día para el panel. Prioriza cache (memoria/disco) para iPhone.

    Sin cache: responde YA con schedule MLB (sin ML ~1–2s) y calienta ML en background.
    ?fresh=1 fuerza recálculo MLB+ML (más lento; puede 502 en Render free).
    """
    fecha_hoy = fecha_str()
    if not fresh:
        # 1) RAM (juegos con ML)
        ahora = time.monotonic()
        if (
            _juegos_ui_cache["fecha"] == fecha_hoy
            and (ahora - _juegos_ui_cache["ts"]) < _JUEGOS_UI_TTL_SEC
            and _juegos_ui_cache.get("juegos")
        ):
            juegos = _juegos_ui_cache["juegos"]
            mem = cargar_memoria()
            fused = fusionar_apuestas_con_juegos(juegos, mem)
            return {
                "ok": True,
                "fecha": fecha_hoy,
                "games": _juegos_para_panel(fused),
                "n": len(fused),
                "cache": "ram",
            }
        # 2) Disco
        disk = _leer_juegos_panel_disk(fecha_hoy)
        if disk:
            return disk

    memoria = cargar_memoria()

    # 3) Rápido: schedule MLB sin evaluar_juegos (evita 502 / 15s en el móvil)
    if not fresh:
        try:
            raw = obtener_juegos_fecha(fecha_hoy, solo_resultados=True)
            fused = fusionar_apuestas_con_juegos(raw, memoria)
            _warm_juegos_full_async(fecha_hoy)
            return {
                "ok": True,
                "fecha": fecha_hoy,
                "games": _juegos_para_panel(fused),
                "n": len(fused),
                "cache": "schedule",
                "parcial": True,
            }
        except Exception as e:
            print(f"[JUEGOS-HOY] schedule rápido falló: {e}")

    # 4) Completo (fresh o fallback)
    try:
        juegos = fusionar_apuestas_con_juegos(
            obtener_juegos_para_panel(fecha_hoy, ligero=True), memoria
        )
        try:
            _guardar_juegos_panel_disk(fecha_hoy, juegos)
        except Exception:
            pass
    except Exception as e:
        disk = _leer_juegos_panel_disk(fecha_hoy, max_age_sec=6 * 3600)
        if disk:
            disk["motivo"] = str(e)[:120]
            disk["stale"] = True
            return disk
        return {"ok": False, "fecha": fecha_hoy, "games": [], "motivo": str(e)[:120]}
    return {
        "ok": True,
        "fecha": fecha_hoy,
        "games": _juegos_para_panel(juegos),
        "n": len(juegos),
        "cache": "fresh" if fresh else "computed",
    }


@app.get("/api/state")
def api_state(
    liquidar: bool = False,
    secret: str | None = None,
    x_cron_secret: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
):
    """Estado del panel (liviano). Por defecto no liquida: el cron ya lo hace.

    ?liquidar=1 fuerza liquidación y exige CRON_SECRET (fail-closed si falta).
    """
    if liquidar:
        _verificar_cron_secreto(_secreto_recibido(secret, x_cron_secret, authorization))
    return construir_estado_completo(liquidar=bool(liquidar), ligero=True)


@app.get("/api/picks-hoy")
def api_picks_hoy():
    """Lista clara de picks recomendados para apostar hoy."""
    estado = construir_estado_completo(ligero=True)
    cfg = estado.get("config", {})
    estr = estado.get("estrategia", {})
    min_prob = float(estr.get("min_prob_modelo", 58))
    max_dia = int(estr.get("max_apuestas_dia", 8))
    vistos: set[str] = set()
    juegos = []
    for g in estado.get("games", []):
        gid = str(g.get("id") or "")
        if not gid or gid in vistos:
            continue
        vistos.add(gid)
        juegos.append(g)
    apostables = sorted(
        [g for g in juegos if apostable_con_mercado(g) and (g.get("probPick") or 0) >= min_prob],
        key=lambda x: x.get("edge", 0) or 0,
        reverse=True,
    )[:max_dia]
    return {
        "fecha": estado.get("fecha_hoy"),
        "min_prob_modelo": min_prob,
        "modo_solo_modelo": cfg.get("modo_solo_modelo", False),
        "total_apostables": len(apostables),
        "picks": [
            {
                "rank": i + 1,
                "equipo": (g.get("pick") or "").replace(" ML", ""),
                "pick": g.get("pick"),
                "prob": g.get("probPick"),
                "partido": f"{g.get('visitante')} @ {g.get('home')}",
                "hora": g.get("hora_inicio_txt"),
                "estado_juego": g.get("estado"),
                "estado_apuesta": g.get("estado_apuesta"),
                "motivo": g.get("motivo_apuesta"),
            }
            for i, g in enumerate(apostables)
        ],
    }


@app.get("/api/live-data")
def api_live_data():
    """Scores/juegos ligeros (usa /api/juegos-hoy)."""
    return api_juegos_hoy(fresh=False)


@app.post("/api/bloquear-hoy", dependencies=_auth_cron())
def api_bloquear_hoy():
    """Fuerza el análisis y bloqueo inmediato de los juegos que tengan valor ahora mismo."""
    resultado = bloquear_apuestas_del_dia(forzar=True)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("motivo"))
    return resultado


@app.post("/api/liquidar", dependencies=_auth_cron())
def api_liquidar():
    memoria = cargar_memoria()
    sincronizar_experimento_a_hoy(memoria)
    cambios = liquidar_todo(cargar_memoria())
    estado = construir_estado_completo(liquidar=False)
    return {
        "liquidaciones": cambios,
        "capital": estado["memoria"]["capital"],
    }


@app.post("/api/reiniciar", dependencies=_auth_cron())
def api_reiniciar(confirm: str | None = None):
    """Reinicia el experimento. Requiere confirm=BORRAR para no borrar por accidente."""
    if (confirm or "").strip().upper() != "BORRAR":
        raise HTTPException(
            status_code=400,
            detail=(
                "Reinicio bloqueado. Para borrar el historial llama "
                "/api/reiniciar?confirm=BORRAR (irreversible)."
            ),
        )
    cfg = cargar_config()
    try:
        prev = cargar_memoria()
        _respaldar_snapshot(prev)
    except Exception as e:
        print(f"[REINICIAR] snapshot previo: {e}")
    for f in DATA_DIR.glob("reporte_dia_*.txt"):
        f.unlink(missing_ok=True)

    memoria = {
        "modo": "simulacion",
        "capital": cfg["capital_inicial"],
        "capital_inicial": cfg["capital_inicial"],
        "dia_actual": 1,
        "dias_totales": cfg["dias_totales"],
        "stake_por_juego": cfg["stake_por_juego"],
        "experimento_activo": True,
        "ultimo_bloqueo": None,
        "dias": [],
        "reinicio_manual": True,
    }
    guardar_memoria(memoria, permitir_wipe=True)
    return {"ok": True, "memoria": memoria}


@app.get("/api/apuestas")
def api_apuestas():
    """Historial de apuestas por día (documento de la base)."""
    memoria = cargar_memoria()
    dia = dia_operativo(memoria)
    return {
        "capital": memoria.get("capital"),
        "dia_actual": memoria.get("dia_actual"),
        "fecha_hoy": fecha_str(),
        "apuestas_hoy": dia.get("apuestas", []) if dia else [],
        "dias": memoria.get("dias", []),
    }


@app.get("/api/resultados")
def api_resultados():
    """Curva, ROI y cortes del experimento. Solo lectura: no liquida ni guarda."""
    from resultados_mlb import calcular_resultados

    return calcular_resultados(cargar_memoria())


@app.get("/api/predicciones")
def api_predicciones():
    """Predicciones del modelo (apostadas y no apostadas) del día actual e historial."""
    memoria = cargar_memoria()
    dia = dia_operativo(memoria)
    return {
        "capital": memoria.get("capital"),
        "dia_actual": memoria.get("dia_actual"),
        "fecha_hoy": fecha_str(),
        "predicciones_hoy": dia.get("predicciones", []) if dia else [],
        "apuestas_hoy": dia.get("apuestas", []) if dia else [],
        "historial": [
            {
                "dia": d.get("dia"),
                "fecha": d.get("fecha"),
                "predicciones": d.get("predicciones", []),
                "apuestas": d.get("apuestas", []),
            }
            for d in memoria.get("dias", [])
        ],
    }


def _estado_bullpen(cfg: dict) -> dict:
    """Lo que ya se leyó de relevistas hoy. No sale a internet desde el health."""
    activo = bool((cfg.get("estrategia") or {}).get("analizar_bullpen", False))
    try:
        from bullpen import resumen as resumen_bullpen

        datos = resumen_bullpen()
    except Exception:
        return {"activo": activo, "ok": False}
    return {
        "activo": activo,
        "ok": bool(datos.get("ok")),
        "fuente": "statsapi",
        "ventana_dias": datos.get("ventana_dias"),
        "juegos_leidos": datos.get("juegos_leidos"),
        "equipos": datos.get("equipos"),
    }


def _persistencia_publica() -> dict:
    """Cache corta: el health check de Render no abre Postgres en cada ping."""
    ahora = time.monotonic()
    previa = _persistencia_cache.get("info")
    if isinstance(previa, dict) and ahora - float(_persistencia_cache.get("ts") or 0) < 30:
        return previa
    try:
        from memoria_store import describir_persistencia

        info = describir_persistencia(DATA_DIR)
    except Exception as e:
        info = {
            "backend": "desconocido",
            "durable": False,
            "conectado": False,
            "aviso": str(e)[:160],
        }
    _persistencia_cache["ts"] = ahora
    _persistencia_cache["info"] = info
    return info


@app.get("/api/health")
def api_health():
    """Ping para Render + cron externo (mantiene el servicio despierto en plan free).

    Ligero a propósito: Render usa este path como healthCheck. Sin wipe recovery
    ni ML — eso va en boot, /api/historial-status y /api/auto-bloqueo-externo.
    """
    cfg = cargar_config()
    mem_h = cargar_memoria()
    hist_fechas = sorted(_fechas_con_historial(mem_h))
    hist_ap, hist_pr = _contar_historial(mem_h)
    cfg_ops = cfg
    rss_mb: float | None = None
    try:
        import resource

        rss_mb = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        pass
    return {
        "ok": True,
        "servicio": "quantum-mlb",
        "persistencia": _persistencia_publica(),
        "rss_mb": rss_mb,
        "capital": mem_h.get("capital"),
        "dia_actual": mem_h.get("dia_actual"),
        "dias_totales": mem_h.get("dias_totales"),
        "experimento_activo": mem_h.get("experimento_activo", True),
        "historial": {
            "fechas": hist_fechas,
            "n_dias": len(hist_fechas),
            "apuestas_liquidadas": hist_ap,
            "preds_liquidadas": hist_pr,
            **{k: v for k, v in _resumen_sello(mem_h).items() if k not in ("fechas", "n_dias", "apuestas_liquidadas", "preds_liquidadas")},
        },
        "hora": datetime.now(tz_experimento()).isoformat(),
        "ia_veto": {
            "activo": bool(cfg.get("usar_ia_veto")),
            "listo": ia_veto_disponible(cfg),
        },
        "clima": {
            "activo": bool(cfg.get("usar_clima", True)),
            "fuente": "open-meteo",
        },
        "lesiones": {
            "activo": bool(cfg.get("usar_lesiones", True)),
            "fuente": "espn",
        },
        "calibracion": {
            "activo": bool(cfg.get("usar_calibracion", True)),
        },
        "bullpen": _estado_bullpen(cfg),
        "pitcher_avanzado": {
            "activo": True,
            "metricas": ["fip", "xfip", "k_pct", "bb_pct"],
        },
        "elo": {
            "activo": bool(cfg.get("usar_elo", True)),
            "peso_elo": float((cfg.get("elo") or {}).get("peso_elo") or 0.40),
            "home_adv": float((cfg.get("elo") or {}).get("home_adv") or 24),
        },
        "inteligencia": {
            "activo": bool(cfg.get("usar_inteligencia", True)),
            "capas": [
                "consenso_mercado",
                "bullpen_dia",
                "park_umpire",
                "tipo_pick",
                "monte_carlo",
                "mc_totales_f5",
            ],
            "peso_consenso": float((cfg.get("inteligencia") or {}).get("peso_consenso") or 0.10),
            "peso_mc": float((cfg.get("inteligencia") or {}).get("peso_mc") or 0.22),
            "mc_sims": int((cfg.get("inteligencia") or {}).get("mc_sims") or 800),
            "mc_sims_efectivos": _mc_sims_health(cfg),
            "monte_carlo_totales": bool(
                (cfg.get("inteligencia") or {}).get("monte_carlo_totales", True)
            ),
            "linea_total_default": float(
                (cfg.get("inteligencia") or {}).get("linea_total_default") or 8.5
            ),
        },
        "odds": {
            "activo": not bool(cfg.get("modo_solo_modelo")),
            "desactivado": bool(cfg.get("modo_solo_modelo")),
            "motivo": (
                "modo_solo_modelo=true (sin Odds API)"
                if cfg.get("modo_solo_modelo")
                else None
            ),
            "proveedor": (cfg.get("lineas") or {}).get("proveedor") or "espn",
            "requiere_mercado": bool((cfg.get("estrategia") or {}).get("requiere_betmgm", True))
            and not bool(cfg.get("modo_solo_modelo")),
            "fallback_internet": bool((cfg.get("lineas") or {}).get("fallback_internet", True)),
            "bookmakers": (cfg.get("lineas") or {}).get("bookmakers") or "draftkings",
            "action_network": action_network_activo(cfg),
            "min_edge_pct": float((cfg.get("estrategia") or {}).get("min_edge_pct", 6.0)),
            **_salud_momios(mem_h),
            "odds_api": estado_odds_api(cfg),
        },
        "scratch_lineup": {
            "activo": bool(cfg.get("usar_scratch_lineup", True)),
            "min_estrellas_fuera": int(
                (cfg.get("estrategia") or {}).get("min_estrellas_fuera_lineup", 2)
            ),
        },
        "factores_humanos": {
            "activo": bool(cfg.get("usar_factores_humanos", True)),
            "señales": ["viaje", "descanso", "zona", "serie", "umpire"],
        },
        "historico_oficial": {
            "activo": bool(cfg.get("usar_historico_oficial", True)),
            "señales": ["L10", "pitcher_vs_rival"],
        },
        "mente": {
            "activo": bool(cfg.get("usar_mente", True)),
            "modo": ((cfg.get("mente") or {}).get("modo") or "normal"),
            "min_confianza": int((cfg.get("mente") or {}).get("min_confianza") or 3),
            "shadow": bool((cfg.get("mente") or {}).get("shadow", False)),
        },
        "mente_errores": _resumen_mente_errores(cfg_ops),
        "vigilancia_cron_min": 5,
        "congelacion": resumen_congelacion_health(cfg),
        "xgboost": {
            "activo": bool(cfg.get("usar_xgboost", True)),
        },
    }


@app.get("/api/mente-errores")
def api_mente_errores_status():
    """Estado de la mente operativa (errores de la app, no picks)."""
    cfg = cargar_config()
    panel = verificar_panel_html(BASE_DIR / "QuantumMLB.html")
    return {
        "ok": True,
        "disponible": mente_errores_disponible(cfg),
        "panel_health": panel,
        **_resumen_mente_errores(cfg),
    }


@app.post("/api/mente-errores/cliente")
async def api_mente_errores_cliente(request: Request):
    """Errores JS del panel (Safari iOS) → mente de errores."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return registrar_error_cliente(
        str(body.get("mensaje") or ""),
        codigo=str(body.get("codigo") or "panel_js"),
        origen=str(body.get("origen") or "panel"),
        panel_ver=str(body.get("panel_ver") or "") or None,
        url=str(body.get("url") or "") or None,
    )


@app.get("/api/panel-health")
def api_panel_health():
    """Comprueba QuantumMLB.html (bugs JS que tumban predicciones)."""
    out = verificar_panel_html(BASE_DIR / "QuantumMLB.html")
    return {"ok": bool(out.get("ok")), **out}


@app.post("/api/mente-errores/ciclo", dependencies=_auth_cron())
@app.get("/api/mente-errores/ciclo", dependencies=_auth_cron())
def api_mente_errores_ciclo(forzar: bool = False):
    """Fuerza un ciclo de diagnóstico + remediación. Exige CRON_SECRET."""
    cfg = cargar_config()
    mem = cargar_memoria()
    out = ejecutar_ciclo_mente_errores(
        cfg,
        vigilancia=None,
        lineas_meta=_lineas_meta_cache if isinstance(_lineas_meta_cache, dict) else None,
        memoria=mem,
        forzar=forzar,
    )
    return out


@app.get("/api/clima-status")
def api_clima_status():
    """Ping Open-Meteo con un estadio de prueba (Coors Field)."""
    cfg = cargar_config()
    if not cfg.get("usar_clima", True):
        return {"ok": False, "activo": False, "motivo": "usar_clima=false"}
    try:
        from clima import obtener_clima_estadio

        sample = obtener_clima_estadio(115)  # Rockies / Coors
        return {
            "ok": bool(sample.get("ok")),
            "activo": True,
            "fuente": "open-meteo",
            "muestra": sample,
        }
    except Exception as e:
        return {"ok": False, "activo": True, "motivo": str(e)[:120]}


@app.get("/api/lesiones-status")
def api_lesiones_status():
    """Ping del board de lesiones ESPN."""
    cfg = cargar_config()
    if not cfg.get("usar_lesiones", True):
        return {"ok": False, "activo": False, "motivo": "usar_lesiones=false"}
    try:
        from lesiones import cargar_reporte_lesiones

        rep = cargar_reporte_lesiones()
        return {
            "ok": bool(rep.get("ok")),
            "activo": True,
            "fuente": "espn",
            "total": rep.get("total"),
            "motivo": rep.get("motivo"),
        }
    except Exception as e:
        return {"ok": False, "activo": True, "motivo": str(e)[:120]}


def _diag_clave_odds(cfg: dict) -> tuple[str | None, dict]:
    """Clave real (enmascarada) para cualquier proveedor. Nunca devuelve la clave."""
    key = cargar_api_key(cfg)
    diag = enmascarar_api_key(key)
    diag["fuente"] = origen_api_key(cfg) if key else None
    return key, diag


@app.get("/api/odds-status")
def api_odds_status():
    """Estado del proveedor de cuotas. Con modo_solo_modelo no se usa ni se exige."""
    cfg = cargar_config()
    solo = bool(cfg.get("modo_solo_modelo"))
    requiere = bool((cfg.get("estrategia") or {}).get("requiere_betmgm", True))
    proveedor = str((cfg.get("lineas") or {}).get("proveedor") or "espn").lower()
    key, diag = _diag_clave_odds(cfg)
    base = {
        "activo": not solo and requiere,
        "requiere_mercado": requiere and not solo,
        "modo_solo_modelo": solo,
        "bookmakers": (cfg.get("lineas") or {}).get("bookmakers") or "draftkings",
        "min_edge_pct": float((cfg.get("estrategia") or {}).get("min_edge_pct", 6.0)),
        "proveedor": proveedor,
        "fallback_internet": bool((cfg.get("lineas") or {}).get("fallback_internet", True)),
        "fallback_solo_modelo": bool(
            (cfg.get("estrategia") or {}).get("fallback_solo_modelo", True)
        ),
        **diag,
    }

    def _out(payload: dict) -> dict:
        return redactar_secreto(payload, key)

    if solo or not requiere:
        return _out({
            **base,
            "ok": True,
            "desactivado": True,
            "motivo": "Odds API desactivada: dinero solo con % del modelo (≥ min_prob)",
        })
    try:
        def _con_espn(out: dict) -> dict:
            if out.get("ok") or not bool((cfg.get("lineas") or {}).get("fallback_internet", True)):
                return out
            try:
                from lineas_espn import obtener_lineas_espn

                _, me = obtener_lineas_espn()
            except Exception as e:
                out["espn_error"] = str(e)[:120]
                return out
            if me.get("ok"):
                out["ok"] = True
                out["fallback_espn"] = True
                out["espn_partidos"] = me.get("partidos")
                out["mensaje"] = (
                    f"{out.get('mensaje') or out.get('motivo') or 'Cuotas no disponibles'} · "
                    f"{me.get('mensaje')}"
                )
                out["motivo"] = None
            else:
                out["fallback_espn"] = False
                out["espn_mensaje"] = me.get("mensaje")
            return out

        if proveedor in ("espn", "espn-draftkings", "internet"):
            from lineas_espn import obtener_lineas_espn

            _, me = obtener_lineas_espn()
            return _out({
                **base,
                "ok": bool(me.get("ok")),
                "fallback_espn": True,
                "partidos": me.get("partidos"),
                "mensaje": me.get("mensaje"),
            })

        # Legacy The Odds API (proveedor betmgm / the-odds-api)
        from lineas_betmgm import obtener_lineas_betmgm

        if not key:
            return _out(_con_espn({
                **base,
                "ok": False,
                "motivo": "Falta ODDS_API_KEY · se intenta ESPN/DraftKings",
                "ayuda": (
                    "Crea key en https://the-odds-api.com → pégala en Render "
                    "como ODDS_API_KEY (sin comillas) → Save → Manual Deploy."
                ),
            }))
        _, meta = obtener_lineas_betmgm(cfg)
        return _out(_con_espn({
            **base,
            "ok": bool(meta.get("ok")),
            "partidos": meta.get("partidos"),
            "mensaje": meta.get("mensaje"),
            "error_code": meta.get("error_code"),
            "http_status": meta.get("http_status"),
            "ayuda": meta.get("ayuda"),
            "requests_restantes": meta.get("requests_restantes"),
            "cache": meta.get("cache"),
        }))
    except Exception as e:
        return _out({**base, "ok": False, "motivo": str(e)[:120]})


@app.get("/api/scratch-status")
def api_scratch_status():
    """Estado del módulo scratch/lineup (sin llamar a StatsAPI pesado)."""
    cfg = cargar_config()
    activo = bool(cfg.get("usar_scratch_lineup", True))
    if not activo:
        return {"ok": False, "activo": False, "motivo": "usar_scratch_lineup=false"}
    try:
        from lineup_scratch import analizar_scratch_lineup, pick_afectado_por_scratch

        demo = analizar_scratch_lineup(
            away_id=None,
            home_id=None,
            pitcher_away_id=111,
            pitcher_home_id=222,
            pitcher_away_nombre="Demo A",
            pitcher_home_nombre="Demo B",
            lineups={"away": [], "home": [], "confirmado": False},
            season=int(cfg.get("temporada_mlb") or 2026),
            pred_congelada={
                "pitcher_away_id": 111,
                "pitcher_home_id": 999,
                "pitcherAway": "Demo A",
                "pitcherHome": "Otro",
            },
            min_estrellas_fuera=int(
                (cfg.get("estrategia") or {}).get("min_estrellas_fuera_lineup", 2)
            ),
        )
        return {
            "ok": True,
            "activo": True,
            "min_estrellas_fuera": int(
                (cfg.get("estrategia") or {}).get("min_estrellas_fuera_lineup", 2)
            ),
            "demo_scratch_home": bool(demo.get("scratch_home")),
            "demo_riesgo": bool(demo.get("riesgo")),
            "pick_helper": pick_afectado_por_scratch is not None,
        }
    except Exception as e:
        return {"ok": False, "activo": True, "motivo": str(e)[:120]}


@app.get("/api/humanos-status")
def api_humanos_status():
    """Ping de factores humanos (viaje / serie / umpire)."""
    cfg = cargar_config()
    if not cfg.get("usar_factores_humanos", True):
        return {"ok": False, "activo": False, "motivo": "usar_factores_humanos=false"}
    try:
        from factores_humanos import analizar_factores_humanos

        demo = analizar_factores_humanos(
            {
                "away_id": 119,
                "home_id": 147,
                "inicio_juego": "2026-08-12T23:05:00+00:00",
                "series_game_number": 3,
                "games_in_series": 3,
                "day_night": "night",
                "officials": [
                    {
                        "official": {"id": 1, "fullName": "Pat Hoberg"},
                        "officialType": "Home Plate",
                    }
                ],
            }
        )
        return {
            "ok": bool(demo.get("ok")),
            "activo": True,
            "señales": ["viaje", "descanso", "zona", "serie", "umpire"],
            "demo_resumen": (demo.get("resumen") or "")[:160],
            "demo_umpire": (demo.get("umpire") or {}).get("hp_nombre"),
        }
    except Exception as e:
        return {"ok": False, "activo": True, "motivo": str(e)[:120]}


@app.get("/api/historico-status")
def api_historico_status():
    """Ping L10 + pitcher vs rival (StatsAPI oficial)."""
    cfg = cargar_config()
    if not cfg.get("usar_historico_oficial", True):
        return {"ok": False, "activo": False, "motivo": "usar_historico_oficial=false"}
    try:
        from historico_oficial import cargar_l10, analizar_historico_oficial

        season = int(cfg.get("temporada_mlb") or 2026)
        l10 = cargar_l10(season)
        demo = analizar_historico_oficial(
            {
                "away_id": 136,
                "home_id": 147,
                "pitcher_away_id": 669358,
                "pitcher_home_id": 543037,
                "fecha": f"{season}-08-12",
            },
            season=season,
        )
        return {
            "ok": bool(demo.get("ok")),
            "activo": True,
            "señales": ["L10", "pitcher_vs_rival"],
            "equipos_l10": len(l10),
            "demo_resumen": (demo.get("resumen") or "")[:180],
        }
    except Exception as e:
        return {"ok": False, "activo": True, "motivo": str(e)[:120]}


@app.get("/api/mente-red")
def api_mente_red():
    """Grafo de la mente para la carpeta local mente/index.html (no va en el panel)."""
    cfg = cargar_config()
    memoria = cargar_memoria()
    lecciones_meta = None
    mente_stats_meta = None
    try:
        from ia_lecciones import resumen_lecciones

        lecciones_meta = resumen_lecciones(memoria)
    except Exception:
        pass
    try:
        from mente_aprendizaje import resumen_mente_stats

        mente_stats_meta = resumen_mente_stats(memoria)
    except Exception:
        pass
    red = _construir_mente_red_panel(cfg, memoria, lecciones_meta, mente_stats_meta)
    return {"ok": bool(red.get("ok")), **red}


@app.get("/api/mente-skills")
def api_mente_skills():
    """Reporte de auto-evolución: habilidad activa, motivo y prueba."""
    try:
        from mente_skills import reporte_auto_evolucion

        return reporte_auto_evolucion(cargar_memoria())
    except Exception as e:
        return {"ok": False, "titulo": "Reporte de Auto-Evolución", "motivo": str(e)[:120]}


@app.get("/api/mente-bitacora")
def api_mente_bitacora():
    """Notas de investigación y cambios para la red en /mente."""
    try:
        from mente_bitacora import resumen_bitacora

        return resumen_bitacora()
    except Exception as e:
        return {"ok": False, "total": 0, "entradas": [], "motivo": str(e)[:120]}


@app.get("/api/mente-status")
def api_mente_status():
    """Estado de la mente (director APOSTAR/PASAR/ESPERAR)."""
    cfg = cargar_config()
    mente_cfg = cfg.get("mente") if isinstance(cfg.get("mente"), dict) else {}
    base = {
        "activo": bool(cfg.get("usar_mente", True)),
        "modo": mente_cfg.get("modo") or "normal",
        "min_confianza": int(mente_cfg.get("min_confianza") or 3),
        "shadow": bool(mente_cfg.get("shadow", False)),
        "groq": bool(ia_veto_disponible({**cfg, "usar_ia_veto": True}) or os.environ.get("GROQ_API_KEY")),
    }
    if not base["activo"]:
        return {**base, "ok": False, "motivo": "usar_mente=false"}
    try:
        demo = mente_conclusion(
            {
                "id": "mente-demo",
                "visitante": "Away Demo",
                "home": "Home Demo",
                "pick": "Home Demo ML",
                "probPick": 62,
                "edge": 7.5,
                "odds": 1.9,
                "lineas_fuente": "draftkings",
                "pitcherAway": "A",
                "pitcherHome": "B",
            },
            cfg,
            {},
            forzar=True,
            solo_local=True,
        )
        return {**base, "ok": True, "demo": {
            "decision": demo.get("decision"),
            "confianza": demo.get("confianza"),
            "autoriza_dinero": demo.get("autoriza_dinero"),
            "fuente": demo.get("fuente"),
        }}
    except Exception as e:
        return {**base, "ok": False, "motivo": str(e)[:120]}


@app.get("/api/calib-status")
def api_calib_status():
    """Estado del calibrador de probabilidades."""
    cfg = cargar_config()
    if not cfg.get("usar_calibracion", True):
        return {"ok": False, "activo": False, "motivo": "usar_calibracion=false"}
    try:
        from calibracion import meta_calibracion, cargar_calibrador, entrenar_calibrador

        cargar_calibrador()
        meta = meta_calibracion()
        mem_meta = cargar_memoria().get("calib_meta") or {}
        # Si aún no hay calibrador en disco pero hay historial, intenta entrenar
        if not meta.get("ok"):
            meta = entrenar_calibrador(cargar_memoria(), min_muestras=30)
            if meta.get("ok"):
                m = cargar_memoria()
                m["calib_meta"] = meta
                guardar_memoria(m)
        return {
            "ok": bool(meta.get("ok")),
            "activo": True,
            **meta,
            "memoria": mem_meta,
        }
    except Exception as e:
        return {"ok": False, "activo": True, "motivo": str(e)[:120]}


@app.get("/api/pitcher-demo")
def api_pitcher_demo():
    """Muestra FIP/xFIP/K%/BB% de un pitcher de prueba (Skubal)."""
    try:
        from modelo_mlb import stats_pitcher

        cfg = cargar_config()
        p = stats_pitcher(669373, int(cfg.get("temporada_mlb") or 2026))
        return {
            "ok": True,
            "pitcher": p.get("nombre"),
            "era": p.get("era"),
            "fip": p.get("fip"),
            "xfip": p.get("xfip"),
            "k_pct": p.get("k_pct"),
            "bb_pct": p.get("bb_pct"),
            "fuente": p.get("metricas_fuente"),
        }
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:120]}


@app.get("/api/ia-status")
def api_ia_status():
    """Comprueba config + ping Groq (sin exponer la key)."""
    cfg = cargar_config()
    lecciones_n = 0
    try:
        from ia_lecciones import asegurar_lista_lecciones, max_lecciones_almacenadas, max_lecciones_prompt

        lecciones_n = len(asegurar_lista_lecciones(cargar_memoria()))
        max_lec = max_lecciones_almacenadas(cfg.get("aprendizaje"))
        max_prompt = max_lecciones_prompt(cfg.get("aprendizaje"))
    except Exception:
        max_lec = 80
        max_prompt = 8
    base = {
        "activo": bool(cfg.get("usar_ia_veto")),
        "key_presente": ia_veto_disponible(cfg),
        "modelo": modelo_groq(cfg),
        "lecciones": lecciones_n,
        "max_lecciones": max_lec,
        "max_lecciones_prompt": max_prompt,
    }
    if not base["activo"]:
        return {**base, "ok": False, "motivo": "usar_ia_veto=false en config"}
    if not base["key_presente"]:
        return {**base, "ok": False, "motivo": "Falta GROQ_API_KEY en Render"}
    ping = probar_conexion_groq(cfg)
    return {**base, **ping}


def ejecutar_trabajo_cron_externo() -> dict:
    """Sincroniza fecha, predicciones, bloqueos y liquidacion."""
    try:
        _intentar_recuperar_wipe()
    except Exception as e:
        print(f"[CRON] restore wipe: {e}")
    sincronizar_experimento_a_hoy()
    reparar_odds_papel(cargar_memoria())
    rellenar_predicciones_recientes(cargar_memoria(), dias_atras=7)
    cfg_cron = cargar_config()
    cuotas_pre = precalentar_cuotas_mercado(cfg_cron)
    if _mercado_requiere_cuotas(cfg_cron) and not cuotas_pre.get("ok"):
        try:
            ejecutar_ciclo_mente_errores(
                cfg_cron,
                lineas_meta=_lineas_meta_cache if isinstance(_lineas_meta_cache, dict) else None,
                memoria=cargar_memoria(),
            )
            precalentar_cuotas_mercado(cfg_cron)
        except Exception as e:
            print(f"[CRON] remediar cuotas: {e}")
    if _mercado_requiere_cuotas(cfg_cron):
        try:
            refrescar_cuotas_pendientes_hoy(cfg_cron)
        except Exception as e:
            print(f"[CRON] upgrade cuotas papel: {e}")
    programar_bloqueos_por_juego()
    registrar_predicciones_del_dia(forzar=False)
    resultado = bloquear_apuestas_del_dia(forzar=False)
    liquidar_todo(cargar_memoria())
    memoria = cargar_memoria()
    cfg = cargar_config()
    # Vigilancia real (antes el cron pasaba vigilancia=None → mente ciega al sueño)
    vigilancia: dict = {}
    try:
        juegos = obtener_juegos_fecha(fecha_str())
        vigilancia = vigilancia_t60(juegos, memoria, cfg)
        if vigilancia.get("nivel") == "alerta" and int(vigilancia.get("total_riesgo") or 0) > 0:
            registrar_predicciones_del_dia(forzar=True)
            try:
                bloquear_apuestas_del_dia(forzar=False)
            except Exception:
                pass
            memoria = cargar_memoria()
            vigilancia = vigilancia_t60(juegos, memoria, cfg)
    except Exception as e:
        print(f"[CRON] vigilancia: {e}")
        vigilancia = {"ok": False, "nivel": "ok", "mensaje": str(e)[:120]}
    mente_err: dict = {}
    try:
        mente_err = ejecutar_ciclo_mente_errores(
            cfg,
            vigilancia=vigilancia if isinstance(vigilancia, dict) else None,
            lineas_meta=_lineas_meta_cache if isinstance(_lineas_meta_cache, dict) else None,
            memoria=memoria,
        )
    except Exception as e:
        print(f"[MENTE-ERRORES] ciclo cron: {e}")
        try:
            registrar_error_runtime("cron", str(e))
        except Exception:
            pass
    import_meta = None
    try:
        cong_cron = resumen_congelacion_health(cfg)
        n_perd = int(cong_cron.get("ventanas_perdidas") or 0)
        if n_perd:
            print(
                f"[CONGELAR] alerta: {n_perd} partido(s) empezaron sin pick congelado"
            )
    except Exception as e:
        print(f"[CONGELAR] resumen cron: {e}")
    try:
        import_meta = _intentar_import_aprendizaje_repo_automatico()
    except Exception as e:
        print(f"[CRON] import aprendizaje: {e}")
    try:
        from inteligencia_mlb import limpiar_caches_inteligencia

        limpiar_caches_inteligencia()
    except Exception:
        pass
    return {
        "ok": True,
        "mensaje": "Auto-bloqueo ejecutado",
        "resultado": resultado,
        "capital": memoria["capital"],
        "dia_actual": memoria.get("dia_actual"),
        "fecha_hoy": fecha_str(),
        "import_aprendizaje": import_meta,
        "vigilancia": {
            "nivel": (vigilancia or {}).get("nivel"),
            "total_riesgo": (vigilancia or {}).get("total_riesgo"),
            "total_perdidos": (vigilancia or {}).get("total_perdidos"),
            "mensaje": (vigilancia or {}).get("mensaje"),
        },
        "mente_errores": {
            "nivel": (mente_err or {}).get("nivel"),
            "mensaje": (mente_err or {}).get("mensaje"),
            "n_hallazgos": len((mente_err or {}).get("hallazgos") or []),
        },
        "congelacion": resumen_congelacion_health(cfg),
    }


def _cron_externo_en_fondo() -> None:
    global _cron_externo_activo
    try:
        ejecutar_trabajo_cron_externo()
    except Exception as e:
        print(f"[CRON] Error en trabajo externo: {e}")
        try:
            registrar_error_runtime("cron_fondo", str(e))
        except Exception:
            pass
    finally:
        _cron_externo_activo = False
        try:
            gc.collect()
        except Exception:
            pass


@app.get("/api/auto-bloqueo-externo", dependencies=_auth_cron())
@app.post("/api/auto-bloqueo-externo", dependencies=_auth_cron())
def api_auto_bloqueo_externo(en_fondo: bool = True):
    """
    Para GitHub Actions u otro cron externo (cada 5-10 min).
    Por defecto responde al instante y corre en segundo plano (en_fondo=1).
    Exige CRON_SECRET (header X-Cron-Secret, Bearer o ?secret=).
    Sin la variable de entorno responde 503.
    """
    global _cron_externo_activo
    if en_fondo:
        with _cron_externo_lock:
            if _cron_externo_activo:
                return {"ok": True, "mensaje": "Cron ya en ejecucion", "en_fondo": True}
            _cron_externo_activo = True
            threading.Thread(target=_cron_externo_en_fondo, daemon=True).start()
        return {"ok": True, "mensaje": "Cron iniciado en segundo plano", "en_fondo": True}
    try:
        return ejecutar_trabajo_cron_externo()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/exportar-memoria", dependencies=_auth_cron())
def api_exportar_memoria():
    """Descarga el documento de la base (backup). Exige CRON_SECRET."""
    memoria = cargar_memoria()
    return memoria


@app.post("/api/subir-memoria", dependencies=_auth_cron())
def api_subir_memoria(
    payload: dict,
    modo: str | None = None,
):
    """Sube memoria_auditoria.json desde la PC local a Render (exige CRON_SECRET).

    modo=fusionar (default, une días) | replace | aprendizaje (paper retroactivo)
    """
    if not isinstance(payload, dict) or "capital" not in payload:
        raise HTTPException(status_code=400, detail="JSON de memoria invalido")
    modo_n = (modo or "fusionar").lower()
    if modo_n in ("aprendizaje", "import"):
        from ia_importar import importar_dump_aprendizaje
        from ia_lecciones import escanear_experiencias_negativas

        memoria = cargar_memoria()
        stats = importar_dump_aprendizaje(memoria, payload)
        n_lec = escanear_experiencias_negativas(memoria)
        try:
            auto_entrenar_ml(memoria)
        except Exception as e:
            print(f"[ML] import: {e}")
        guardar_memoria(memoria)
        return {
            "ok": True,
            "modo": "aprendizaje",
            "import": stats,
            "lecciones_nuevas": n_lec,
            "capital": memoria.get("capital"),
            "dias": len(memoria.get("dias", [])),
        }
    if modo_n in ("fusionar", "merge", "union"):
        disk = cargar_memoria()
        merged = _fusionar_memoria(payload, disk)
        guardar_memoria(merged)
        sincronizar_experimento_a_hoy(merged)
        memoria = cargar_memoria()
        ap, pr = _contar_historial(memoria)
        return {
            "ok": True,
            "modo": "fusionar",
            "capital": memoria.get("capital"),
            "dia_actual": memoria.get("dia_actual"),
            "dias": len(memoria.get("dias", [])),
            "historial": {"apuestas": ap, "preds": pr},
        }
    guardar_memoria(payload)
    memoria = cargar_memoria()
    return {
        "ok": True,
        "modo": "replace",
        "capital": memoria.get("capital"),
        "dia_actual": memoria.get("dia_actual"),
        "dias": len(memoria.get("dias", [])),
    }


def _intentar_import_aprendizaje_repo_automatico() -> dict | None:
    """
    Si el disco tiene menos lecciones que memoria_auditoria.json del repo, importa.
    Idempotente — no requiere CRON_SECRET (solo compara counts locales).
    """
    global _import_auto_ok_ts
    if _en_render() and (time.monotonic() - _import_auto_ok_ts) < _IMPORT_AUTO_TTL_SEC:
        return None
    origen = BASE_DIR / "memoria_auditoria.json"
    if not origen.exists():
        return None
    try:
        bundled = json.loads(origen.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[IMPORT-AUTO] backup ilegible: {e}")
        return None
    if not isinstance(bundled, dict):
        return None
    n_bundle = len(bundled.get("lecciones") or [])
    n_bundle_preds = sum(
        1
        for d in bundled.get("dias") or []
        for p in (d.get("predicciones") or [])
        if isinstance(p, dict) and p.get("estado") == "liquidado"
    )
    memoria = cargar_memoria(force=True)
    n_disk = len(memoria.get("lecciones") or [])
    n_disk_preds = sum(
        1
        for d in memoria.get("dias") or []
        for p in (d.get("predicciones") or [])
        if isinstance(p, dict) and p.get("estado") == "liquidado"
    )
    # Ya sincronizado
    if n_disk >= n_bundle - 2 and n_disk_preds >= n_bundle_preds - 2:
        _import_auto_ok_ts = time.monotonic()
        return None
    print(
        f"[IMPORT-AUTO] disco lecciones={n_disk} preds_liq={n_disk_preds} "
        f"· repo lecciones={n_bundle} preds_liq={n_bundle_preds} → importando"
    )
    with _memoria_lock:
        memoria = cargar_memoria(force=True)
        try:
            out = _ejecutar_import_aprendizaje(memoria, bundled)
            n_after = len(cargar_memoria(force=True).get("lecciones") or [])
            print(f"[IMPORT-AUTO] OK · lecciones ahora {n_after}")
            return out
        except HTTPException as e:
            print(f"[IMPORT-AUTO] omitido: {e.detail}")
            return None
        except Exception as e:
            print(f"[IMPORT-AUTO] fallo: {e}")
            return None


def _ejecutar_import_aprendizaje(memoria: dict, dump: dict | None = None, *, experiencias: list | None = None) -> dict:
    """Fusiona dump/experiencias retroactivas, escanea lecciones y reentrena ML."""
    from ia_importar import importar_dump_aprendizaje, importar_experiencias_lista
    from ia_lecciones import escanear_experiencias_negativas, resumen_lecciones

    stats: dict = {}
    if isinstance(dump, dict):
        stats["dump"] = importar_dump_aprendizaje(memoria, dump)
    if isinstance(experiencias, list):
        stats["lista"] = importar_experiencias_lista(memoria, experiencias)
    if not stats:
        raise HTTPException(
            status_code=400,
            detail="Nada que importar (dump o experiencias vacíos)",
        )

    n_lec = escanear_experiencias_negativas(memoria)
    try:
        ml = auto_entrenar_ml(memoria)
    except Exception as e:
        ml = {"ok": False, "mensaje": str(e)}
    guardar_memoria(memoria)
    return {
        "ok": True,
        "import": stats,
        "lecciones_procesadas": n_lec,
        "lecciones": resumen_lecciones(memoria),
        "ml_meta": ml,
        "dias": len(memoria.get("dias") or []),
    }


@app.post("/api/importar-aprendizaje", dependencies=_auth_cron())
def api_importar_aprendizaje(payload: dict):
    """
    Plan 4: importa pasado para aprender (no infla WR del panel).

    Body:
      - dump completo de memoria, o
      - {"memoria": {...}} dump, o
      - {"experiencias": [ {...}, ... ]}
    Exige CRON_SECRET.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON invalido")

    memoria = cargar_memoria()
    dump = payload.get("memoria") if isinstance(payload.get("memoria"), dict) else None
    if dump is None and "capital" in payload and "dias" in payload:
        dump = payload
    exp = payload.get("experiencias") if isinstance(payload.get("experiencias"), list) else None
    return _ejecutar_import_aprendizaje(memoria, dump, experiencias=exp)


@app.post("/api/importar-aprendizaje-repo", dependencies=_auth_cron())
@app.get("/api/importar-aprendizaje-repo", dependencies=_auth_cron())
def api_importar_aprendizaje_repo():
    """
    Importa lecciones y preds retroactivos desde memoria_auditoria.json del repo.
    No sube capital ni infla WR del panel (solo aprendizaje + ML).
    Exige CRON_SECRET.
    """
    if _cron_externo_activo:
        return {
            "ok": True,
            "omitido": "cron_activo",
            "mensaje": "Import omitido: el cron ya corre (anti-OOM)",
        }
    origen = BASE_DIR / "memoria_auditoria.json"
    if not origen.exists():
        raise HTTPException(status_code=404, detail="No hay memoria_auditoria.json en el servidor")
    try:
        bundled = json.loads(origen.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup ilegible: {e}") from e
    if not isinstance(bundled, dict) or "dias" not in bundled:
        raise HTTPException(status_code=400, detail="memoria_auditoria.json inválido")

    memoria = cargar_memoria()
    antes = len((memoria.get("lecciones") or []))
    out = _ejecutar_import_aprendizaje(memoria, bundled)
    despues = len((cargar_memoria().get("lecciones") or []))
    out["fuente"] = str(origen.name)
    out["lecciones_antes"] = antes
    out["lecciones_despues"] = despues
    out["lecciones_nuevas"] = max(0, despues - antes)
    return out


@app.post("/api/procesar-experiencias", dependencies=_auth_cron())
@app.get("/api/procesar-experiencias", dependencies=_auth_cron())
def api_procesar_experiencias(forzar: bool = False):
    """
    Escanea histórico: lecciones negativas + contadores de aprendizaje de la mente.
    forzar=1 ignora flags de backfill previo.
    """
    from ia_lecciones import (
        escanear_experiencias_negativas,
        resumen_lecciones,
    )
    from mente_aprendizaje import recomputar_stats_desde_historial, resumen_mente_stats

    memoria = cargar_memoria()
    if forzar:
        memoria.pop("experiencias_negativas_backfill_hecho", None)
        memoria.pop("lecciones_backfill_hecho", None)
    n = escanear_experiencias_negativas(memoria)
    n_stats = recomputar_stats_desde_historial(memoria)
    memoria["experiencias_negativas_backfill_hecho"] = True
    memoria["lecciones_backfill_hecho"] = True
    guardar_memoria(memoria)
    meta = resumen_lecciones(memoria)
    return {
        "ok": True,
        "nuevas": n,
        "mente_stats_recomputados": n_stats,
        "lecciones": meta,
        "por_patron": meta.get("por_patron") or {},
        "mente_stats": resumen_mente_stats(memoria),
    }


@app.post("/api/restaurar-backup", dependencies=_auth_cron())
def api_restaurar_backup():
    """Fusiona el JSON del repo con el disco si faltan días (wipe o redeploy)."""
    origen = BASE_DIR / "memoria_auditoria.json"
    if not origen.exists():
        raise HTTPException(status_code=404, detail="No hay memoria_auditoria.json en el repo")
    try:
        bundled = json.loads(origen.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup ilegible: {e}") from e
    disk = cargar_memoria()
    if disk.get("reinicio_manual"):
        ap, pr = _contar_historial(disk)
        return {
            "ok": False,
            "motivo": "reinicio_manual=true; no se restaura el backup",
            "dia_actual": disk.get("dia_actual"),
            "capital": disk.get("capital"),
            "historial": {"apuestas": ap, "preds": pr},
        }
    wipe_clasico = _memoria_parece_reinicio(disk)
    dias_perdidos = _backup_tiene_dias_que_el_disco_perdio(bundled, disk)
    if not wipe_clasico and not dias_perdidos:
        ap, pr = _contar_historial(disk)
        return {
            "ok": True,
            "motivo": "Nada que restaurar; el disco ya tiene los días del backup",
            "dia_actual": disk.get("dia_actual"),
            "capital": disk.get("capital"),
            "historial": {"apuestas": ap, "preds": pr},
        }
    merged = _fusionar_memoria(bundled, disk)
    try:
        from ia_lecciones import escanear_experiencias_negativas

        escanear_experiencias_negativas(merged)
    except Exception as e:
        print(f"[LECCIONES] restore: {e}")
    guardar_memoria(merged)
    sincronizar_experimento_a_hoy(merged)
    memoria = cargar_memoria()
    ap, pr = _contar_historial(memoria)
    return {
        "ok": True,
        "capital": memoria.get("capital"),
        "dia_actual": memoria.get("dia_actual"),
        "dias": len(memoria.get("dias", [])),
        "historial": {"apuestas": ap, "preds": pr},
        "lecciones": len(memoria.get("lecciones") or []),
        "wipe_clasico": wipe_clasico,
        "dias_perdidos": dias_perdidos,
    }


@app.post("/api/avanzar-dia", dependencies=_auth_cron())
def api_avanzar_dia():
    """Fuerza sincronización del experimento a la fecha real."""
    memoria = sincronizar_experimento_a_hoy()
    try:
        programar_bloqueos_por_juego()
    except Exception:
        pass
    return {
        "ok": True,
        "nuevo_dia": memoria["dia_actual"],
        "fecha_hoy": fecha_str(),
    }


if __name__ == "__main__":
    print("=" * 60)
    print("  QUANTUM MLB — Experimento 10 días")
    print("  Panel: http://localhost:8000")
    print(
        "  Congelado: "
        + "/".join(f"T-{m}" for m in ventanas_congelacion(cargar_config()))
        + f" · dinero {cargar_config().get('minutos_antes_juego', 60)} min antes"
    )
    print(f"  Stake fijo: ${apuesta_fija_dolares(cargar_config()):.2f} por juego")
    print("=" * 60)
    
    try:
        import os
        port = int(os.environ.get("PORT", 8000))
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print("\n" + "!"*60)
        print(f"ERROR AL INICIAR EL SERVIDOR: {e}")
        if "address already in use" in str(e).lower():
            print("Sugerencia: El puerto 8000 ya está siendo usado por otro programa.")
        print("!"*60)
        input("\nPresiona ENTER para cerrar...")
