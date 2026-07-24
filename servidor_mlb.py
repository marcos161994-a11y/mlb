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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from lineas_betmgm import aplicar_lineas_a_juegos
from lineas_betmgm import normalizar_nombre_equipo as norm_nombre
from modelo_mlb import evaluar_juegos, calcular_stake_dinamico, cuota_desde_prob
from ml_predictor import auto_entrenar_ml

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


def _inicializar_datos_persistencia() -> None:
    """Copia memoria local a DATA_DIR; restaura backup del repo si hubo wipe."""
    if DATA_DIR.resolve() == BASE_DIR.resolve():
        return
    origen = BASE_DIR / "memoria_auditoria.json"
    if origen.exists():
        try:
            bundled = json.loads(origen.read_text(encoding="utf-8"))
        except Exception:
            bundled = None
        if bundled is not None:
            if not MEMORIA_PATH.exists():
                MEMORIA_PATH.write_text(
                    json.dumps(bundled, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"[CLOUD] Memoria copiada a {MEMORIA_PATH}")
            else:
                try:
                    disk = json.loads(MEMORIA_PATH.read_text(encoding="utf-8"))
                except Exception:
                    disk = None
                b_ap, b_pr = _contar_historial(bundled)
                if disk is not None and _memoria_parece_reinicio(disk) and (b_ap + b_pr) > 0:
                    merged = _fusionar_memoria(bundled, disk)
                    MEMORIA_PATH.write_text(
                        json.dumps(merged, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    print(
                        f"[CLOUD] Memoria recuperada desde repo "
                        f"(backup {b_ap} apuestas / {b_pr} preds + día en disco)"
                    )
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


def calcular_estadisticas_modelo(memoria: dict) -> dict:
    """
    Calcula aciertos/fallos del modelo.
    Si un juego tiene apuesta, no se cuenta también su predicción (evita doble conteo).
    """
    total_predicciones = 0
    aciertos = 0
    fallos = 0
    
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
            if prediccion.get("estado") == "liquidado":
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
        "win_rate": round(win_rate, 1)
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
    params = {"sportId": 1, "hydrate": "probablePitcher,lineups,linescore,team"}
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
            lineups_api = juego.get("lineups", {})
            lineup_confirmado = bool(lineups_api.get("away") and lineups_api.get("home"))
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
                "pitcher_away_id": away.get("probablePitcher", {}).get("id"),
                "pitcher_home_id": home.get("probablePitcher", {}).get("id"),
                "scoreAway": s_away,
                "home": home_name,
                "scoreHome": s_home,
                "pick": "",
                "odds": 0,
                "lineup_confirmado": lineup_confirmado,
                "apostable": False,
                "ganador": winner,
                "inicio_juego": inicio.isoformat(),
                "hora_bloqueo": bloqueo.isoformat(),
                "hora_inicio_txt": inicio.strftime("%I:%M %p"),
                "hora_bloqueo_txt": bloqueo.strftime("%I:%M %p"),
                "logoAway": f"https://www.mlbstatic.com/team-logos/{away['team']['id']}.svg",
                "logoHome": f"https://www.mlbstatic.com/team-logos/{home['team']['id']}.svg",
            })

    global _lineas_meta_cache
    print(f"[INFO] Se encontraron {len(juegos)} juegos. Procesando líneas...")
    
    if not solo_resultados:
        if cfg.get("modo_solo_modelo") or not cfg.get("estrategia", {}).get("requiere_betmgm", True):
            _lineas_meta_cache = {
                "ok": True,
                "fuente": "modelo",
                "mensaje": "Modo solo modelo (sin BetMGM / sin Odds API)",
                "partidos": len(juegos),
            }
        else:
            juegos, _lineas_meta_cache = aplicar_lineas_a_juegos(juegos, cfg)
        bias = calcular_bias_aprendizaje(memoria)
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
    
    if cambios:
        print(f"[DEBUG LIQ DIA] Se realizaron {cambios} cambios para el día {dia['fecha']}. Recalculando y guardando.")
        recalcular_capital(memoria)
        actualizar_resumen(memoria)
        auto_entrenar_ml(memoria)
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
) -> bool:
    """Guarda/actualiza predicción de un juego. No mueve capital."""
    pick = (juego.get("pick") or "").strip()
    if not pick:
        return False
    if "predicciones" not in dia:
        dia["predicciones"] = []

    stake_v = float(stake_virtual if stake_virtual is not None else stake_virtual_prediccion())
    ahora = datetime.now(tz_experimento()).isoformat()
    existente = next((p for p in dia["predicciones"] if p.get("game_id") == juego["id"]), None)
    if existente:
        # No cambiar pick ya congelado; solo marcar si hubo dinero
        if con_dinero:
            existente["con_dinero"] = True
        if existente.get("stake_virtual") is None:
            existente["stake_virtual"] = stake_v
        return False

    prob = float(juego.get("probPick") or 50)
    odds = juego.get("odds")
    odds_amer = juego.get("odds_american")
    if not odds or float(odds) <= 1.0:
        odds, odds_amer = cuota_desde_prob(prob)

    dia["predicciones"].append(
        {
            "game_id": juego["id"],
            "visitante": juego["visitante"],
            "home": juego["home"],
            "pick": juego["pick"],
            "odds": float(odds),
            "odds_american": odds_amer if odds_amer is not None else 150,
            "edge": juego.get("edge", 0),
            "probPick": prob,
            "motivo_apuesta": juego.get("motivo_apuesta", ""),
            "pitcherAway": juego.get("pitcherAway"),
            "pitcherHome": juego.get("pitcherHome"),
            "inicio_juego": juego.get("inicio_juego"),
            "estado": "pendiente",
            "resultado": None,
            "profit": None,
            "stake_virtual": stake_v,
            "con_dinero": bool(con_dinero),
            "predicho_en": ahora,
        }
    )
    return True


