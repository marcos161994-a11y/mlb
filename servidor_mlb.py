"""
Quantum MLB — Experimento de 10 días (paper trading con resultados reales MLB).

Cada juego se evalúa y bloquea el stake configurado automáticamente 1 hora ANTES de su inicio
(hora Puerto Rico), solo si hay valor vs BetMGM. Al finalizar se liquida P/L.
"""

from __future__ import annotations

import json
import os
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
from modelo_mlb import evaluar_juegos, calcular_stake_dinamico
from parleys_betmgm import generar_parleys, seleccionar_mejor_parley, formatear_recomendacion_parley

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
_lineas_meta_cache: dict = {"ok": False, "mensaje": "Sin cargar"}
CONFIG_PATH = BASE_DIR / "config_experimento.json"
MEMORIA_PATH = DATA_DIR / "memoria_auditoria.json"

MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
scheduler = BackgroundScheduler()


def _inicializar_datos_persistencia() -> None:
    """Copia memoria local a DATA_DIR en el primer arranque en la nube."""
    if DATA_DIR.resolve() == BASE_DIR.resolve():
        return
    origen = BASE_DIR / "memoria_auditoria.json"
    if origen.exists() and not MEMORIA_PATH.exists():
        MEMORIA_PATH.write_text(origen.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[CLOUD] Memoria copiada a {MEMORIA_PATH}")


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
    with open(MEMORIA_PATH, "w", encoding="utf-8") as f:
        print(f"[GUARDAR] Guardando memoria. Capital: {memoria['capital']:.2f}, Día: {memoria['dia_actual']}")
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
    memoria = cargar_memoria()
    # Usar la fecha del día actual del experimento para que el dashboard sea consistente
    dia = dia_operativo(memoria)
    # Si el experimento está activo, respetamos la fecha de la memoria
    if memoria.get("experimento_activo") and dia and dia.get("fecha"):
        try:
            fecha_mem = datetime.strptime(dia["fecha"], "%Y-%m-%d").date()
            return fecha_mem
        except Exception: pass

    # Si el día actual no existe en memoria, calculamos la fecha relativa al inicio del experimento
    if memoria.get("dias") and len(memoria["dias"]) > 0:
        try:
            f_inicio = datetime.strptime(memoria["dias"][0]["fecha"], "%Y-%m-%d").date()
            desplazamiento = memoria["dia_actual"] - 1
            print(f"[DEBUG] hoy_local() calculando fecha relativa: {f_inicio + timedelta(days=desplazamiento)}")
            return f_inicio + timedelta(days=desplazamiento)
        except Exception: pass

    print(f"[DEBUG] hoy_local() usando ahora_simulado: {ahora_simulado().date()}")
    return ahora_simulado().date()


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
        "capital_bruto": memoria["capital"] + en_juego, # Capital + lo que está en juego
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
    dia = dia_operativo(memoria)
    if dia and dia["fecha"] == fecha:
        return dia
    dia = {
        "dia": memoria["dia_actual"],
        "fecha": fecha,
        "bloqueado_en": None,
        "apuestas": [],
        "predicciones": [],  # Predicciones de juegos no apostados
        "resumen": {},
    }
    memoria["dias"].append(dia)
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
    Calcula estadísticas de aciertos/fallos del modelo incluyendo predicciones no apostadas.
    """
    total_predicciones = 0
    aciertos = 0
    fallos = 0
    
    for dia in memoria.get("dias", []):
        # Contar apuestas liquidadas
        for apuesta in dia.get("apuestas", []):
            if apuesta["estado"] in ("ganada", "perdida"):
                total_predicciones += 1
                if apuesta["estado"] == "ganada":
                    aciertos += 1
                else:
                    fallos += 1
        
        # Contar predicciones no apostadas liquidadas
        if "predicciones" in dia:
            for prediccion in dia["predicciones"]:
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
            abs_state = status_info.get("abstractGameState")
            detailed = status_info.get("detailedState", "")

            estado = "PROGRAMADO"
            if abs_state == "Live" or "In Progress" in detailed or "Warmup" in detailed:
                estado = "EN VIVO"
            elif abs_state == "Final" or "Final" in detailed or "Game Over" in detailed:
                estado = "FINALIZADO"

            away = juego["teams"]["away"]
            home = juego["teams"]["home"]
            visitante = away["team"]["name"]
            home_name = home["team"]["name"]
            lineups_api = juego.get("lineups", {})
            lineup_confirmado = bool(lineups_api.get("away") and lineups_api.get("home"))
            ls = juego.get("linescore", {}).get("teams", {})
            s_away = int(ls.get("away", {}).get("runs") or away.get("score", 0))
            s_home = int(ls.get("home", {}).get("runs") or home.get("score", 0))
            inicio = parse_inicio_juego(juego["gameDate"])
            bloqueo = hora_bloqueo_para_inicio(inicio)
            winner = None
            if estado == "FINALIZADO":
                winner = visitante if s_away > s_home else (home_name if s_home > s_away else None)
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
        juegos, _lineas_meta_cache = aplicar_lineas_a_juegos(juegos, cfg)
        bias = calcular_bias_aprendizaje(memoria)
        juegos = evaluar_juegos(juegos, cfg, bias)
    else:
        print(f"[INFO] Modo solo_resultados activo para {fecha or 'hoy'}. Saltando IA y Cuotas.")
        
    return juegos


def liquidar_apuesta(apuesta: dict, juego: dict, stake: float) -> bool:
    """Liquida si el juego finalizó. Devuelve True si hubo cambio."""
    from datetime import datetime
    
    # Si el juego terminó pero no hay ganador oficial en el JSON, lo calculamos por score
    estado = juego.get("estado", "")
    
    # Verificar si el juego parece terminado aunque API diga EN VIVO
    score_away = juego.get("scoreAway")
    score_home = juego.get("scoreHome")
    inning = juego.get("inning")
    
    # Si tiene scores y el inning es 9 o más, o si los scores son diferentes y parece terminado
    juego_terminado = False
    if estado == "FINALIZADO":
        juego_terminado = True
    elif estado == "EN VIVO" and score_away is not None and score_home is not None:
        # Si está en inning 9 o más, probablemente terminó
        if inning and (str(inning).startswith("9") or str(inning).startswith("10") or str(inning).startswith("11")):
            juego_terminado = True
            print(f"[DEBUG LIQ] Juego {juego['id']} marcado como EN VIVO pero inning {inning}, considerando terminado")
        # Si hay scores y parece que ya pasó mucho tiempo (más de 4 horas desde inicio)
        elif juego.get("inicio"):
            try:
                inicio = datetime.fromisoformat(juego["inicio"].replace("Z", "+00:00"))
                ahora = datetime.now(inicio.tzinfo)
                horas_pasadas = (ahora - inicio).total_seconds() / 3600
                if horas_pasadas > 4:
                    juego_terminado = True
                    print(f"[DEBUG LIQ] Juego {juego['id']} marcado como EN VIVO pero pasaron {horas_pasadas:.1f} horas, considerando terminado")
            except:
                pass
        # Si hay scores y son diferentes, asumir que el juego terminó (último recurso)
        elif int(score_away) != int(score_home):
            juego_terminado = True
            print(f"[DEBUG LIQ] Juego {juego['id']} marcado como EN VIVO pero hay scores diferentes ({score_away}-{score_home}), considerando terminado")
    
    if not juego_terminado:
        print(f"[DEBUG LIQ] Juego {juego['id']} no terminado. Estado: {estado}, Inning: {inning}")
        return False

    # Normalizar para que "Oakland Athletics" coincida con "Athletics"
    pick_norm = norm_nombre(nombre_equipo_en_pick(apuesta["pick"]))
    ganador_norm = norm_nombre(juego.get("ganador") or "")

    # Si el juego terminó pero no hay ganador oficial en el JSON, lo calculamos por score
    if not ganador_norm and juego.get("scoreAway") != juego.get("scoreHome"):
        print(f"[DEBUG LIQ] Juego {juego['id']} FINALIZADO pero sin ganador oficial. Intentando por score.")
        if int(juego.get("scoreAway", 0)) > int(juego.get("scoreHome", 0)):
            ganador_norm = norm_nombre(juego["visitante"])
        else:
            ganador_norm = norm_nombre(juego["home"])

    if not ganador_norm:
        print(f"[DEBUG LIQ] Juego {juego['id']} FINALIZADO pero no se pudo determinar ganador. Scores: {juego.get('scoreAway')}-{juego.get('scoreHome')}")
        return False

    print(f"[LIQUIDACIÓN] Juego {juego['id']}: Comparando Pick '{pick_norm}' vs Ganador '{ganador_norm}'")

    nuevo_estado = "ganada" if pick_norm == ganador_norm else "perdida"
    nuevo_marcador = (
        f"{juego['visitante']} {juego['scoreAway']} - "
        f"{juego['home']} {juego['scoreHome']}"
    )
    
    # Si el estado y el marcador coinciden exactamente con lo guardado, no hay nada que cambiar
    if apuesta.get("estado") == nuevo_estado and apuesta.get("marcador_final") == nuevo_marcador:
        print(f"[DEBUG LIQ] Juego {juego['id']} ya liquidado con el mismo estado ({nuevo_estado}).")
        return False

    apuesta["estado"] = nuevo_estado
    if nuevo_estado == "ganada":
        apuesta["profit"] = round(stake * (apuesta["odds"] - 1), 2)
    else:
        apuesta["profit"] = round(-stake, 2)

    apuesta["marcador_final"] = (
        f"{juego['visitante']} {juego['scoreAway']} - "
        f"{juego['home']} {juego['scoreHome']}"
    )
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
    # Verificar si hay apuestas o predicciones pendientes
    apuestas_pendientes = any(a["estado"] == "pendiente" for a in dia.get("apuestas", []))
    predicciones_pendientes = any(p.get("estado") == "pendiente" for p in dia.get("predicciones", []))
    
    if not apuestas_pendientes and not predicciones_pendientes:
        return 0
        
    # Si hay predicciones pendientes, necesitamos datos completos (scores, inning)
    solo_resultados = not predicciones_pendientes
    juegos = obtener_juegos_fecha(dia["fecha"], solo_resultados=solo_resultados)
    if not juegos:
        print(f"[DEBUG LIQ DIA] No se encontraron juegos para el día {dia['fecha']}. No se liquida.")
        return 0
    
    por_id = {g["id"]: g for g in juegos}
    cambios = 0
    for apuesta in dia.get("apuestas", []):
        juego = por_id.get(apuesta["game_id"])
        if juego and liquidar_apuesta(apuesta, juego, apuesta["stake"]):
            cambios += 1
    
    # Liquidar también predicciones no apostadas
    if "predicciones" in dia:
        for prediccion in dia["predicciones"]:
            if prediccion.get("estado") == "pendiente":
                juego = por_id.get(prediccion["game_id"])
                if juego:
                    # Determinar resultado de la predicción
                    estado = juego.get("estado", "")
                    score_away = juego.get("scoreAway")
                    score_home = juego.get("scoreHome")
                    inning = juego.get("inning")
                    
                    # Verificar si el juego terminó (misma lógica que liquidar_apuesta)
                    juego_terminado = False
                    if estado == "FINALIZADO":
                        juego_terminado = True
                    elif estado == "EN VIVO" and score_away is not None and score_home is not None:
                        # Si hay scores diferentes, asumir que el juego terminó
                        if int(score_away) != int(score_home):
                            juego_terminado = True
                        # Si está en inning 9 o más, probablemente terminó
                        elif inning and (str(inning).startswith("9") or str(inning).startswith("10") or str(inning).startswith("11")):
                            juego_terminado = True
                    
                    if juego_terminado:
                        # Calcular ganador
                        ganador = None
                        if not juego.get("ganador") and score_away != score_home:
                            if int(score_away) > int(score_home):
                                ganador = norm_nombre(juego["visitante"])
                            else:
                                ganador = norm_nombre(juego["home"])
                        else:
                            ganador = norm_nombre(juego.get("ganador") or "")
                        
                        # Comparar con predicción
                        pick_norm = norm_nombre(nombre_equipo_en_pick(prediccion["pick"]))
                        resultado = "acierto" if pick_norm == ganador else "fallo"
                        
                        prediccion["estado"] = "liquidado"
                        prediccion["resultado"] = resultado
                        prediccion["marcador_final"] = (
                            f"{juego['visitante']} {score_away} - "
                            f"{juego['home']} {score_home}"
                        )
                        prediccion["liquidado_en"] = datetime.now(tz_experimento()).isoformat()
                        cambios += 1
                        print(f"[PREDICCIÓN] {prediccion['pick']} -> {resultado.upper()} ({prediccion['marcador_final']})")
    
    if cambios:
        print(f"[DEBUG LIQ DIA] Se realizaron {cambios} cambios para el día {dia['fecha']}. Recalculando y guardando.")
        recalcular_capital(memoria)
        actualizar_resumen(memoria)
        guardar_memoria(memoria)
    return cambios


def liquidar_todo(memoria: dict) -> int:
    total = 0
    for dia in memoria["dias"]:
        # SOLO pedir datos a la API si el día realmente tiene algo que liquidar
        if any(a["estado"] == "pendiente" for a in dia.get("apuestas", [])):
            total += liquidar_dia(memoria, dia)
        
    return total


def avanzar_dia_automatico() -> None:
    """Avanza al siguiente día del experimento a la medianoche de forma autónoma."""
    memoria = cargar_memoria()
    if not memoria.get("experimento_activo") or not memoria.get("dias"):
        return

    hoy_real = ahora_simulado().date()
    try:
        f_inicio = datetime.strptime(memoria["dias"][0]["fecha"], "%Y-%m-%d").date()
        hubo_cambio = False
        while memoria["dia_actual"] < memoria.get("dias_totales", 10):
            # Fecha asociada al día operativo actual (basada en el puntero dia_actual)
            fecha_operativa = f_inicio + timedelta(days=memoria["dia_actual"] - 1)
            
            if fecha_operativa < hoy_real:
                # Asegurar registro del día que estamos dejando atrás
                asegurar_dia_operativo(memoria, fecha_operativa.strftime("%Y-%m-%d"))
                
                memoria["dia_actual"] += 1
                hubo_cambio = True
                
                # Inicializar el nuevo día operativo
                nueva_fecha_str = (f_inicio + timedelta(days=memoria["dia_actual"] - 1)).strftime("%Y-%m-%d")
                asegurar_dia_operativo(memoria, nueva_fecha_str)
                
                print(f"[SISTEMA] Avanzando automáticamente al DÍA {memoria['dia_actual']} ({nueva_fecha_str})")
            else:
                break

        if hubo_cambio:
            guardar_memoria(memoria)
            programar_bloqueos_por_juego()
    except Exception as e:
        print(f"[SISTEMA] Error al avanzar el día automáticamente: {e}")


def bloquear_juego(game_id: str, forzar: bool = False) -> dict:
    """Evalúa 1h antes del partido y bloquea el stake configurado si hay valor vs BetMGM."""
    memoria = cargar_memoria()
    cfg = cargar_config()
    estr = cfg.get("estrategia", {})
    max_dia = int(estr.get("max_apuestas_dia", 5))
    hoy = fecha_str()

    print(f"[DEBUG BLOQUEO] Intentando bloquear juego {game_id} para el día {hoy}. Forzar: {forzar}")
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

    juegos = obtener_juegos_fecha(hoy)
    juego = next((j for j in juegos if j["id"] == game_id), None)
    if not juego:
        print(f"[DEBUG BLOQUEO] Juego {game_id} no encontrado en la API para el día {hoy}.")
        return {"ok": False, "motivo": "Juego no encontrado en el calendario."}

    if juego["estado"] == "FINALIZADO" and not forzar:
        print(f"[DEBUG BLOQUEO] Juego {game_id} ya FINALIZADO y no se fuerza. Estado: {juego['estado']}")
        return {"ok": False, "motivo": "El juego ya terminó."}

    if not juego.get("apostable"):
        print(f"[DEBUG BLOQUEO] Juego {game_id} no apostable. Motivo: {juego.get('motivo_apuesta', 'Desconocido')}")
        
        # Guardar predicción aunque no sea apostable
        ahora = datetime.now(tz_experimento())
        if "predicciones" not in dia:
            dia["predicciones"] = []
        
        # Verificar si ya existe predicción para este juego
        if not any(p["game_id"] == game_id for p in dia["predicciones"]):
            dia["predicciones"].append({
                "game_id": juego["id"],
                "visitante": juego["visitante"],
                "home": juego["home"],
                "pick": juego["pick"],
                "odds": juego.get("odds", 1.5),
                "odds_american": juego.get("odds_american", 150),
                "edge": juego.get("edge", 0),
                "probPick": juego.get("probPick", 50),
                "motivo_apuesta": juego.get("motivo_apuesta", "Sin valor"),
                "pitcherAway": juego.get("pitcherAway"),
                "pitcherHome": juego.get("pitcherHome"),
                "inicio_juego": juego.get("inicio_juego"),
                "estado": "pendiente",
                "resultado": None,  # acierto/fallo
                "predicho_en": ahora.isoformat(),
            })
            guardar_memoria(memoria)
        
        return {
            "ok": False,
            "motivo": juego.get("motivo_apuesta", "Sin valor vs BetMGM ahora."),
            "juego": juego["visitante"] + " vs " + juego["home"],
        }

    # Calcular stake dinámico si está activado
    edge = juego.get("edge", 0)
    confianza = min(max((edge - 5.0) / 10.0, 0.5), 1.0)  # Normalizar edge a confianza 0.5-1.0
    stake = calcular_stake_dinamico(memoria["capital"], edge, confianza, cfg)
    
    riesgo = sum(a["stake"] for a in dia["apuestas"] if a["estado"] == "pendiente")
    print(f"[DEBUG BLOQUEO] Juego {game_id} - Riesgo: {riesgo}, Stake: {stake}, Capital: {memoria['capital']}")
    if riesgo + stake > memoria["capital"]:
        return {
            "ok": False,
            "motivo": f"Banca insuficiente (${memoria['capital']:.2f}).",
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
            "casa": "BetMGM",
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
    if not dia.get("bloqueado_en"):
        dia["bloqueado_en"] = ahora.isoformat()

    # Asegurar que la memoria refleje el stake configurado actualmente
    memoria["stake_por_juego"] = stake

    actualizar_resumen(memoria)
    guardar_memoria(memoria)
    exportar_reporte(memoria, dia)

    print(
        f"[BLOQUEO] {juego['visitante']} vs {juego['home']} → "
        f"{juego['pick']} @ {juego['odds']} (edge +{juego.get('edge')}%)"
    )
    return {
        "ok": True,
        "pick": juego["pick"],
        "odds": juego["odds"],
        "edge": juego.get("edge"),
        "game_id": game_id,
    }


def bloquear_apuestas_del_dia(forzar: bool = False) -> dict:
    """Reprograma bloqueos 1h antes de cada juego y procesa los vencidos."""
    programar_bloqueos_por_juego()
    memoria = cargar_memoria()
    hoy = fecha_str()
    ahora = ahora_simulado()
    juegos = obtener_juegos_fecha(hoy)
    nuevas = 0
    omitidos = []

    for juego in juegos:
        if juego["estado"] == "FINALIZADO" and not forzar:
            continue
        hb = datetime.fromisoformat(juego["hora_bloqueo"])
        ya_pasó = hb <= ahora or forzar
        if not ya_pasó:
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
    dia = dia_operativo(memoria)
    por_id = {}
    if dia:
        por_id = {a["game_id"]: a for a in dia["apuestas"]}

    resultado = []
    for juego in juegos:
        copia = dict(juego)
        ap = por_id.get(juego["id"])
        if ap:
            copia["stake"] = ap["stake"]
            copia["pick"] = ap["pick"]
            copia["odds"] = ap["odds"]
            copia["odds_american"] = ap.get("odds_american")
            copia["lineas_fuente"] = ap.get("lineas_fuente", "betmgm")
            copia["estado_apuesta"] = ap["estado"]
            copia["profit"] = ap.get("profit")
        else:
            copia["stake"] = memoria["stake_por_juego"]
            copia["estado_apuesta"] = "sin_bloquear"
            copia["profit"] = None
        copia["apostable"] = copia.get("apostable", False)
        copia["edge"] = copia.get("edge") or ap.get("edge") if ap else copia.get("edge")
        copia["motivo_apuesta"] = copia.get("motivo_apuesta", "")
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
    # Actualizar parleys cada minuto para datos en vivo
    scheduler.add_job(
        lambda: obtener_juegos_fecha(fecha_str()),
        CronTrigger(minute="*", timezone=tz),
        id="actualizar_parleys_vivo",
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


def construir_estado_completo(liquidar: bool = False) -> dict:
    memoria = cargar_memoria()
    # Sincronizar el día del experimento con el tiempo real/simulado
    avanzar_dia_automatico()
    memoria = cargar_memoria()

    # Asegurar que el día actual existe en memoria para que los contadores no salgan en 0
    asegurar_dia_operativo(memoria)

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
    dia = dia_operativo(memoria)
    ahora = ahora_simulado()
    juegos = []
    try:
        juegos = fusionar_apuestas_con_juegos(
            obtener_juegos_fecha(fecha_str()), memoria
        )
    except Exception as e:
        print(f"Error cargando juegos: {e}")

    # Calcular estadísticas del modelo
    stats_modelo = calcular_estadisticas_modelo(memoria)
    
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
        "fecha_hoy": fecha_str(),
        "games": juegos,
        "stats_modelo": stats_modelo,
    }


@app.get("/api/state")
def api_state():
    return construir_estado_completo(liquidar=True)


@app.get("/api/live-data")
def api_live_data():
    estado = construir_estado_completo()
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
    estado = construir_estado_completo(liquidar=True)
    return {
        "liquidaciones": 1,
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


@app.get("/api/auto-bloqueo-externo")
@app.post("/api/auto-bloqueo-externo")
def api_auto_bloqueo_externo(secret: str | None = None):
    """
    Para cron-job.org u otro servicio externo (cada 5-10 min).
    Opcional: ?secret=TU_CRON_SECRET (variable CRON_SECRET en Render).
    """
    _verificar_cron_secreto(secret)
    try:
        programar_bloqueos_por_juego()
        resultado = bloquear_apuestas_del_dia(forzar=False)
        liquidar_todo(cargar_memoria())
        memoria = cargar_memoria()
        return {
            "ok": True,
            "mensaje": "Auto-bloqueo ejecutado",
            "resultado": resultado,
            "capital": memoria["capital"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/avanzar-dia")
def api_avanzar_dia():
    """Fuerza el avance al siguiente día del experimento."""
    memoria = cargar_memoria()
    if memoria["dia_actual"] < memoria["dias_totales"]:
        memoria["dia_actual"] += 1
        guardar_memoria(memoria)
        programar_bloqueos_por_juego()
        return {"ok": True, "nuevo_dia": memoria["dia_actual"]}
    return {"ok": False, "motivo": "Ya se alcanzó el límite de días."}


@app.get("/api/parleys")
def api_parleys():
    """Genera y devuelve recomendaciones de parleys basadas en los juegos del día."""
    cfg = cargar_config()
    estrategia = cfg.get("estrategia", {})
    
    # Obtener juegos del día con modelo y líneas
    juegos = obtener_juegos_fecha(fecha_str())
    
    # Generar parleys
    parleys = generar_parleys(
        juegos,
        max_picks_por_parley=3,
        min_edge_individual=estrategia.get("min_edge_pct", 5.0),
        min_prob_modelo=estrategia.get("min_prob_modelo", 52.0)
    )
    
    # Seleccionar el mejor parley
    mejor_parley = seleccionar_mejor_parley(parleys)
    
    return {
        "parleys": parleys[:10],  # Top 10 parleys
        "mejor_parley": mejor_parley,
        "recomendacion_texto": formatear_recomendacion_parley(mejor_parley) if mejor_parley else "No hay parleys recomendados",
        "total_juegos_analizados": len(juegos),
        "juegos_con_valor": len([j for j in juegos if j.get("apostable")])
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
