"""
Quantum MLB — Experimento de 10 días (paper trading con resultados reales MLB).

Cada juego se evalúa y bloquea el stake configurado automáticamente 1 hora ANTES de su inicio
(hora Puerto Rico), solo si hay valor vs BetMGM. Al finalizar se liquida P/L.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from lineas_betmgm import aplicar_lineas_a_juegos
from lineas_betmgm import normalizar_nombre_equipo as norm_nombre
from modelo_mlb import (
    evaluar_juegos,
    calcular_stake_dinamico,
    cuota_desde_prob,
    tiene_cuota_mercado,
    apostable_con_mercado,
)
from ml_predictor import auto_entrenar_ml
from ia_groq import ia_veto_disponible, probar_conexion_groq, veto_apuesta
from mente_mlb import (
    mente_conclusion,
    mente_disponible,
    aplicar_stake_mente,
    generar_briefing_juego,
)
from whatsapp_alerta import (
    notificar_pick_t60,
    whatsapp_disponible,
    telegram_disponible,
    alerta_disponible,
    formatear_mensaje_pick,
    enviar_whatsapp,
    enviar_telegram,
    enviar_alerta,
    vincular_telegram_chat,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
_lineas_meta_cache: dict = {"ok": False, "mensaje": "Sin cargar"}
CONFIG_PATH = BASE_DIR / "config_experimento.json"
MEMORIA_PATH = DATA_DIR / "memoria_auditoria.json"
_memoria_lock = threading.RLock()

MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
scheduler = BackgroundScheduler()
_cron_externo_lock = threading.Lock()
_cron_externo_activo = False
_juegos_ui_cache: dict = {"fecha": "", "ts": 0.0, "juegos": []}
_JUEGOS_UI_TTL_SEC = 90


def _contar_historial(memoria: dict) -> tuple[int, int]:
    """(apuestas liquidadas, predicciones con resultado) para comparar backups."""
    apuestas = 0
    preds = 0
    for dia in memoria.get("dias") or []:
        for a in dia.get("apuestas") or []:
            if a.get("estado") in ("ganada", "perdida"):
                apuestas += 1
        for p in dia.get("predicciones") or []:
            if p.get("resultado") in ("acierto", "fallo"):
                preds += 1
    return apuestas, preds


def _memoria_parece_reinicio(memoria: dict) -> bool:
    """True si parece un wipe/reinicio (día 1, banca inicial, sin historial dinero)."""
    dias = memoria.get("dias") or []
    capital = float(memoria.get("capital") or 0)
    inicial = float(memoria.get("capital_inicial") or 100)
    apuestas, preds = _contar_historial(memoria)
    return (
        int(memoria.get("dia_actual") or 1) <= 1
        and abs(capital - inicial) < 0.01
        and apuestas == 0
        and len(dias) <= 2
        and preds <= 10  # solo el día recién creado tras el wipe
    )


def _fusionar_memoria(base: dict, extra: dict) -> dict:
    """Une historial base con días más nuevos de extra (p.ej. picks de hoy tras wipe)."""
    out = copy.deepcopy(base)
    by_fecha = {d["fecha"]: d for d in out.get("dias") or [] if d.get("fecha")}
    for dia in extra.get("dias") or []:
        fecha = dia.get("fecha")
        if not fecha:
            continue
        if fecha not in by_fecha:
            by_fecha[fecha] = copy.deepcopy(dia)
            continue
        dest = by_fecha[fecha]
        preds = {str(p.get("game_id")): p for p in (dest.get("predicciones") or [])}
        for p in dia.get("predicciones") or []:
            gid = str(p.get("game_id") or "")
            cur = preds.get(gid)
            if cur is None or (
                cur.get("estado") == "pendiente" and p.get("estado") == "liquidado"
            ):
                preds[gid] = p
        dest["predicciones"] = list(preds.values())
        if not dest.get("apuestas") and dia.get("apuestas"):
            dest["apuestas"] = copy.deepcopy(dia["apuestas"])
    dias = sorted(by_fecha.values(), key=lambda d: d["fecha"])
    for i, d in enumerate(dias, 1):
        d["dia"] = i
    out["dias"] = dias
    # Capital real solo de apuestas con dinero
    cap = float(out.get("capital_inicial") or 100)
    for d in dias:
        for a in d.get("apuestas") or []:
            if a.get("estado") in ("ganada", "perdida") and a.get("profit") is not None:
                cap += float(a["profit"])
    out["capital"] = round(cap, 2)
    return out


def _intentar_recuperar_wipe() -> bool:
    """Si el disco parece reinicio y el repo tiene historial, restaura + fusiona hoy."""
    origen = BASE_DIR / "memoria_auditoria.json"
    if not origen.exists() or not MEMORIA_PATH.exists():
        return False
    try:
        bundled = json.loads(origen.read_text(encoding="utf-8"))
        disk = json.loads(MEMORIA_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    if disk.get("reinicio_manual"):
        return False
    b_ap, b_pr = _contar_historial(bundled)
    if not _memoria_parece_reinicio(disk) or (b_ap + b_pr) <= 0:
        return False
    merged = _fusionar_memoria(bundled, disk)
    MEMORIA_PATH.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"[CLOUD] Memoria recuperada desde repo "
        f"(backup {b_ap} apuestas / {b_pr} preds + día en disco)"
    )
    return True


def _inicializar_datos_persistencia() -> None:
    """Copia memoria local a DATA_DIR; restaura backup del repo si hubo wipe."""
    if DATA_DIR.resolve() == BASE_DIR.resolve():
        return
    origen = BASE_DIR / "memoria_auditoria.json"
    if origen.exists() and not MEMORIA_PATH.exists():
        try:
            bundled = json.loads(origen.read_text(encoding="utf-8"))
            MEMORIA_PATH.write_text(
                json.dumps(bundled, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[CLOUD] Memoria copiada a {MEMORIA_PATH}")
        except Exception as e:
            print(f"[CLOUD] No se pudo copiar memoria: {e}")
    else:
        _intentar_recuperar_wipe()
    for nombre in ("modelo_rf_mlb.pkl", "scaler_rf_mlb.pkl"):
        src = BASE_DIR / nombre
        dst = DATA_DIR / nombre
        if src.exists() and not dst.exists():
            dst.write_bytes(src.read_bytes())
            print(f"[CLOUD] Modelo ML copiado a {dst}")


def _verificar_cron_secreto(secret: str | None) -> None:
    esperado = os.environ.get("CRON_SECRET", "").strip()
    if esperado and secret != esperado:
        raise HTTPException(status_code=403, detail="Cron secret inválido")


def cargar_config() -> dict:
    if not CONFIG_PATH.exists():
        # Crear una configuración por defecto si no existe para evitar el cierre
        print(f"[ERROR] No se encontró {CONFIG_PATH.name}. Creando uno básico...")
        cfg_base = {"capital_inicial": 100.0, "dias_totales": 10, "stake_por_juego": 5.0, "timezone": "America/Puerto_Rico", "temporada_mlb": 2026, "lineas": {"api_key": ""}, "estrategia": {"min_edge_pct": 5.0, "max_apuestas_dia": 5, "min_prob_modelo": 52.0}}
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg_base, f, indent=2)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def cargar_memoria() -> dict:
    if MEMORIA_PATH.exists():
        try:
            with open(MEMORIA_PATH, encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            print(f"[ERROR] {MEMORIA_PATH.name} está corrupto. Se iniciará una nueva memoria.")
        except Exception as e:
            print(f"[ERROR] Error inesperado cargando memoria: {e}")
            
    cfg = cargar_config()
    return {
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


def guardar_memoria(memoria: dict) -> None:
    with _memoria_lock:
        with open(MEMORIA_PATH, "w", encoding="utf-8") as f:
            print(
                f"[GUARDAR] Guardando memoria. Capital: {memoria['capital']:.2f}, "
                f"Día: {memoria['dia_actual']}"
            )
            json.dump(memoria, f, indent=2, ensure_ascii=False)
        js_path = DATA_DIR / "memoria_dashboard.js"
        js_path.write_text(
            f"const datosMemoria = {json.dumps(memoria, ensure_ascii=False)};",
            encoding="utf-8",
        )


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


def hora_bloqueo_para_inicio(inicio: datetime) -> datetime:
    mins = int(cargar_config().get("minutos_antes_juego", 60))
    return inicio - timedelta(minutes=mins)


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
    Lógica de Auto-Aprendizaje: Analiza si el modelo ha fallado mucho recientemente.
    Retorna un valor que ajusta la fuerza de los equipos en el modelo.
    """
    todas = []
    for d in memoria.get("dias", []):
        for a in d.get("apuestas", []):
            if a["estado"] in ("ganada", "perdida"):
                todas.append(a)
    
    if len(todas) < 5: # No hay suficiente historial para aprender todavía
        return 0.0
    
    ganadas = sum(1 for a in todas if a["estado"] == "ganada")
    win_rate = ganadas / len(todas)
    
    # Si el win rate es bajo (ej. < 45%), el modelo se vuelve más "pesimista" (bias negativo)
    # Esto obliga a que los equipos tengan que ser mucho mejores para ser elegidos.
    if win_rate < 0.45:
        print(f"[APRENDIZAJE] Win rate bajo ({win_rate:.1%}). Aplicando bias cauteloso.")
        return -1.2
    elif win_rate > 0.60:
        print(f"[APRENDIZAJE] Excelente rendimiento ({win_rate:.1%}). Modelo con confianza.")
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
            a.get("game_id")
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
            if prediccion.get("game_id") in apostados:
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
            # Postponed/Cancelled a veces vienen con abstractGameState=Final: no liquidar.
            estado = "PROGRAMADO"
            if (
                coded in ("D", "C", "DR", "DI")
                or "Postponed" in detailed
                or "Cancelled" in detailed
                or "Suspended" in detailed
            ):
                estado = "POSPUESTO"
            elif (
                abs_state == "Live"
                or coded in ("I", "IW", "IR")
                or "In Progress" in detailed
                or "Warmup" in detailed
                or "Manager Challenge" in detailed
            ):
                estado = "EN VIVO"
            elif (
                abs_state == "Final"
                or coded in ("F", "O", "FT", "FR")
                or detailed in ("Final", "Game Over", "Completed Early")
            ):
                estado = "FINALIZADO"

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
                "fecha": juego.get("gameDate", "").split("T")[0],
                "estado": estado,
                "visitante": visitante,
                "away_id": away["team"]["id"],
                "home_id": home["team"]["id"],
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
            try:
                from lineas_oddspapi import redactar_secretos

                if isinstance(_lineas_meta_cache, dict) and _lineas_meta_cache.get("mensaje"):
                    _lineas_meta_cache["mensaje"] = redactar_secretos(
                        _lineas_meta_cache["mensaje"]
                    )
            except Exception:
                pass
            bias = calcular_bias_aprendizaje(memoria)
            cfg_eval = cfg
            # Si OddsPapi/API falla Y ESPN no trajo cuotas: estudio, no apostar.
            if not (_lineas_meta_cache or {}).get("ok") and (cfg.get("estrategia") or {}).get(
                "fallback_solo_modelo", True
            ):
                cfg_eval = {**cfg, "modo_solo_modelo": True}
                _lineas_meta_cache = {
                    **(_lineas_meta_cache or {}),
                    "fallback_solo_modelo": True,
                    "mensaje": "Sin cuota de casa ahora · estudio (no apostar). ESPN/OddsPapi no disponibles.",
                }
            juegos = evaluar_juegos(juegos, cfg_eval, bias)
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
    if juego.get("estado") not in ("EN VIVO", "PROGRAMADO", "POSPUESTO"):
        return False
    apuesta["estado"] = "pendiente"
    apuesta["profit"] = None
    apuesta.pop("marcador_final", None)
    apuesta.pop("liquidado_en", None)
    print(f"[LIQUIDACIÓN] Revertida liquidación prematura juego {juego.get('id')} (aún {juego.get('estado')})")
    return True


