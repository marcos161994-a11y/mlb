"""
Mente de errores — director operativo de la aplicación (no decide apuestas).

Detecta fallos recurrentes (OddsPapi quemado, circuito, Telegram caído,
T-60 sin congelar, juegos perdidos por sueño Render, shadow accidental)
y aplica remediaciones seguras:
  - forzar proveedor ESPN + fallback internet
  - respetar/abrir circuito OddsPapi
  - apagar shadow de la mente de picks
  - forzar registro T-60 al despertar
  - registrar incidentes en DATA_DIR
  - avisar por Telegram con cooldown (opcional)

No inventa picks ni mueve dinero.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
STATE_FILE = "mente_errores.json"
MAX_INCIDENTES = 40
DEFAULT_COOLDOWN_ALERTA_MIN = 360

# Acciones permitidas (solo ops)
ACCION_FORZAR_ESPN = "forzar_espn"
ACCION_ACTIVAR_FALLBACK = "activar_fallback_internet"
ACCION_APAGAR_SHADOW = "apagar_shadow"
ACCION_RESPETAR_CIRCUITO = "respetar_circuito"
ACCION_RESTAURAR_TELEGRAM = "restaurar_telegram"
ACCION_FORZAR_REGISTRO_T60 = "forzar_registro_t60"
ACCION_RESTAURAR_HISTORIAL = "restaurar_historial"
ACCION_NOTIFICAR = "notificar"
ACCION_REGISTRAR = "registrar"


def _ahora() -> datetime:
    return datetime.now()


def _iso(dt: datetime | None = None) -> str:
    return (dt or _ahora()).isoformat(timespec="seconds")


def _state_path() -> Path:
    d = Path(os.environ.get("DATA_DIR") or str(DATA_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d / STATE_FILE


def _leer_estado() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {
            "incidentes": [],
            "overrides": {},
            "cooldowns": {},
            "ultimo_ciclo": None,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "incidentes": [],
            "overrides": {},
            "cooldowns": {},
            "ultimo_ciclo": None,
        }
    if not isinstance(data, dict):
        return {
            "incidentes": [],
            "overrides": {},
            "cooldowns": {},
            "ultimo_ciclo": None,
        }
    data.setdefault("incidentes", [])
    data.setdefault("overrides", {})
    data.setdefault("cooldowns", {})
    return data


def _guardar_estado(estado: dict[str, Any]) -> None:
    path = _state_path()
    estado["actualizado_en"] = _iso()
    path.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


def _cfg_mente_errores(cfg: dict | None) -> dict[str, Any]:
    cfg = cfg or {}
    bloque = cfg.get("mente_errores") if isinstance(cfg.get("mente_errores"), dict) else {}
    activo = bool(cfg.get("usar_mente_errores", True))
    if "activo" in bloque:
        activo = bool(bloque.get("activo"))
    return {
        "activo": activo,
        "auto_remediar": bool(bloque.get("auto_remediar", True)),
        "notificar": bool(bloque.get("notificar", True)),
        "cooldown_alerta_min": int(
            bloque.get("cooldown_alerta_min") or DEFAULT_COOLDOWN_ALERTA_MIN
        ),
    }


def mente_errores_disponible(cfg: dict | None = None) -> bool:
    return bool(_cfg_mente_errores(cfg).get("activo"))


def overrides_activos() -> dict[str, Any]:
    est = _leer_estado()
    ov = est.get("overrides") if isinstance(est.get("overrides"), dict) else {}
    return dict(ov)


def aplicar_overrides_config(cfg: dict) -> dict:
    """Fusiona overrides de remediación sobre la config cargada (solo lectura en caliente)."""
    ov = overrides_activos()
    if not ov:
        return cfg
    out = dict(cfg)
    lineas = dict(out.get("lineas") or {})
    mente = dict(out.get("mente") or {})
    if "lineas.proveedor" in ov:
        lineas["proveedor"] = ov["lineas.proveedor"]
    if "lineas.fallback_internet" in ov:
        lineas["fallback_internet"] = bool(ov["lineas.fallback_internet"])
    if "mente.shadow" in ov:
        mente["shadow"] = bool(ov["mente.shadow"])
    if "mente.modo" in ov:
        mente["modo"] = ov["mente.modo"]
    out["lineas"] = lineas
    out["mente"] = mente
    out["_mente_errores_overrides"] = dict(ov)
    return out


def _set_override(estado: dict, clave: str, valor: Any) -> None:
    ov = estado.setdefault("overrides", {})
    if not isinstance(ov, dict):
        ov = {}
        estado["overrides"] = ov
    ov[clave] = valor


def _push_incidente(estado: dict, incidente: dict[str, Any]) -> None:
    lista = estado.setdefault("incidentes", [])
    if not isinstance(lista, list):
        lista = []
        estado["incidentes"] = lista
    lista.append(incidente)
    if len(lista) > MAX_INCIDENTES:
        estado["incidentes"] = lista[-MAX_INCIDENTES:]


def _cooldown_ok(estado: dict, clave: str, minutos: int) -> bool:
    cds = estado.get("cooldowns") if isinstance(estado.get("cooldowns"), dict) else {}
    raw = cds.get(clave)
    if not raw:
        return True
    try:
        prev = datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return True
    return _ahora() >= prev + timedelta(minutes=max(1, minutos))


def _marcar_cooldown(estado: dict, clave: str) -> None:
    cds = estado.setdefault("cooldowns", {})
    if not isinstance(cds, dict):
        cds = {}
        estado["cooldowns"] = cds
    cds[clave] = _iso()


def registrar_error_runtime(origen: str, mensaje: str, codigo: str = "runtime") -> dict:
    """Registra un error capturado en cron/servidor (sin remediación completa)."""
    estado = _leer_estado()
    inc = {
        "hora": _iso(),
        "codigo": codigo,
        "severidad": "alta" if codigo == "runtime" else "media",
        "origen": (origen or "app")[:40],
        "mensaje": (mensaje or "")[:220],
        "acciones": [ACCION_REGISTRAR],
    }
    _push_incidente(estado, inc)
    _guardar_estado(estado)
    return inc


def diagnosticar(
    cfg: dict | None = None,
    *,
    vigilancia: dict | None = None,
    lineas_meta: dict | None = None,
) -> list[dict[str, Any]]:
    """Devuelve hallazgos (sin aplicar aún)."""
    cfg = cfg or {}
    hallazgos: list[dict[str, Any]] = []
    lineas = cfg.get("lineas") if isinstance(cfg.get("lineas"), dict) else {}
    mente = cfg.get("mente") if isinstance(cfg.get("mente"), dict) else {}
    proveedor = str(lineas.get("proveedor") or "oddspapi").lower()
    fallback = bool(lineas.get("fallback_internet", True))

    circ: dict[str, Any] = {"abierto": False}
    try:
        from lineas_oddspapi import estado_circuito

        circ = estado_circuito() or {"abierto": False}
    except Exception as e:
        hallazgos.append(
            {
                "codigo": "circuito_lectura",
                "severidad": "baja",
                "mensaje": f"No se pudo leer circuito OddsPapi: {e}"[:160],
                "acciones": [ACCION_REGISTRAR],
            }
        )

    if circ.get("abierto"):
        hallazgos.append(
            {
                "codigo": "oddspapi_circuito",
                "severidad": "alta",
                "mensaje": (
                    f"OddsPapi en pausa hasta {circ.get('hasta_hora') or 'luego'}: "
                    f"{(circ.get('mensaje') or 'auth/rate/red')[:100]}"
                ),
                "acciones": [
                    ACCION_RESPETAR_CIRCUITO,
                    ACCION_FORZAR_ESPN,
                    ACCION_ACTIVAR_FALLBACK,
                    ACCION_NOTIFICAR,
                ],
                "meta": {
                    "hasta_hora": circ.get("hasta_hora"),
                    "http_status": circ.get("http_status"),
                },
            }
        )

    if proveedor in ("oddspapi", "odds-papi", "odds_papi") and circ.get("abierto"):
        hallazgos.append(
            {
                "codigo": "proveedor_oddspapi_activo_con_circuito",
                "severidad": "alta",
                "mensaje": "Proveedor sigue en OddsPapi con circuito abierto · conviene ESPN",
                "acciones": [ACCION_FORZAR_ESPN, ACCION_ACTIVAR_FALLBACK],
            }
        )

    if not fallback:
        hallazgos.append(
            {
                "codigo": "fallback_internet_off",
                "severidad": "media",
                "mensaje": "fallback_internet=false · si OddsPapi falla no hay ESPN",
                "acciones": [ACCION_ACTIVAR_FALLBACK],
            }
        )

    if bool(mente.get("shadow")) or str(mente.get("modo") or "").lower() == "shadow":
        hallazgos.append(
            {
                "codigo": "mente_shadow",
                "severidad": "alta",
                "mensaje": "Mente de picks en shadow · bloquearía el dinero de todas",
                "acciones": [ACCION_APAGAR_SHADOW, ACCION_NOTIFICAR],
            }
        )

    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    if bool(tg.get("activo", True)):
        try:
            from whatsapp_alerta import telegram_disponible

            st_tg = telegram_disponible(cfg) or {}
            # telegram_disponible usa "ok" (no "listo")
            if not st_tg.get("ok"):
                hallazgos.append(
                    {
                        "codigo": "telegram_no_listo",
                        "severidad": "alta",
                        "mensaje": (
                            "Telegram caído tras wipe/redeploy: "
                            f"{st_tg.get('motivo') or 'falta token/chat'}"
                        )[:180],
                        "acciones": [
                            ACCION_RESTAURAR_TELEGRAM,
                            ACCION_REGISTRAR,
                            ACCION_NOTIFICAR,
                        ],
                    }
                )
        except Exception:
            pass

    # Historial wipeado en Render (días del backup del repo que el disco ya no tiene)
    try:
        from servidor_mlb import (
            BASE_DIR,
            MEMORIA_PATH,
            _backup_tiene_dias_que_el_disco_perdio,
            _contar_historial,
            _fechas_con_historial,
        )

        origen = BASE_DIR / "memoria_auditoria.json"
        if origen.exists() and MEMORIA_PATH.exists():
            bundled = json.loads(origen.read_text(encoding="utf-8"))
            disk = json.loads(MEMORIA_PATH.read_text(encoding="utf-8"))
            if not disk.get("reinicio_manual") and _backup_tiene_dias_que_el_disco_perdio(
                bundled, disk
            ):
                b_ap, b_pr = _contar_historial(bundled)
                perdidas = sorted(_fechas_con_historial(bundled) - _fechas_con_historial(disk))
                hallazgos.append(
                    {
                        "codigo": "historial_wipeado",
                        "severidad": "alta",
                        "mensaje": (
                            "El disco perdió días con predicciones "
                            f"({', '.join(perdidas[:4])}{'…' if len(perdidas) > 4 else ''}). "
                            f"Backup tiene {b_pr} preds / {b_ap} apuestas. Restaurando."
                        )[:180],
                        "acciones": [
                            ACCION_RESTAURAR_HISTORIAL,
                            ACCION_REGISTRAR,
                            ACCION_NOTIFICAR,
                        ],
                    }
                )
    except Exception:
        pass

    if isinstance(vigilancia, dict) and vigilancia.get("nivel") == "alerta":
        total_riesgo = int(vigilancia.get("total_riesgo") or 0)
        total_perdidos = int(vigilancia.get("total_perdidos") or 0)
        if total_riesgo > 0:
            hallazgos.append(
                {
                    "codigo": "vigilancia_t60",
                    "severidad": "alta",
                    "mensaje": str(
                        vigilancia.get("mensaje") or "Juegos sin pick fijo cerca de T-60"
                    )[:180],
                    "acciones": [
                        ACCION_FORZAR_REGISTRO_T60,
                        ACCION_REGISTRAR,
                        ACCION_NOTIFICAR,
                    ],
                    "meta": {
                        "total_riesgo": total_riesgo,
                        "en_riesgo": (vigilancia.get("en_riesgo") or [])[:5],
                    },
                    "cooldown_min": 45,
                }
            )
        if total_perdidos > 0:
            muestras = vigilancia.get("perdidos") or []
            nombres = ", ".join(
                f"{p.get('visitante')}@{p.get('home')}" for p in muestras[:3]
            )
            hallazgos.append(
                {
                    "codigo": "juegos_sin_pick_perdidos",
                    "severidad": "alta",
                    "mensaje": (
                        f"{total_perdidos} juego(s) sin predicción "
                        f"(posible Render dormido): {nombres}"
                    )[:180],
                    "acciones": [ACCION_REGISTRAR, ACCION_NOTIFICAR],
                    "meta": {
                        "total_perdidos": total_perdidos,
                        "perdidos": muestras[:5],
                    },
                    "cooldown_min": 120,
                }
            )

    if isinstance(lineas_meta, dict) and lineas_meta.get("ok") is False:
        msg = str(lineas_meta.get("mensaje") or "Sin cuotas")[:160]
        hallazgos.append(
            {
                "codigo": "cuotas_fallo",
                "severidad": "alta",
                "mensaje": msg,
                "acciones": [ACCION_FORZAR_ESPN, ACCION_ACTIVAR_FALLBACK, ACCION_REGISTRAR],
            }
        )

    # Dedup por codigo (mantener el de mayor severidad / primero)
    vistos: set[str] = set()
    unicos: list[dict[str, Any]] = []
    for h in hallazgos:
        c = str(h.get("codigo") or "")
        if c in vistos:
            continue
        vistos.add(c)
        unicos.append(h)
    return unicos


def _hallazgo_recien_registrado(estado: dict, codigo: str, minutos: int = 30) -> bool:
    for inc in reversed(estado.get("incidentes") or []):
        if not isinstance(inc, dict):
            continue
        if str(inc.get("codigo") or "") != codigo:
            continue
        raw = inc.get("hora")
        if not raw:
            return False
        try:
            prev = datetime.fromisoformat(str(raw).replace("Z", ""))
        except ValueError:
            return False
        return _ahora() < prev + timedelta(minutes=max(1, minutos))
    return False


def _aplicar_acciones(
    estado: dict,
    hallazgos: list[dict[str, Any]],
    cfg: dict,
    opts: dict[str, Any],
) -> list[dict[str, Any]]:
    aplicadas: list[dict[str, Any]] = []
    if not opts.get("auto_remediar", True):
        for h in hallazgos:
            codigo = str(h.get("codigo") or "")
            if _hallazgo_recien_registrado(estado, codigo):
                continue
            _push_incidente(
                estado,
                {
                    "hora": _iso(),
                    "codigo": h.get("codigo"),
                    "severidad": h.get("severidad"),
                    "mensaje": h.get("mensaje"),
                    "acciones": ["solo_diagnostico"],
                },
            )
        return aplicadas

    for h in hallazgos:
        codigo = str(h.get("codigo") or "")
        acciones = list(h.get("acciones") or [])
        hechas: list[str] = []
        for acc in acciones:
            if acc == ACCION_FORZAR_ESPN:
                _set_override(estado, "lineas.proveedor", "espn")
                hechas.append(acc)
            elif acc == ACCION_ACTIVAR_FALLBACK:
                _set_override(estado, "lineas.fallback_internet", True)
                hechas.append(acc)
            elif acc == ACCION_APAGAR_SHADOW:
                _set_override(estado, "mente.shadow", False)
                if str((cfg.get("mente") or {}).get("modo") or "").lower() == "shadow":
                    _set_override(estado, "mente.modo", "normal")
                hechas.append(acc)
            elif acc == ACCION_RESPETAR_CIRCUITO:
                hechas.append(acc)
            elif acc == ACCION_RESTAURAR_TELEGRAM:
                try:
                    from whatsapp_alerta import sincronizar_telegram_persistencia

                    mem = None
                    try:
                        # Evita import circular duro: el caller puede inyectar
                        mem = cfg.get("_memoria_ref")
                    except Exception:
                        mem = None
                    sync = sincronizar_telegram_persistencia(cfg, mem)
                    if sync.get("ok"):
                        hechas.append(acc)
                        h["mensaje"] = (
                            f"Telegram restaurado desde {sync.get('fuente')}"
                        )[:160]
                        h["severidad"] = "baja"
                    else:
                        hechas.append(acc)
                        h["mensaje"] = (
                            (sync.get("motivo") or h.get("mensaje") or "")[:160]
                        )
                except Exception as e:
                    hechas.append(acc)
                    h["mensaje"] = f"Restore Telegram falló: {e}"[:160]
            elif acc == ACCION_RESTAURAR_HISTORIAL:
                try:
                    from servidor_mlb import _intentar_recuperar_wipe

                    ok = bool(_intentar_recuperar_wipe())
                    hechas.append(acc)
                    h["mensaje"] = (
                        "Historial fusionado desde backup del repo"
                        if ok
                        else "No se pudo restaurar historial (¿reinicio_manual?)"
                    )[:160]
                except Exception as e:
                    hechas.append(acc)
                    h["mensaje"] = f"Restore historial falló: {e}"[:160]
            elif acc == ACCION_FORZAR_REGISTRO_T60:
                try:
                    # Lazy import: evita ciclo mente_errores ↔ servidor_mlb
                    from servidor_mlb import (
                        registrar_predicciones_del_dia,
                        bloquear_apuestas_del_dia,
                    )

                    reg = registrar_predicciones_del_dia(forzar=True)
                    try:
                        bloquear_apuestas_del_dia(forzar=False)
                    except Exception:
                        pass
                    hechas.append(acc)
                    nuevas = int((reg or {}).get("predicciones_nuevas") or 0)
                    h["meta"] = {
                        **(h.get("meta") if isinstance(h.get("meta"), dict) else {}),
                        "predicciones_nuevas": nuevas,
                    }
                    if nuevas:
                        h["mensaje"] = (
                            f"Forzado T-60: +{nuevas} predicción(es) congelada(s)"
                        )[:160]
                except Exception as e:
                    hechas.append(acc)
                    h["mensaje"] = f"Forzar registro T-60 falló: {e}"[:160]
            elif acc == ACCION_REGISTRAR:
                hechas.append(acc)
            elif acc == ACCION_NOTIFICAR:
                if opts.get("notificar"):
                    hechas.append(acc)
        if not _hallazgo_recien_registrado(estado, codigo):
            _push_incidente(
                estado,
                {
                    "hora": _iso(),
                    "codigo": h.get("codigo"),
                    "severidad": h.get("severidad"),
                    "mensaje": h.get("mensaje"),
                    "acciones": hechas or [ACCION_REGISTRAR],
                    "meta": h.get("meta"),
                },
            )
        aplicadas.append(
            {
                "codigo": h.get("codigo"),
                "acciones": hechas,
                "mensaje": h.get("mensaje"),
                "severidad": h.get("severidad"),
            }
        )
    return aplicadas


def _notificar_si_cabe(
    estado: dict,
    aplicadas: list[dict[str, Any]],
    cfg: dict,
    opts: dict[str, Any],
) -> dict[str, Any] | None:
    if not opts.get("notificar"):
        return None
    criticos = [
        a
        for a in aplicadas
        if a.get("severidad") == "alta" and ACCION_NOTIFICAR in (a.get("acciones") or [])
    ]
    if not criticos:
        return None
    # Un mensaje agrupado; cooldown por el código más grave
    codigo = str(criticos[0].get("codigo") or "ops")
    clave = f"alerta:{codigo}"
    # Vigilancia / picks perdidos: avisar más seguido (Render free duerme)
    mins = int(opts.get("cooldown_alerta_min") or DEFAULT_COOLDOWN_ALERTA_MIN)
    for a in criticos:
        try:
            cm = int((a.get("meta") or {}).get("cooldown_min") or 0)
        except (TypeError, ValueError):
            cm = 0
        # hallazgo puede traer cooldown_min en el propio dict (no solo meta)
        try:
            # recuperar del hallazgo original vía aplicadas no tiene cooldown;
            # usar defaults por código
            pass
        except Exception:
            pass
        if a.get("codigo") == "vigilancia_t60":
            mins = min(mins, 45)
        elif a.get("codigo") == "juegos_sin_pick_perdidos":
            mins = min(mins, 120)
        elif a.get("codigo") == "historial_wipeado":
            mins = min(mins, 60)
    if not _cooldown_ok(estado, clave, mins):
        return {"omitido": True, "motivo": "cooldown", "codigo": codigo}
    lineas = []
    for a in criticos[:4]:
        lineas.append(f"• {a.get('codigo')}: {(a.get('mensaje') or '')[:120]}")
    texto = (
        "🛠 MENTE ERRORES Quantum MLB\n"
        + "\n".join(lineas)
        + "\nRemediación: ESPN/fallback/shadow/registro T-60 si aplica."
        + "\nSi Render free duerme: cron externo cada 5 min a /api/health o /api/cron."
    )
    try:
        from whatsapp_alerta import enviar_alerta

        res = enviar_alerta(texto, cfg, forzar=True)
        _marcar_cooldown(estado, clave)
        return {"ok": bool((res or {}).get("ok")), "resultado": res, "codigo": codigo}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "codigo": codigo}


def ejecutar_ciclo(
    cfg: dict | None = None,
    *,
    vigilancia: dict | None = None,
    lineas_meta: dict | None = None,
    memoria: dict | None = None,
    forzar: bool = False,
) -> dict[str, Any]:
    """Diagnostica + remedia. Idempotente; seguro para cron cada 5 min."""
    cfg = cfg or {}
    opts = _cfg_mente_errores(cfg)
    if not opts.get("activo") and not forzar:
        return {
            "ok": True,
            "activo": False,
            "mensaje": "Mente de errores desactivada",
            "hallazgos": [],
            "acciones": [],
        }

    # Diagnóstico sobre config YA con overrides actuales
    cfg_eff = aplicar_overrides_config(dict(cfg))
    if memoria is not None:
        cfg_eff["_memoria_ref"] = memoria
        if isinstance(memoria.get("telegram"), dict):
            cfg_eff["_memoria_telegram"] = memoria["telegram"]

    # Siempre intenta reescribir disco desde env/memoria (anti wipe Render)
    telegram_ok_tras = False
    sync_info: dict[str, Any] = {}
    try:
        from whatsapp_alerta import sincronizar_telegram_persistencia

        sync_info = sincronizar_telegram_persistencia(cfg_eff, memoria) or {}
        if sync_info.get("ok") and (
            sync_info.get("wrote_token")
            or sync_info.get("wrote_chat")
            or sync_info.get("memoria_actualizada")
        ):
            telegram_ok_tras = True
        elif sync_info.get("ok"):
            # Ya estaba sano; no marcar como "restaurado" para no spam de saves
            telegram_ok_tras = False
    except Exception:
        pass

    hallazgos = diagnosticar(cfg_eff, vigilancia=vigilancia, lineas_meta=lineas_meta)
    estado = _leer_estado()
    aplicadas = _aplicar_acciones(estado, hallazgos, cfg_eff, opts) if hallazgos else []

    # Si se restauró Telegram en acciones, re-evaluar y bajar el hallazgo
    if any(
        ACCION_RESTAURAR_TELEGRAM in (a.get("acciones") or []) for a in aplicadas
    ):
        try:
            from whatsapp_alerta import telegram_disponible

            st = telegram_disponible(cfg_eff) or {}
            telegram_ok_tras = bool(st.get("ok")) or telegram_ok_tras
            if telegram_ok_tras:
                hallazgos = [h for h in hallazgos if h.get("codigo") != "telegram_no_listo"]
                for a in aplicadas:
                    if a.get("codigo") == "telegram_no_listo":
                        a["severidad"] = "baja"
                        a["mensaje"] = "Telegram restaurado tras wipe/redeploy"
        except Exception:
            pass
    elif sync_info.get("ok"):
        # Ya listo vía env/memoria/disco: no reportar falso positivo
        hallazgos = [h for h in hallazgos if h.get("codigo") != "telegram_no_listo"]

    # Si se restauró historial, re-evaluar y no dejar el panel en "alerta" eterna
    if any(ACCION_RESTAURAR_HISTORIAL in (a.get("acciones") or []) for a in aplicadas):
        try:
            from servidor_mlb import (
                BASE_DIR,
                MEMORIA_PATH,
                _backup_tiene_dias_que_el_disco_perdio,
            )

            origen = BASE_DIR / "memoria_auditoria.json"
            if origen.exists() and MEMORIA_PATH.exists():
                bundled = json.loads(origen.read_text(encoding="utf-8"))
                disk = json.loads(MEMORIA_PATH.read_text(encoding="utf-8"))
                if disk.get("reinicio_manual") or not _backup_tiene_dias_que_el_disco_perdio(
                    bundled, disk
                ):
                    hallazgos = [h for h in hallazgos if h.get("codigo") != "historial_wipeado"]
                    for a in aplicadas:
                        if a.get("codigo") == "historial_wipeado":
                            a["severidad"] = "baja"
                            a["mensaje"] = "Historial restaurado desde backup del repo"
        except Exception:
            pass

    aviso = _notificar_si_cabe(estado, aplicadas, cfg_eff, opts) if aplicadas else None

    if telegram_ok_tras and not hallazgos:
        msg_final = "Mente errores OK · Telegram restaurado"
    else:
        msg_final = _mensaje_resumen(hallazgos, aplicadas)

    resumen = {
        "ok": True,
        "activo": True,
        "hora": _iso(),
        "hallazgos": hallazgos,
        "acciones": aplicadas,
        "overrides": dict(estado.get("overrides") or {}),
        "nivel": _nivel_desde(hallazgos),
        "mensaje": msg_final,
        "notificacion": aviso,
        "telegram_restaurado": telegram_ok_tras,
        "memoria_telegram_dirty": bool(sync_info.get("memoria_actualizada")),
    }
    estado["ultimo_ciclo"] = {
        "hora": resumen["hora"],
        "nivel": resumen["nivel"],
        "mensaje": resumen["mensaje"],
        "n_hallazgos": len(hallazgos),
        "n_acciones": len(aplicadas),
        "telegram_restaurado": telegram_ok_tras,
    }
    _guardar_estado(estado)
    print(f"[MENTE-ERRORES] {resumen['mensaje']}")
    return resumen


def _nivel_desde(hallazgos: list[dict[str, Any]]) -> str:
    if any(h.get("severidad") == "alta" for h in hallazgos):
        return "alerta"
    if hallazgos:
        return "aviso"
    return "ok"


def _mensaje_resumen(
    hallazgos: list[dict[str, Any]], aplicadas: list[dict[str, Any]]
) -> str:
    if not hallazgos:
        return "Mente errores OK · sin fallos operativos"
    codigos = ", ".join(str(h.get("codigo")) for h in hallazgos[:3])
    n_acc = sum(len(a.get("acciones") or []) for a in aplicadas)
    return f"Mente errores · {len(hallazgos)} hallazgo(s): {codigos} · {n_acc} acción(es)"


def resumen_para_panel(cfg: dict | None = None) -> dict[str, Any]:
    """Estado corto para /api/state y health."""
    opts = _cfg_mente_errores(cfg)
    estado = _leer_estado()
    ultimo = estado.get("ultimo_ciclo") if isinstance(estado.get("ultimo_ciclo"), dict) else {}
    incidentes = [
        x for x in (estado.get("incidentes") or []) if isinstance(x, dict)
    ]
    recientes = list(reversed(incidentes[-5:]))
    nivel = ultimo.get("nivel") or "ok"
    return {
        "activo": bool(opts.get("activo")),
        "auto_remediar": bool(opts.get("auto_remediar")),
        "nivel": nivel,
        "mensaje": ultimo.get("mensaje")
        or ("Mente errores lista" if opts.get("activo") else "Mente errores off"),
        "overrides": dict(estado.get("overrides") or {}),
        "ultimo_ciclo": ultimo or None,
        "incidentes_recientes": [
            {
                "hora": r.get("hora"),
                "codigo": r.get("codigo"),
                "severidad": r.get("severidad"),
                "mensaje": (r.get("mensaje") or "")[:120],
                "acciones": r.get("acciones") or [],
            }
            for r in recientes
        ],
        "total_incidentes": len(incidentes),
    }


def limpiar_overrides(claves: list[str] | None = None) -> dict[str, Any]:
    """Quita overrides (todo o claves concretas). Útil tras rotar key OddsPapi."""
    estado = _leer_estado()
    ov = estado.get("overrides") if isinstance(estado.get("overrides"), dict) else {}
    if claves is None:
        estado["overrides"] = {}
    else:
        for k in claves:
            ov.pop(k, None)
        estado["overrides"] = ov
    _guardar_estado(estado)
    return {"ok": True, "overrides": dict(estado.get("overrides") or {})}