def registrar_predicciones_del_dia(forzar: bool = False) -> dict:
    """
    Registra un pick en PAPEL para los juegos del día.
    - PROGRAMADO: cuando ya pasó la hora de bloqueo (o forzar).
    - EN VIVO: solo si aún no había predicción (alcanzar juegos que ya empezaron).
    No registra FINALIZADO/POSPUESTO a posteriori.
    La apuesta con dinero es aparte.
    """
    memoria = cargar_memoria()
    hoy = fecha_str()
    ahora = ahora_simulado()
    dia = asegurar_dia_operativo(memoria, hoy)
    juegos = obtener_juegos_fecha(hoy)
    stake_v = stake_virtual_prediccion(memoria)
    ya = {p.get("game_id") for p in dia.get("predicciones", [])}
    nuevas = 0

    for juego in juegos:
        estado = juego.get("estado")
        if estado not in ("PROGRAMADO", "EN VIVO"):
            continue
        if not (juego.get("pick") or "").strip():
            continue
        if estado == "PROGRAMADO":
            try:
                hb = datetime.fromisoformat(juego["hora_bloqueo"])
            except Exception:
                continue
            if not forzar and hb > ahora:
                continue
        elif estado == "EN VIVO" and juego["id"] in ya and not forzar:
            continue
        if guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=stake_v):
            nuevas += 1

    if nuevas:
        guardar_memoria(memoria)
    return {"ok": True, "predicciones_nuevas": nuevas, "fecha": hoy}


def rellenar_predicciones_fecha(memoria: dict, fecha: str) -> int:
    """
    Si faltaron predicciones (servidor apagado), las crea para esa fecha
    usando el modelo actual y los resultados de MLB.
    """
    dia = dia_por_fecha(memoria, fecha)
    if not dia:
        dia = asegurar_dia_operativo(memoria, fecha)

    juegos = obtener_juegos_fecha(fecha, solo_resultados=False)
    if not juegos:
        return 0

    stake_v = stake_virtual_prediccion(memoria)
    ya = {p.get("game_id") for p in dia.get("predicciones", [])}
    nuevas = 0

    for juego in juegos:
        if juego.get("estado") == "POSPUESTO":
            continue
        if not (juego.get("pick") or "").strip():
            continue
        if juego["id"] in ya:
            continue
        if guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=stake_v):
            pred = next(p for p in dia["predicciones"] if p["game_id"] == juego["id"])
            pred["retroactivo"] = True
            nuevas += 1

    return nuevas


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