def liquidar_apuesta(apuesta: dict, juego: dict, stake: float) -> bool:
    """Liquida si el juego finalizó. Devuelve True si hubo cambio."""
    if _revertir_liquidacion_prematura(apuesta, juego):
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

    apuesta["estado"] = nuevo_estado
    if nuevo_estado == "ganada":
        apuesta["profit"] = round(stake * (apuesta["odds"] - 1), 2)
    else:
        apuesta["profit"] = round(-stake, 2)

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
    for apuesta in dia.get("apuestas", []):
        juego = por_id.get(str(apuesta.get("game_id") or ""))
        if not juego:
            continue
        if apuesta.get("estado") == "pendiente" or apuesta.get("estado") in ("ganada", "perdida"):
            if liquidar_apuesta(apuesta, juego, apuesta["stake"]):
                cambios += 1
    
    # Liquidar también predicciones no apostadas (y corregir si se liquidaron mal)
    if "predicciones" in dia:
        for prediccion in dia["predicciones"]:
            if prediccion.get("estado") not in ("pendiente", "liquidado"):
                continue
            juego = por_id.get(str(prediccion.get("game_id") or ""))
            if not juego:
                continue

            if prediccion.get("estado") == "liquidado" and juego.get("estado") in (
                "EN VIVO", "PROGRAMADO", "POSPUESTO"
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
            if any(a.get("game_id") == prediccion.get("game_id") for a in apuestas):
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
    
    if cambios:
        print(f"[DEBUG LIQ DIA] Se realizaron {cambios} cambios para el día {dia['fecha']}. Recalculando y guardando.")
        recalcular_capital(memoria)
        actualizar_resumen(memoria)
        auto_entrenar_ml(memoria)
        try:
            from calibracion import entrenar_calibrador

            memoria["calib_meta"] = entrenar_calibrador(memoria, min_muestras=30)
        except Exception as e:
            print(f"[CALIB] auto: {e}")
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


def guardar_prediccion(
    dia: dict,
    juego: dict,
    *,
    con_dinero: bool = False,
    stake_virtual: float | None = None,
    permitir_gracia: bool = False,
) -> bool:
    """Guarda/actualiza predicción de un juego. No mueve capital."""
    pick = (juego.get("pick") or "").strip()
    if not pick:
        return False
    if "predicciones" not in dia:
        dia["predicciones"] = []

    stake_v = float(stake_virtual if stake_virtual is not None else stake_virtual_prediccion())
    ahora_dt = datetime.now(tz_experimento())
    ahora = ahora_dt.isoformat()
    existente = next((p for p in dia["predicciones"] if p.get("game_id") == juego["id"]), None)
    if existente:
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

    cfg = cargar_config()
    gracia_min = float(cfg.get("minutos_gracia_bloqueo", 30))
    inicio = _parse_iso_dt(juego.get("inicio_juego"))
    mins_despues = (
        (ahora_dt - inicio).total_seconds() / 60.0 if inicio else None
    )

    # EN VIVO: solo con gracia explícita (Render dormido en T-60).
    if estado == "EN VIVO":
        if not permitir_gracia:
            print(
                f"[PREDICCIONES] No se congela pick nuevo en estado EN VIVO "
                f"({juego.get('visitante')}@{juego.get('home')})"
            )
            return False
        if mins_despues is None or mins_despues > gracia_min:
            print(
                f"[PREDICCIONES] EN VIVO fuera de gracia "
                f"({mins_despues} min > {gracia_min}) "
                f"({juego.get('visitante')}@{juego.get('home')})"
            )
            return False
    elif mins_despues is not None and mins_despues > gracia_min:
        print(
            f"[PREDICCIONES] No se congela pick post-inicio "
            f"({mins_despues:.0f}m > gracia {gracia_min:.0f}m) "
            f"({juego.get('visitante')}@{juego.get('home')})"
        )
        return False

    prob = float(juego.get("probPick") or 50)
    odds = juego.get("odds")
    odds_amer = juego.get("odds_american")
    if not odds or float(odds) <= 1.0:
        odds, odds_amer = cuota_desde_prob(prob)

    # Apostable solo con cuota de casa. Un 72% sin mercado no es valor.
    apostable_flag = apostable_con_mercado(juego)

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
    if estado == "EN VIVO" and permitir_gracia:
        extra = (
            f"Congelado en gracia EN VIVO "
            f"({(mins_despues or 0):.0f} min tras inicio)"
        )
        motivo = f"{motivo} · {extra}".strip(" ·")

    dia["predicciones"].append(
        {
            "game_id": juego["id"],
            "visitante": juego["visitante"],
            "home": juego["home"],
            "pick": juego["pick"],
            "odds": float(odds),
            "odds_american": odds_amer if odds_amer is not None else 150,
            "edge": 0 if not tiene_cuota_mercado(juego) else juego.get("edge", 0),
            "probPick": prob,
            "apostable": apostable_flag,
            "lineas_fuente": juego.get("lineas_fuente") or "modelo",
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
            "congelado_en_gracia": bool(estado == "EN VIVO" and permitir_gracia),
            "valida_stats": True,
            "invalida_tarde": False,
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
        }
    )
    return True


def registrar_predicciones_del_dia(forzar: bool = False) -> dict:
    """
    Registra pick en PAPEL para juegos PROGRAMADOS (tras T-60).
    Si Render dormía: también EN VIVO dentro de minutos_gracia_bloqueo.
    FINALIZADO: no se inventa pick a posteriori.
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
    gracia = float(cfg.get("minutos_gracia_bloqueo", 30))

    for juego in juegos:
        estado = juego.get("estado")
        gid = str(juego.get("id") or "")
        permitir_gracia = False
        if estado == "EN VIVO":
            mins = _minutos_desde_inicio(juego)
            if mins is None or mins > gracia:
                if gid not in ya:
                    omitidas_vivo += 1
                continue
            permitir_gracia = True
        elif estado != "PROGRAMADO":
            continue
        if not (juego.get("pick") or "").strip():
            continue
        if gid in ya and not forzar:
            continue
        if estado == "PROGRAMADO" and not forzar:
            try:
                hb = datetime.fromisoformat(juego["hora_bloqueo"])
            except Exception:
                continue
            if hb > ahora:
                continue
        if guardar_prediccion(
            dia,
            juego,
            con_dinero=False,
            stake_virtual=stake_v,
            permitir_gracia=permitir_gracia,
        ):
            # Marca validez: solo PROGRAMADO pre-inicio
            pred = next(p for p in dia["predicciones"] if str(p.get("game_id")) == gid)
            if permitir_gracia:
                pred["valida_stats"] = prediccion_valida_para_stats(pred)
                pred["invalida_tarde"] = not pred["valida_stats"]
            else:
                pred["valida_stats"] = True
                pred["invalida_tarde"] = False
            # Mente local para el aviso (sin Groq) + WhatsApp del equipo elegido
            try:
                cfg_wa = cfg
                if cfg_wa.get("usar_mente", True) and not isinstance(pred.get("ia_mente"), dict):
                    mente_t60 = mente_conclusion(
                        juego, cfg_wa, memoria, forzar=True, solo_local=True
                    )
                    pred["ia_mente"] = mente_t60
                    juego["ia_mente"] = mente_t60
                notificar_pick_t60(juego, pred, cfg_wa, fase="t60")
            except Exception as e:
                print(f"[WHATSAPP] aviso T-60: {e}")
            nuevas += 1
            ya.add(gid)

    if nuevas:
        guardar_memoria(memoria)
    if omitidas_vivo:
        print(f"[PREDICCIONES] Omitidas {omitidas_vivo} EN VIVO (fuera de gracia / ya empezados).")
    return {
        "ok": True,
        "predicciones_nuevas": nuevas,
        "omitidas_en_vivo": omitidas_vivo,
        "fecha": hoy,
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
    """Totales separados: predicciones (papel) vs apuestas con dinero.

    Devuelve también `_mutado` (interno) si rellenó profit faltante.
    """
    pred_aciertos = pred_fallos = 0
    pred_ganado = pred_perdido = 0.0
    pred_excluidas = 0
    din_ganadas = din_perdidas = 0
    din_ganado = din_perdido = 0.0
    mutado = False

    for dia in memoria.get("dias", []):
        for p in dia.get("predicciones", []):
            if p.get("estado") != "liquidado":
                continue
            profit = p.get("profit")
            if profit is None and p.get("resultado") in ("acierto", "fallo"):
                stake_v = float(
                    p.get("stake_virtual") or stake_virtual_prediccion(memoria)
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
            if not prediccion_valida_para_stats(p):
                pred_excluidas += 1
                continue
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
            if a["estado"] == "ganada":
                din_ganadas += 1
                din_ganado += max(profit, 0)
            else:
                din_perdidas += 1
                din_perdido += abs(min(profit, 0))

    pred_total = pred_aciertos + pred_fallos
    din_total = din_ganadas + din_perdidas
    return {
        "predicciones": {
            "total": pred_total,
            "aciertos": pred_aciertos,
            "fallos": pred_fallos,
            "win_rate": round(100 * pred_aciertos / pred_total, 1) if pred_total else 0,
            "ganado": round(pred_ganado, 2),
            "perdido": round(pred_perdido, 2),
            "neto": round(pred_ganado - pred_perdido, 2),
            "excluidas_tarde": pred_excluidas,
        },
        "dinero": {
            "total": din_total,
            "ganadas": din_ganadas,
            "perdidas": din_perdidas,
            "win_rate": round(100 * din_ganadas / din_total, 1) if din_total else 0,
            "ganado": round(din_ganado, 2),
            "perdido": round(din_perdido, 2),
            "neto": round(din_ganado - din_perdido, 2),
        },
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
    """
    PROGRAMADO siempre (si ya pasó T-60 o forzar).
    EN VIVO: solo gracia corta tras el inicio (Render dormido en T-60).
    """
    estado = juego.get("estado")
    if estado == "PROGRAMADO":
        return True, ""
    if estado != "EN VIVO":
        return False, f"El juego ya está {estado}; solo se apuesta antes/al inicio."

    cfg = cargar_config()
    gracia = float(cfg.get("minutos_gracia_bloqueo", 30))
    mins = _minutos_desde_inicio(juego)
    if mins is None:
        return False, "EN VIVO sin hora de inicio; no se bloquea dinero."
    if mins < -5:
        # Aún no debería estar EN VIVO según reloj; permitir
        return True, "gracia_preinicio"
    if mins <= gracia or forzar:
        return True, f"gracia_en_vivo_{mins:.0f}m"
    return False, (
        f"EN VIVO hace {mins:.0f} min (gracia {gracia:.0f} min); "
        "no se apuesta dinero a partido avanzado."
    )


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
    # Congelar papel en PROGRAMADO; EN VIVO solo dentro de la gracia (Render dormido).
    ok_gracia, _motivo_g = _permite_bloqueo_dinero(juego, forzar=forzar)
    if juego.get("estado") == "PROGRAMADO":
        guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=stake_v)
    elif juego.get("estado") == "EN VIVO" and ok_gracia:
        guardar_prediccion(
            dia,
            juego,
            con_dinero=False,
            stake_virtual=stake_v,
            permitir_gracia=True,
        )

    # Si ya había predicción congelada, la apuesta con dinero debe usar ESE pick
    pred_existente = next(
        (p for p in dia.get("predicciones", []) if str(p.get("game_id")) == gid),
        None,
    )
    if pred_existente and (pred_existente.get("pick") or "").strip():
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
        # Congelado apostable solo si había cuota real. El % alto no basta.
        if apostable_con_mercado(pred_existente) or apostable_con_mercado(juego):
            juego["apostable"] = True
        else:
            juego["apostable"] = False
            juego["edge"] = 0
            if pred_existente.get("apostable"):
                pred_existente["apostable"] = False

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
            if scratch.get("riesgo") and pick_afectado_por_scratch(
                pick_now, juego.get("visitante") or "", juego.get("home") or "", scratch
            ):
                motivo = "Spot no apto para dinero ahora"
                if pred_existente is not None:
                    pred_existente["apostable"] = False
                    pred_existente["scratch_lineup"] = scratch
                guardar_memoria(memoria)
                print(f"[SCRATCH] Dinero cancelado: {scratch.get('alerta')}")
                return {
                    "ok": False,
                    "motivo": motivo,
                    "juego": juego["visitante"] + " vs " + juego["home"],
                    "prediccion_guardada": True,
                    "scratch_lineup": scratch,
                }
        except Exception as e:
            print(f"[SCRATCH] refresh bloqueo: {e}")

    # Con mercado: exigir edge. Sin cuota de casa: nunca dinero (ni con % alto).
    if not cfg.get("modo_solo_modelo") and (cfg.get("estrategia") or {}).get("requiere_betmgm", True):
        min_edge = float((cfg.get("estrategia") or {}).get("min_edge_pct", 6.0))
        edge_now = juego.get("edge")
        if tiene_cuota_mercado(juego) or tiene_cuota_mercado(pred_existente or {}):
            if edge_now is None or float(edge_now) < min_edge:
                motivo = "Sin valor vs mercado ahora"
                if pred_existente is not None:
                    pred_existente["apostable"] = False
                guardar_memoria(memoria)
                return {
                    "ok": False,
                    "motivo": motivo,
                    "juego": juego["visitante"] + " vs " + juego["home"],
                    "prediccion_guardada": True,
                }
        else:
            motivo = "Sin cuota real de mercado — el % del modelo no es valor"
            if pred_existente is not None:
                pred_existente["apostable"] = False
                pred_existente["edge"] = 0
                pred_existente["motivo_apuesta"] = (
                    f"{pred_existente.get('motivo_apuesta') or ''} · {motivo}"
                ).strip(" ·")
            guardar_memoria(memoria)
            return {
                "ok": False,
                "motivo": motivo,
                "juego": juego["visitante"] + " vs " + juego["home"],
                "prediccion_guardada": True,
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
            # Si el paper se congeló sin WhatsApp (Render dormido), avisar ahora
            try:
                notificar_pick_t60(juego, pred_existente, cfg, fase="bloqueo")
            except Exception as e:
                print(f"[WHATSAPP] aviso bloqueo: {e}")
        if not mente.get("autoriza_dinero"):
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

    edge = juego.get("edge", 0)
    confianza = min(max((edge - 5.0) / 10.0, 0.5), 1.0)
    if mente and mente.get("autoriza_dinero") and float(mente.get("stake_pct") or 0) > 0:
        stake = aplicar_stake_mente(memoria["capital"], mente, cfg)
    else:
        stake = calcular_stake_dinamico(memoria["capital"], edge, confianza, cfg)

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
    elif veto.get("ok") and veto.get("decision") == "APOSTAR":
        motivo_final = (
            f"{motivo_final} · IA APOSTAR: {veto.get('motivo')} "
            f"(conf {veto.get('confianza')})"
        ).strip(" ·")
    dia["apuestas"].append(
        {
            "game_id": juego["id"],
            "visitante": juego["visitante"],
            "home": juego["home"],
            "pick": juego["pick"],
            "odds": juego["odds"],
            "odds_american": juego.get("odds_american"),
            "lineas_fuente": juego.get("lineas_fuente", "betmgm"),
            "casa": "Modelo" if juego.get("lineas_fuente") == "modelo" else "BetMGM",
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
        "game_id": game_id,
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
        apostable = apostable_con_mercado(juego)
        if not apostable and pred is not None:
            apostable = apostable_con_mercado(pred)
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
    }


def programar_bloqueos_por_juego() -> None:
    """Programa un job por juego: inicio del partido menos 60 min (hora PR)."""
    cfg = cargar_config()
    tz = cfg["timezone"]
    ahora = ahora_simulado()

    for job in scheduler.get_jobs():
        if job.id and job.id.startswith("bloqueo_juego_"):
            scheduler.remove_job(job.id)

    juegos = obtener_juegos_fecha(fecha_str())
    for juego in juegos:
        if juego["estado"] == "FINALIZADO":
            continue
        hb = datetime.fromisoformat(juego["hora_bloqueo"])
        if hb <= ahora:
            continue
        gid = juego["id"]
        scheduler.add_job(
            lambda g=gid: bloquear_juego(g),
            DateTrigger(run_date=hb, timezone=tz),
            id=f"bloqueo_juego_{gid}",
            replace_existing=True,
        )
        print(
            f"[PROGRAMADO] {juego['visitante']} vs {juego['home']} → "
            f"bloqueo {juego['hora_bloqueo_txt']} (juego {juego['hora_inicio_txt']})"
        )


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
            copia["estado_apuesta"] = ap["estado"]
            copia["profit"] = ap.get("profit")
            copia["edge"] = ap.get("edge", copia.get("edge"))
            copia["motivo_apuesta"] = ap.get("motivo_apuesta", copia.get("motivo_apuesta", ""))
            copia["pick_congelado"] = True
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
            copia["pick_congelado"] = True
            if not tiene_cuota_mercado(copia) and not tiene_cuota_mercado(pred):
                copia["apostable"] = False
                copia["edge"] = 0
            else:
                copia["apostable"] = apostable_con_mercado(pred) or apostable_con_mercado(copia)
            copia["resultado_papel"] = pred.get("resultado")
            copia["invalida_tarde"] = bool(
                pred.get("invalida_tarde") or not prediccion_valida_para_stats(pred)
            )
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
    scheduler.add_job(
        lambda: liquidar_todo(cargar_memoria()),
        CronTrigger(minute="*/10", timezone=tz),
        id="liquidacion_periodica",
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: bloquear_apuestas_del_dia(forzar=False),
        CronTrigger(minute="*/5", timezone=tz),
        id="bloqueo_periodico",
        replace_existing=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading

    programar_tareas_background()
    scheduler.start()
    _inicializar_datos_persistencia()

    def en_fondo():
        print("[MOTOR] Iniciando motor autónomo de sincronización en segundo plano...")
        try:
            # Catch-up de días si el servidor estuvo apagado o se pasó la medianoche
            avanzar_dia_automatico()
            reparar_odds_papel(cargar_memoria())
            rellenar_predicciones_recientes(cargar_memoria(), dias_atras=7)
            # Al arrancar, procesamos inmediatamente los juegos que ya deberían estar bloqueados
            bloquear_apuestas_del_dia(forzar=False)
            # Luego programamos los del resto del día
            programar_bloqueos_por_juego()
        except Exception as e:
            print(f"[MOTOR] Error programando bloqueos: {e}")
        try:
            liquidar_todo(cargar_memoria())
        except Exception as e:
            print(f"[MOTOR] Error en liquidación inicial: {e}")

    threading.Thread(target=en_fondo, daemon=True).start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Quantum MLB", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def panel():
    return FileResponse("QuantumMLB.html")


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
        _juegos_ui_cache.update({"fecha": fecha, "ts": ahora, "juegos": juegos})
    return juegos


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
            memoria = cargar_memoria()
        except Exception as e:
            print(f"Aviso relleno predicciones: {e}")

        # Registrar picks en papel de todos los juegos listos (sin mover banca)
        try:
            registrar_predicciones_del_dia(forzar=False)
            memoria = cargar_memoria()
        except Exception as e:
            print(f"Aviso predicciones: {e}")
    else:
        # Panel ligero: igual registrar/catch-up (incluye FINALIZADO sin pred)
        # para que los resultados del día no se queden en "pendiente".
        try:
            registrar_predicciones_del_dia(forzar=False)
            memoria = cargar_memoria()
        except Exception as e:
            print(f"Aviso predicciones (ligero): {e}")

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
    juegos = []
    try:
        juegos = fusionar_apuestas_con_juegos(
            obtener_juegos_para_panel(fecha_hoy, ligero=ligero), memoria
        )
    except Exception as e:
        print(f"Error cargando juegos: {e}")

    # Marcar historial tardío (Yankees mid-game, etc.) sin borrar marcadores
    try:
        n_tarde = marcar_predicciones_tardias(memoria)
        if n_tarde:
            guardar_memoria(memoria)
            print(f"[PREDICCIONES] Marcadas {n_tarde} como inválidas (congeladas tras el inicio).")
    except Exception as e:
        print(f"[PREDICCIONES] marcar tardías: {e}")

    # Calcular estadísticas del modelo
    stats_modelo = calcular_estadisticas_modelo(memoria)
    pl_split = resumen_predicciones_y_dinero(memoria)
    if pl_split.pop("_mutado", False):
        try:
            guardar_memoria(memoria)
        except Exception:
            pass

    # Resumen del día también con predicciones en papel (para el panel)
    resumen_hoy = dict(dia["resumen"]) if dia else {
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

        if not (memoria.get("mente_stats") or {}).get("actualizado_en"):
            n_ms = recomputar_stats_desde_historial(memoria)
            if n_ms:
                guardar_memoria(memoria)
                print(f"[MENTE-APRENDIZAJE] Backfill stats: {n_ms}")
        mente_stats_meta = resumen_mente_stats(memoria)
    except Exception as e:
        print(f"[MENTE-APRENDIZAJE] aviso estado: {e}")
    
    return {
        "memoria": memoria,
        "banca": resumen_banca(memoria),
        "dia_hoy": dia,
        "config": cfg,
        "lineas": _lineas_meta_cache,
        "estrategia": cfg.get("estrategia", {}),
        "total_juegos_bloqueados": len(dia["apuestas"]) if dia else 0,
        "oportunidades_valor_hoy": sum(1 for j in juegos if j.get("apostable")),
        "minutos_antes_juego": cfg.get("minutos_antes_juego", 60),
        "fecha_hoy": fecha_hoy,
        "games": juegos,
        "stats_modelo": stats_modelo,
        "pl_split": pl_split,
        "ml_meta": memoria.get("ml_meta"),
        "calib_meta": memoria.get("calib_meta"),
        "lecciones": lecciones_meta,
        "ia_veto": {
            "activo": bool(cfg.get("usar_ia_veto")),
            "listo": ia_veto_disponible(cfg),
            "modelo": (cfg.get("groq") or {}).get("model") or "llama-3.1-8b-instant",
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
    }


@app.get("/api/state")
def api_state():
    """Estado del panel. Liquida pendientes barato (solo marcadores MLB)."""
    # En Render free el cron a veces no corre si el servicio duerme:
    # liquidar aquí garantiza que al abrir/refrescar el panel salgan resultados.
    return construir_estado_completo(liquidar=True, ligero=True)


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
        if g.get("id") in vistos:
            continue
        vistos.add(g["id"])
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
    estado = construir_estado_completo(ligero=True)
    return {"games": estado["games"]}


@app.post("/api/bloquear-hoy")
def api_bloquear_hoy():
    """Fuerza el análisis y bloqueo inmediato de los juegos que tengan valor ahora mismo."""
    resultado = bloquear_apuestas_del_dia(forzar=True)
    if not resultado.get("ok"):
        raise HTTPException(status_code=400, detail=resultado.get("motivo"))
    return resultado


@app.post("/api/liquidar")
def api_liquidar():
    memoria = cargar_memoria()
    sincronizar_experimento_a_hoy(memoria)
    cambios = liquidar_todo(cargar_memoria())
    estado = construir_estado_completo(liquidar=False)
    return {
        "liquidaciones": cambios,
        "capital": estado["memoria"]["capital"],
    }


@app.post("/api/reiniciar")
def api_reiniciar():
    """Reinicia el experimento por completo, borrando historial previo."""
    cfg = cargar_config()
    # Borrar archivos de reporte antiguos
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
        # Evita que el auto-restore del backup deshaga un reinicio deliberado
        "reinicio_manual": True,
    }
    guardar_memoria(memoria)
    return {"ok": True, "memoria": memoria}


@app.get("/api/apuestas")
def api_apuestas():
    """Historial de apuestas por día (desde memoria_auditoria.json)."""
    memoria = cargar_memoria()
    dia = dia_operativo(memoria)
    return {
        "capital": memoria.get("capital"),
        "dia_actual": memoria.get("dia_actual"),
        "fecha_hoy": fecha_str(),
        "apuestas_hoy": dia.get("apuestas", []) if dia else [],
        "dias": memoria.get("dias", []),
    }


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


@app.get("/api/health")
def api_health():
    """Ping para Render + cron externo (mantiene el servicio despierto en plan free)."""
    cfg = cargar_config()
    circ: dict = {"abierto": False}
    try:
        from lineas_oddspapi import estado_circuito

        circ = estado_circuito()
    except Exception:
        pass
    return {
        "ok": True,
        "servicio": "quantum-mlb",
        "capital": cargar_memoria().get("capital"),
        "dia_actual": cargar_memoria().get("dia_actual"),
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
        "pitcher_avanzado": {
            "activo": True,
            "metricas": ["fip", "xfip", "k_pct", "bb_pct"],
        },
        "odds": {
            "activo": not bool(cfg.get("modo_solo_modelo")),
            "proveedor": (cfg.get("lineas") or {}).get("proveedor") or "oddspapi",
            "requiere_mercado": bool((cfg.get("estrategia") or {}).get("requiere_betmgm", True)),
            "fallback_internet": bool((cfg.get("lineas") or {}).get("fallback_internet", True)),
            "bookmakers": (cfg.get("lineas") or {}).get("bookmakers") or "draftkings",
            "min_edge_pct": float((cfg.get("estrategia") or {}).get("min_edge_pct", 6.0)),
            "key_presente": bool(
                os.environ.get("ODDSPAPI_API_KEY", "").strip()
                or os.environ.get("ODDS_PAPI_KEY", "").strip()
                or ((cfg.get("lineas") or {}).get("api_key") or "").strip()
                or (DATA_DIR / "oddspapi_api_key.txt").exists()
            ),
            "circuito": bool(circ.get("abierto")),
            "circuito_hasta_hora": circ.get("hasta_hora"),
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
        "whatsapp": whatsapp_disponible(cfg),
        "telegram": telegram_disponible(cfg),
        "alertas": alerta_disponible(cfg),
        "xgboost": {
            "activo": bool(cfg.get("usar_xgboost", True)),
        },
    }


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


@app.get("/api/odds-status")
def api_odds_status():
    """Estado del proveedor de cuotas (OddsPapi / The Odds API)."""
    cfg = cargar_config()
    solo = bool(cfg.get("modo_solo_modelo"))
    requiere = bool((cfg.get("estrategia") or {}).get("requiere_betmgm", True))
    proveedor = str((cfg.get("lineas") or {}).get("proveedor") or "oddspapi").lower()
    base = {
        "activo": not solo,
        "requiere_mercado": requiere and not solo,
        "modo_solo_modelo": solo,
        "bookmakers": (cfg.get("lineas") or {}).get("bookmakers") or "draftkings",
        "min_edge_pct": float((cfg.get("estrategia") or {}).get("min_edge_pct", 6.0)),
        "proveedor": proveedor,
        "fallback_internet": bool((cfg.get("lineas") or {}).get("fallback_internet", True)),
        "fallback_solo_modelo": bool(
            (cfg.get("estrategia") or {}).get("fallback_solo_modelo", True)
        ),
    }
    if solo or not requiere:
        return {
            **base,
            "ok": True,
            "desactivado": True,
            "motivo": "Cuotas desactivadas (modo solo modelo)",
        }
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
                    f"{out.get('mensaje') or out.get('motivo') or 'OddsPapi no disponible'} · "
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
            return {
                **base,
                "ok": bool(me.get("ok")),
                "key_presente": False,
                "fallback_espn": True,
                "partidos": me.get("partidos"),
                "mensaje": me.get("mensaje"),
            }

        if proveedor in ("oddspapi", "odds-papi", "odds_papi"):
            from lineas_oddspapi import (
                cargar_api_key,
                circuito_abierto,
                estado_circuito,
                fingerprint_key,
                obtener_lineas_oddspapi,
            )

            key = cargar_api_key(cfg)
            if circuito_abierto():
                st = estado_circuito()
                return _con_espn({
                    **base,
                    "ok": False,
                    "key_presente": bool(key),
                    "key_fingerprint": fingerprint_key(key) if key else None,
                    "circuito": True,
                    "circuito_hasta": st.get("hasta"),
                    "circuito_hasta_hora": st.get("hasta_hora"),
                    "http_status": st.get("http_status"),
                    "motivo": st.get("mensaje"),
                    "mensaje": st.get("mensaje"),
                    "ayuda": (
                        "OddsPapi se pausó sola (401/429). "
                        "Las cuotas salen de ESPN/DraftKings. "
                        "Se reintenta al vencer la pausa o al pegar una key nueva."
                    ),
                })
            if not key:
                return _con_espn({
                    **base,
                    "ok": False,
                    "key_presente": False,
                    "motivo": "Falta ODDSPAPI_API_KEY · se intenta ESPN/DraftKings",
                    "ayuda": "Crea key en https://oddspapi.io o usa el fallback ESPN (sin key)",
                })
            _, meta = obtener_lineas_oddspapi(cfg)
            return _con_espn({
                **base,
                "ok": bool(meta.get("ok")),
                "key_presente": True,
                "key_fingerprint": meta.get("key_fingerprint") or fingerprint_key(key),
                "key_source": meta.get("key_source"),
                "key_length": meta.get("key_length") or len(key),
                "key_score": meta.get("key_score"),
                "api_version": meta.get("api_version"),
                "http_status": meta.get("http_status"),
                "error_api": meta.get("error_api"),
                "tournament_id": meta.get("tournament_id"),
                "partidos": meta.get("partidos"),
                "fixtures_mlb": meta.get("fixtures_mlb"),
                "mensaje": meta.get("mensaje"),
                "cache": meta.get("cache"),
                "intentos": meta.get("intentos"),
                "circuito": bool(meta.get("circuito")),
                "circuito_hasta": meta.get("circuito_hasta"),
                "ayuda": (
                    None
                    if meta.get("ok")
                    else (
                        "Si OddsPapi falla, se usan cuotas ESPN/DraftKings de internet. "
                        "Key incompleta: GitHub Action 'Configurar OddsPapi' o UUID de 36 caracteres."
                    )
                ),
            })

        from lineas_betmgm import cargar_api_key, obtener_lineas_betmgm

        key = cargar_api_key(cfg)
        if not key:
            return _con_espn({
                **base,
                "ok": False,
                "key_presente": False,
                "motivo": "Falta ODDS_API_KEY · se intenta ESPN/DraftKings",
            })
        _, meta = obtener_lineas_betmgm(cfg)
        return _con_espn({
            **base,
            "ok": bool(meta.get("ok")),
            "key_presente": True,
            "partidos": meta.get("partidos"),
            "mensaje": meta.get("mensaje"),
            "requests_restantes": meta.get("requests_restantes"),
            "cache": meta.get("cache"),
        })
    except Exception as e:
        return {**base, "ok": False, "motivo": str(e)[:120]}


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
                "lineas_fuente": "oddspapi",
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


@app.get("/api/whatsapp-status")
def api_whatsapp_status():
    """Estado de alertas WhatsApp (CallMeBot). Preferir Telegram si WhatsApp está lleno."""
    cfg = cargar_config()
    wa = cfg.get("whatsapp") if isinstance(cfg.get("whatsapp"), dict) else {}
    disp = whatsapp_disponible(cfg)
    return {
        **disp,
        "activo_config": bool(wa.get("activo", False)),
        "proveedor": wa.get("proveedor") or "callmebot",
        "solo_apostables": bool(wa.get("solo_apostables", False)),
        "nota": "Si CallMeBot WhatsApp está lleno, usa /api/telegram-status",
        "setup": (
            "1) Agrega el bot CallMeBot en WhatsApp. "
            "2) Envía: I allow callmebot to send me messages. "
            "3) Pon phone+apikey en Render (WHATSAPP_PHONE, CALLMEBOT_APIKEY)."
        ),
    }


@app.get("/api/telegram-status")
def api_telegram_status():
    """Estado de alertas Telegram (BotFather oficial o CallMeBot)."""
    cfg = cargar_config()
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    disp = telegram_disponible(cfg)
    return {
        **disp,
        "activo_config": bool(tg.get("activo", True)),
        "setup": disp.get("setup")
        or (
            "1) Telegram → @BotFather → /newbot\n"
            "2) Render: TELEGRAM_BOT_TOKEN=...\n"
            "3) Escribe hola a TU bot\n"
            "4) Abre /api/telegram-vincular\n"
            "5) POST /api/telegram-test"
        ),
    }


@app.get("/api/telegram-vincular")
def api_telegram_vincular():
    """
    Tras crear el bot y escribirle 'hola', esto guarda tu chat_id
    y te manda un mensaje de confirmación.
    """
    cfg = cargar_config()
    return vincular_telegram_chat(cfg)


@app.get("/api/telegram-guardar-token")
def api_telegram_guardar_token(token: str = "", secret: str = ""):
    """
    Guarda el token del bot en disco (DATA_DIR), sin Environment de Render.
    Si ya existe CRON_SECRET en Render, pásalo: &secret=...
    Primera vez (sin token aún): secret opcional.
    """
    from whatsapp_alerta import configurar_bot_token, leer_bot_token_guardado

    ya_hay = bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or leer_bot_token_guardado()
    )
    # Primera configuración: no exigir secret. Cambiar token después: sí.
    if ya_hay:
        _verificar_cron_secreto(secret or None)
    return configurar_bot_token(token, cargar_config())


@app.post("/api/telegram-guardar-token")
async def api_telegram_guardar_token_post(request: Request):
    """JSON: {"token":"123:AA...","secret":"..."}."""
    from whatsapp_alerta import configurar_bot_token, leer_bot_token_guardado

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    secret = str((body or {}).get("secret") or "")
    ya_hay = bool(
        os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or leer_bot_token_guardado()
    )
    if ya_hay:
        _verificar_cron_secreto(secret or None)
    return configurar_bot_token(str((body or {}).get("token") or ""), cargar_config())


@app.get("/api/alertas-status")
def api_alertas_status():
    """Canal de alerta activo (Telegram preferido, WhatsApp fallback)."""
    cfg = cargar_config()
    return alerta_disponible(cfg)


@app.post("/api/whatsapp-test")
async def api_whatsapp_test(request: Request):
    """Envía un mensaje de prueba por WhatsApp."""
    cfg = cargar_config()
    disp = whatsapp_disponible(cfg)
    if not disp.get("ok"):
        return {**disp, "enviado": False}
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    texto = str((body or {}).get("texto") or "").strip()
    if not texto:
        texto = formatear_mensaje_pick(
            {
                "visitante": "Away Test",
                "home": "Home Test",
                "pick": "Home Test ML",
                "probPick": 61,
                "edge": 8.0,
                "odds": 1.95,
                "odds_american": -105,
                "hora_inicio_txt": "07:05 PM",
                "ia_mente": {"decision": "APOSTAR", "confianza": 4, "razones": ["Prueba WhatsApp"]},
            },
            cfg=cfg,
            fase="test",
        )
    res = enviar_whatsapp(texto, cfg, forzar=True)
    return {**res, "enviado": bool(res.get("ok")), "preview": texto[:200]}


@app.post("/api/telegram-test")
async def api_telegram_test(request: Request):
    """Envía un mensaje de prueba por Telegram."""
    cfg = cargar_config()
    disp = telegram_disponible(cfg)
    if not disp.get("ok"):
        return {**disp, "enviado": False}
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    texto = str((body or {}).get("texto") or "").strip()
    if not texto:
        texto = formatear_mensaje_pick(
            {
                "visitante": "Away Test",
                "home": "Home Test",
                "pick": "Home Test ML",
                "probPick": 61,
                "edge": 8.0,
                "odds": 1.95,
                "odds_american": -105,
                "hora_inicio_txt": "07:05 PM",
                "ia_mente": {"decision": "APOSTAR", "confianza": 4, "razones": ["Prueba Telegram"]},
            },
            cfg=cfg,
            fase="test",
        )
    res = enviar_telegram(texto, cfg, forzar=True)
    return {**res, "enviado": bool(res.get("ok")), "preview": texto[:200]}


@app.post("/api/alerta-test")
async def api_alerta_test(request: Request):
    """Prueba el canal activo (Telegram o WhatsApp)."""
    cfg = cargar_config()
    disp = alerta_disponible(cfg)
    if not disp.get("ok"):
        return {**disp, "enviado": False}
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    texto = str((body or {}).get("texto") or "").strip() or formatear_mensaje_pick(
        {
            "visitante": "Away Test",
            "home": "Home Test",
            "pick": "Home Test ML",
            "probPick": 61,
            "edge": 8.0,
            "odds": 1.95,
            "hora_inicio_txt": "07:05 PM",
            "ia_mente": {"decision": "APOSTAR", "confianza": 4, "razones": ["Prueba alerta"]},
        },
        cfg=cfg,
        fase="test",
    )
    res = enviar_alerta(texto, cfg, forzar=True)
    return {**res, "enviado": bool(res.get("ok")), "preview": texto[:200]}


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
        from ia_lecciones import asegurar_lista_lecciones

        lecciones_n = len(asegurar_lista_lecciones(cargar_memoria()))
    except Exception:
        pass
    base = {
        "activo": bool(cfg.get("usar_ia_veto")),
        "key_presente": ia_veto_disponible(cfg),
        "modelo": (cfg.get("groq") or {}).get("model") or "llama-3.1-8b-instant",
        "lecciones": lecciones_n,
    }
    if not base["activo"]:
        return {**base, "ok": False, "motivo": "usar_ia_veto=false en config"}
    if not base["key_presente"]:
        return {**base, "ok": False, "motivo": "Falta GROQ_API_KEY en Render"}
    ping = probar_conexion_groq(cfg)
    return {**base, **ping}


def ejecutar_trabajo_cron_externo() -> dict:
    """Sincroniza fecha, predicciones, bloqueos y liquidacion."""
    sincronizar_experimento_a_hoy()
    reparar_odds_papel(cargar_memoria())
    rellenar_predicciones_recientes(cargar_memoria(), dias_atras=7)
    programar_bloqueos_por_juego()
    registrar_predicciones_del_dia(forzar=False)
    resultado = bloquear_apuestas_del_dia(forzar=False)
    liquidar_todo(cargar_memoria())
    memoria = cargar_memoria()
    return {
        "ok": True,
        "mensaje": "Auto-bloqueo ejecutado",
        "resultado": resultado,
        "capital": memoria["capital"],
        "dia_actual": memoria.get("dia_actual"),
        "fecha_hoy": fecha_str(),
    }


def _cron_externo_en_fondo() -> None:
    global _cron_externo_activo
    try:
        ejecutar_trabajo_cron_externo()
    except Exception as e:
        print(f"[CRON] Error en trabajo externo: {e}")
    finally:
        _cron_externo_activo = False


@app.post("/api/configurar-oddspapi")
@app.get("/api/configurar-oddspapi")
async def api_configurar_oddspapi(request: Request, secret: str | None = None, key: str | None = None):
    """
    Guarda ODDSPAPI key en disco persistente (DATA_DIR).
    Requiere CRON_SECRET. Body JSON {"key":"..."} o ?key=
    """
    _verificar_cron_secreto(secret)
    raw = key
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = None
        if isinstance(body, dict):
            raw = raw or body.get("key") or body.get("api_key") or body.get("apiKey")
    if not raw:
        raise HTTPException(status_code=400, detail="Falta key (body JSON o ?key=)")
    try:
        from lineas_oddspapi import guardar_api_key, obtener_lineas_oddspapi

        info = guardar_api_key(str(raw))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:160]) from e

    cfg = cargar_config()
    _, meta = obtener_lineas_oddspapi(cfg)
    return {
        "ok": bool(meta.get("ok")),
        "guardado": info,
        "partidos": meta.get("partidos"),
        "api_version": meta.get("api_version"),
        "mensaje": meta.get("mensaje"),
        "key_fingerprint": meta.get("key_fingerprint") or info.get("key_fingerprint"),
        "key_source": meta.get("key_source"),
    }


@app.get("/api/auto-bloqueo-externo")
@app.post("/api/auto-bloqueo-externo")
def api_auto_bloqueo_externo(secret: str | None = None, en_fondo: bool = True):
    """
    Para cron-job.org u otro servicio externo (cada 5-10 min).
    Por defecto responde al instante y corre en segundo plano (en_fondo=1).
    Opcional: ?secret=TU_CRON_SECRET (variable CRON_SECRET en Render).
    """
    _verificar_cron_secreto(secret)
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


@app.get("/api/exportar-memoria")
def api_exportar_memoria(secret: str | None = None):
    """Descarga memoria_auditoria.json (backup). Requiere CRON_SECRET."""
    _verificar_cron_secreto(secret)
    memoria = cargar_memoria()
    return memoria


@app.post("/api/subir-memoria")
def api_subir_memoria(
    payload: dict,
    secret: str | None = None,
    modo: str | None = None,
):
    """Sube memoria_auditoria.json desde la PC local a Render (requiere CRON_SECRET).

    modo=replace (default) | aprendizaje (fusiona como paper retroactivo, plan 4)
    """
    _verificar_cron_secreto(secret)
    if not isinstance(payload, dict) or "capital" not in payload:
        raise HTTPException(status_code=400, detail="JSON de memoria invalido")
    if (modo or "replace").lower() in ("aprendizaje", "merge", "import"):
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
    guardar_memoria(payload)
    memoria = cargar_memoria()
    return {
        "ok": True,
        "modo": "replace",
        "capital": memoria.get("capital"),
        "dia_actual": memoria.get("dia_actual"),
        "dias": len(memoria.get("dias", [])),
    }


@app.post("/api/importar-aprendizaje")
def api_importar_aprendizaje(payload: dict, secret: str | None = None):
    """
    Plan 4: importa pasado para aprender (no infla WR del panel).

    Body:
      - dump completo de memoria, o
      - {"memoria": {...}} dump, o
      - {"experiencias": [ {...}, ... ]}
    Requiere CRON_SECRET.
    """
    _verificar_cron_secreto(secret)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON invalido")

    from ia_importar import importar_dump_aprendizaje, importar_experiencias_lista
    from ia_lecciones import escanear_experiencias_negativas, resumen_lecciones

    memoria = cargar_memoria()
    stats: dict = {}
    dump = payload.get("memoria") if isinstance(payload.get("memoria"), dict) else None
    if dump is None and "capital" in payload and "dias" in payload:
        dump = payload
    if dump is not None:
        stats["dump"] = importar_dump_aprendizaje(memoria, dump)
    if isinstance(payload.get("experiencias"), list):
        stats["lista"] = importar_experiencias_lista(memoria, payload["experiencias"])
    if not stats:
        raise HTTPException(
            status_code=400,
            detail="Envía memoria dump o {'experiencias': [...]}",
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


@app.post("/api/procesar-experiencias")
@app.get("/api/procesar-experiencias")
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


@app.post("/api/restaurar-backup")
def api_restaurar_backup(secret: str | None = None):
    """Restaura memoria desde el JSON del repo si el disco parece un reinicio/wipe."""
    _verificar_cron_secreto(secret)
    origen = BASE_DIR / "memoria_auditoria.json"
    if not origen.exists():
        raise HTTPException(status_code=404, detail="No hay memoria_auditoria.json en el repo")
    try:
        bundled = json.loads(origen.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup ilegible: {e}") from e
    disk = cargar_memoria()
    if not _memoria_parece_reinicio(disk):
        ap, pr = _contar_historial(disk)
        return {
            "ok": False,
            "motivo": "La memoria actual no parece un reinicio; no se sobrescribe",
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
    }


@app.post("/api/avanzar-dia")
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
    print(f"  Bloqueo automático: {cargar_config().get('minutos_antes_juego', 60)} min antes de cada inicio")
    print(f"  Stake: ${cargar_config().get('stake_por_juego', 3.0)} por juego")
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