def bloquear_juego(game_id: str, forzar: bool = False) -> dict:
    """1h antes: siempre registra predicción; si es apostable, también apuesta con dinero."""
    cfg = cargar_config()
    estr = cfg.get("estrategia", {})
    max_dia = int(estr.get("max_apuestas_dia", 5))
    hoy = fecha_str()

    print(f"[DEBUG BLOQUEO] Intentando bloquear juego {game_id} para el día {hoy}. Forzar: {forzar}")

    # Red fuera del lock
    juegos = obtener_juegos_fecha(hoy)
    juego = next((j for j in juegos if j["id"] == game_id), None)
    if not juego:
        print(f"[DEBUG BLOQUEO] Juego {game_id} no encontrado en la API para el día {hoy}.")
        return {"ok": False, "motivo": "Juego no encontrado en el calendario."}

    if juego["estado"] != "PROGRAMADO":
        print(f"[DEBUG BLOQUEO] Juego {game_id} no programado ({juego['estado']}). No se bloquea.")
        return {
            "ok": False,
            "motivo": f"El juego ya está {juego['estado']}; solo se apuesta antes del inicio.",
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
    if any(a["game_id"] == game_id for a in dia["apuestas"]):
        return {"ok": False, "motivo": "Este juego ya fue bloqueado."}

    if contar_apuestas_hoy(memoria, hoy) >= max_dia and not forzar:
        return {
            "ok": False,
            "motivo": f"Ya tienes {max_dia} apuestas hoy (máximo del día).",
        }

    stake_v = stake_virtual_prediccion(memoria)
    guardar_prediccion(dia, juego, con_dinero=False, stake_virtual=stake_v)

    # Si ya había predicción congelada, la apuesta con dinero debe usar ESE pick
    pred_existente = next(
        (p for p in dia.get("predicciones", []) if p.get("game_id") == game_id),
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

    if not juego.get("apostable"):
        print(f"[DEBUG BLOQUEO] Juego {game_id} no apostable. Motivo: {juego.get('motivo_apuesta', 'Desconocido')}")
        guardar_memoria(memoria)
        return {
            "ok": False,
            "motivo": juego.get("motivo_apuesta", "Sin valor vs BetMGM ahora."),
            "juego": juego["visitante"] + " vs " + juego["home"],
            "prediccion_guardada": True,
        }

    edge = juego.get("edge", 0)
    confianza = min(max((edge - 5.0) / 10.0, 0.5), 1.0)
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
            "motivo_apuesta": juego.get("motivo_apuesta"),
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
    juegos = obtener_juegos_fecha(hoy)
    nuevas = 0
    omitidos = []

    for juego in juegos:
        if juego["estado"] != "PROGRAMADO":
            continue
        hb = datetime.fromisoformat(juego["hora_bloqueo"])
        ya_pasó = hb <= ahora or forzar
        if not ya_pasó:
            continue
        # Solo intentar apuesta con dinero si el modelo lo marca apostable
        if not juego.get("apostable"):
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
    dia = dia_operativo(memoria)
    por_id = {}
    preds_por_id = {}
    if dia:
        por_id = {a["game_id"]: a for a in dia.get("apuestas", [])}
        preds_por_id = {p["game_id"]: p for p in dia.get("predicciones", [])}

    resultado = []
    for juego in juegos:
        copia = dict(juego)
        ap = por_id.get(juego["id"])
        pred = preds_por_id.get(juego["id"])
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
            copia["estado_apuesta"] = "sin_bloquear"
            copia["profit"] = None
            copia["pick_congelado"] = True
        else:
            copia["stake"] = memoria["stake_por_juego"]
            copia["estado_apuesta"] = "sin_bloquear"
            copia["profit"] = None
            copia["pick_congelado"] = False
        copia["apostable"] = copia.get("apostable", False)
        if not copia.get("motivo_apuesta"):
            copia["motivo_apuesta"] = ""
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
    pred_aciertos = sum(1 for p in preds_hoy if p.get("resultado") == "acierto")
    pred_fallos = sum(1 for p in preds_hoy if p.get("resultado") == "fallo")
    pred_pend = sum(1 for p in preds_hoy if p.get("estado") == "pendiente")
    pred_neto = round(
        sum(float(p.get("profit") or 0) for p in preds_hoy if p.get("profit") is not None),
        2,
    )
    resumen_hoy["pred_aciertos"] = pred_aciertos
    resumen_hoy["pred_fallos"] = pred_fallos
    resumen_hoy["pred_pendientes"] = pred_pend
    resumen_hoy["pred_neto"] = pred_neto
    resumen_hoy["pred_total"] = len(preds_hoy)
    if dia:
        dia["resumen"] = resumen_hoy
    
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
        [g for g in juegos if g.get("apostable") and (g.get("probPick") or 0) >= min_prob],
        key=lambda x: x.get("probPick", 0),
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
    return {
        "ok": True,
        "servicio": "quantum-mlb",
        "capital": cargar_memoria().get("capital"),
        "dia_actual": cargar_memoria().get("dia_actual"),
        "hora": datetime.now(tz_experimento()).isoformat(),
    }


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
def api_subir_memoria(payload: dict, secret: str | None = None):
    """Sube memoria_auditoria.json desde la PC local a Render (requiere CRON_SECRET)."""
    _verificar_cron_secreto(secret)
    if not isinstance(payload, dict) or "capital" not in payload:
        raise HTTPException(status_code=400, detail="JSON de memoria invalido")
    guardar_memoria(payload)
    memoria = cargar_memoria()
    return {
        "ok": True,
        "capital": memoria.get("capital"),
        "dia_actual": memoria.get("dia_actual"),
        "dias": len(memoria.get("dias", [])),
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
