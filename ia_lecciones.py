"""
Lecciones de experiencia (post-mortem IA) para la memoria del experimento.

Al liquidar un fallo, se guarda una lección estructurada.
Esas lecciones se reinyectan en el veto IA para que "aprenda".
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from aprendizaje_mlb import (
    TIPO_ACIERTO,
    analisis_capas_inteligencia,
    calcular_movimiento_linea,
    lecciones_seleccionadas_para_prompt,
    peso_muestra_aprendizaje,
)

from ia_groq import modelo_groq

BASE_DIR = Path(__file__).resolve().parent
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"
MAX_LECCIONES = 80  # default; override en config → aprendizaje.max_lecciones
MAX_PARA_PROMPT = 8  # default; override en config → aprendizaje.max_lecciones_prompt
MAX_LECCIONES_MIN = 80
MAX_LECCIONES_MAX = 2000
MAX_PROMPT_MIN = 4
MAX_PROMPT_MAX = 24


def _cfg_aprendizaje() -> dict[str, Any]:
    try:
        path = BASE_DIR / "config_experimento.json"
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8"))
            ap = cfg.get("aprendizaje")
            if isinstance(ap, dict):
                return ap
    except Exception:
        pass
    return {}


def max_lecciones_almacenadas(cfg: dict | None = None) -> int:
    """Cuántas lecciones se guardan en memoria (tope duro en disco)."""
    ap = cfg if isinstance(cfg, dict) else _cfg_aprendizaje()
    try:
        n = int(ap.get("max_lecciones", MAX_LECCIONES))
    except (TypeError, ValueError):
        n = MAX_LECCIONES
    return max(MAX_LECCIONES_MIN, min(n, MAX_LECCIONES_MAX))


def max_lecciones_prompt(cfg: dict | None = None) -> int:
    """Cuántas lecciones entran al prompt Groq por decisión (subset curado)."""
    ap = cfg if isinstance(cfg, dict) else _cfg_aprendizaje()
    try:
        n = int(ap.get("max_lecciones_prompt", MAX_PARA_PROMPT))
    except (TypeError, ValueError):
        n = MAX_PARA_PROMPT
    return max(MAX_PROMPT_MIN, min(n, MAX_PROMPT_MAX))


def aplicar_tope_lecciones(memoria: dict, cfg: dict | None = None) -> int:
    """Recorta la lista conservando las más recientes. Devuelve cuántas se eliminaron."""
    limite = max_lecciones_almacenadas(cfg)
    lista = asegurar_lista_lecciones(memoria)
    exceso = len(lista) - limite
    if exceso > 0:
        memoria["lecciones"] = lista[-limite:]
    return max(0, exceso)

PATRONES = (
    "favorito_inflado",
    "sin_cuota_real",
    "scratch_lineup",
    "starter_riesgo",
    "bullpen",
    "clima_park",
    "edge_falso",
    "underdog_trampa",
    "underdog_valor",
    "retry_cuota_ok",
    "linea_en_contra",
    "refuerzo_capas",
    "oportunidad_perdida",
    "veto_acertado",
    "mala_practica_sin_mercado",
    "otro",
)

# Tipos de lección (varios por el mismo game_id)
TIPO_FALLO = "fallo_postmortem"
TIPO_OPORTUNIDAD = "oportunidad_perdida"
TIPO_VETO_OK = "veto_acertado"
TIPO_SIN_CUOTA = "sin_cuota_real"


def _api_key(cfg: dict | None = None) -> str:
    env = (os.environ.get("GROQ_API_KEY") or "").strip()
    if env:
        return env
    if cfg:
        return str((cfg.get("groq") or {}).get("api_key") or "").strip()
    return ""


def asegurar_lista_lecciones(memoria: dict) -> list:
    lec = memoria.get("lecciones")
    if not isinstance(lec, list):
        memoria["lecciones"] = []
    return memoria["lecciones"]


def resumen_lecciones(memoria: dict, limite: int = 12) -> dict[str, Any]:
    lec = asegurar_lista_lecciones(memoria)
    por_patron: dict[str, int] = {}
    for item in lec:
        if not isinstance(item, dict):
            continue
        p = str(item.get("patron") or "otro")
        por_patron[p] = por_patron.get(p, 0) + 1
    recientes = [x for x in lec if isinstance(x, dict)][-limite:]
    recientes = list(reversed(recientes))
    return {
        "total": len(lec),
        "por_patron": por_patron,
        "recientes": recientes,
    }


def texto_lecciones_para_prompt(
    memoria: dict | None,
    max_n: int | None = None,
    *,
    juego: dict | None = None,
    cfg: dict | None = None,
) -> str:
    if not memoria:
        return "Lecciones previas: ninguna aún."
    if max_n is None:
        max_n = max_lecciones_prompt(cfg.get("aprendizaje") if isinstance(cfg, dict) else None)
    lec = [x for x in asegurar_lista_lecciones(memoria) if isinstance(x, dict)]
    if not lec:
        return "Lecciones previas: ninguna aún."
    seleccion = lecciones_seleccionadas_para_prompt(
        lec, max_n=max_n, juego=juego, cfg=cfg
    )
    lineas = []
    for i, item in enumerate(seleccion, 1):
        signo = "+" if item.get("tipo") == TIPO_ACIERTO else "−"
        lineas.append(
            f"{i}. [{signo}{item.get('patron') or 'otro'}"
            f"{('/' + str(item['tipo'])) if item.get('tipo') else ''}] "
            f"{item.get('leccion') or item.get('motivo') or ''}"
            f" (pick={item.get('pick') or '?'}, {item.get('fecha') or ''})"
        )
    return "Lecciones recientes (fallos y aciertos; aplica si el spot se parece):\n" + "\n".join(lineas)


def _ya_existe_leccion(
    memoria: dict,
    game_id: str | None,
    tipo: str | None = None,
) -> bool:
    if not game_id:
        return False
    for item in asegurar_lista_lecciones(memoria):
        if not isinstance(item, dict):
            continue
        if str(item.get("game_id") or "") != str(game_id):
            continue
        if tipo is None:
            return True
        item_tipo = str(item.get("tipo") or TIPO_FALLO)
        if item_tipo == str(tipo):
            return True
    return False


def _append_leccion(memoria: dict, item: dict, cfg: dict | None = None) -> dict:
    lista = asegurar_lista_lecciones(memoria)
    lista.append(item)
    aplicar_tope_lecciones(memoria, cfg)
    print(f"[LECCIONES] + {item.get('tipo') or item.get('patron')}: {item.get('leccion')}")
    return item


def _pred_permite_leccion(pred: dict) -> bool:
    """Tardíos basura no; retroactivo/aprendizaje_solo sí (plan 4)."""
    if pred.get("invalida_tarde"):
        return False
    if pred.get("retroactivo") or pred.get("aprendizaje_solo"):
        return True
    if pred.get("valida_stats") is False:
        return False
    return True


def _tuvo_pasar(pred: dict) -> bool:
    veto = pred.get("ia_veto") if isinstance(pred.get("ia_veto"), dict) else {}
    if str(veto.get("decision") or "").upper() == "PASAR":
        return True
    motivo = str(pred.get("motivo_apuesta") or "").upper()
    return "IA PASAR" in motivo or "PASAR:" in motivo


def _sin_cuota_real(pred: dict, juego: dict | None = None) -> bool:
    fuente = str(
        pred.get("lineas_fuente") or (juego or {}).get("lineas_fuente") or "modelo"
    ).lower()
    if fuente in ("modelo", "", "none", "import"):
        return True
    motivo = str(pred.get("motivo_apuesta") or "").lower()
    return "sin mercado" in motivo or "solo stats" in motivo


def _base_item(
    pred: dict,
    *,
    tipo: str,
    patron: str,
    leccion: str,
    motivo: str,
    cuando: str | None,
    confianza: int = 4,
    fuente: str = "heuristica",
) -> dict[str, Any]:
    game_id = pred.get("game_id")
    return {
        "id": f"lec-{tipo}-{game_id or 'x'}-{int(datetime.now(timezone.utc).timestamp())}",
        "tipo": tipo,
        "fecha": cuando or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "game_id": game_id,
        "visitante": pred.get("visitante"),
        "home": pred.get("home"),
        "pick": pred.get("pick"),
        "probPick": pred.get("probPick"),
        "edge": pred.get("edge"),
        "lineas_fuente": pred.get("lineas_fuente"),
        "marcador_final": pred.get("marcador_final"),
        "patron": patron,
        "leccion": leccion,
        "motivo": motivo,
        "confianza": confianza,
        "fuente": fuente,
        "modelo": None,
        "creada_en": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _heuristica_leccion(pred: dict, juego: dict | None = None) -> dict[str, Any]:
    """Fallback sin Groq: clasifica el fallo con señales locales."""
    motivo = str(pred.get("motivo_apuesta") or "").lower()
    fuente = str(pred.get("lineas_fuente") or (juego or {}).get("lineas_fuente") or "modelo")
    lesiones = pred.get("lesiones") if isinstance(pred.get("lesiones"), dict) else {}
    scratch = pred.get("scratch_lineup") if isinstance(pred.get("scratch_lineup"), dict) else {}
    edge = pred.get("edge")
    try:
        edge_f = float(edge) if edge is not None else 0.0
    except (TypeError, ValueError):
        edge_f = 0.0

    patron = "otro"
    leccion = "Revisar edge real vs cuota y contexto de roster antes de dinero."
    if fuente in ("modelo", "", "None", "none") or "sin mercado" in motivo or "solo stats" in motivo:
        patron = "sin_cuota_real"
        leccion = "No apostar con dinero si no hay cuota de mercado (Pinnacle/DK)."
    elif scratch.get("riesgo") or "scratch" in motivo:
        patron = "scratch_lineup"
        leccion = "PASAR si scratch/lineup debilita el lado del pick."
    elif lesiones.get("starter_riesgo") or "starter" in motivo or "lesion" in motivo:
        patron = "starter_riesgo"
        leccion = "PASAR si el abridor del pick está en riesgo o lesionado."
    elif edge_f > 0 and edge_f < 6:
        patron = "edge_falso"
        leccion = "Exigir edge >= 6% contra cuota real; edge bajo no basta."
    elif float(pred.get("probPick") or 0) >= 62 and edge_f < 15:
        patron = "favorito_inflado"
        leccion = "Prob ≥62% exige edge ≥15% para dinero; si no, solo papel."
    elif "bullpen" in motivo or float((pred.get("ml_features") or {}).get("fatiga_bullpen") or 0) >= 0.7:
        patron = "bullpen"
        leccion = "Desconfiar de favoritos con bullpen cargado / fatiga alta."
    elif float(pred.get("linea_movimiento_pct") or 0) <= -5:
        patron = "linea_en_contra"
        leccion = "Cuota empeoró tras T-60; mercado en contra del pick."

    capas = analisis_capas_inteligencia(pred)
    if capas.get("capas"):
        leccion = f"{leccion} · Capas: {','.join(capas['capas'][:4])}"

    return {
        "patron": patron,
        "leccion": leccion,
        "motivo": f"Heurística post-fallo ({patron})",
        "confianza": 3,
        "fuente": "heuristica",
    }


def _parse_json_leccion(texto: str) -> dict[str, Any] | None:
    raw = (texto or "").strip()
    if not raw:
        return None
    candidatos = [raw]
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        candidatos.insert(0, m.group(0))
    for c in candidatos:
        try:
            data = json.loads(c)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _postmortem_groq(pred: dict, cfg: dict, juego: dict | None = None) -> dict[str, Any] | None:
    key = _api_key(cfg)
    if not key:
        return None
    groq_cfg = cfg.get("groq") or {}
    model = modelo_groq(cfg)
    timeout = min(float(groq_cfg.get("timeout_sec") or 8), 12.0)

    visitante = pred.get("visitante") or (juego or {}).get("visitante") or "?"
    home = pred.get("home") or (juego or {}).get("home") or "?"
    pick = pred.get("pick") or "?"
    marcador = pred.get("marcador_final") or ""
    feats = pred.get("ml_features") if isinstance(pred.get("ml_features"), dict) else {}
    patrones = ", ".join(PATRONES)

    prompt = (
        "Eres coach de apuestas MLB. El pick FALLÓ. Extrae UNA lección accionable.\n"
        f"Partido: {visitante} @ {home}\n"
        f"Pick: {pick}\n"
        f"Prob: {pred.get('probPick')}% edge={pred.get('edge')} odds={pred.get('odds')}\n"
        f"Fuente cuotas: {pred.get('lineas_fuente') or 'desconocida'}\n"
        f"Pitchers: {pred.get('pitcherAway')} vs {pred.get('pitcherHome')}\n"
        f"Marcador: {marcador}\n"
        f"Motivo original: {pred.get('motivo_apuesta') or 'n/a'}\n"
        f"Features clave: era={feats.get('era_pitcher')} fip={feats.get('fip_pitcher')} "
        f"bullpen={feats.get('fatiga_bullpen')} run_env={feats.get('run_env')}\n"
        f"Patrón debe ser uno de: {patrones}\n"
        "Responde SOLO JSON:\n"
        '{"patron":"...","leccion":"max 18 palabras","motivo":"max 12 palabras","confianza":1-5}'
    )
    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.2,
                "max_tokens": 140,
                "messages": [
                    {"role": "system", "content": "Respondes solo JSON válido."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"[LECCIONES] Groq HTTP {r.status_code}")
            return None
        texto = (((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        parsed = _parse_json_leccion(texto)
        if not parsed:
            return None
        patron = str(parsed.get("patron") or "otro").strip().lower()
        if patron not in PATRONES:
            patron = "otro"
        leccion = str(parsed.get("leccion") or "").strip()[:160]
        if not leccion:
            return None
        try:
            conf = int(parsed.get("confianza") or 3)
        except (TypeError, ValueError):
            conf = 3
        return {
            "patron": patron,
            "leccion": leccion,
            "motivo": str(parsed.get("motivo") or "post-mortem")[:120],
            "confianza": max(1, min(5, conf)),
            "fuente": "groq",
            "modelo": model,
        }
    except Exception as e:
        print(f"[LECCIONES] Error Groq: {e}")
        return None


def registrar_leccion_desde_fallo(
    memoria: dict,
    pred: dict,
    cfg: dict | None = None,
    juego: dict | None = None,
    cuando: str | None = None,
) -> dict[str, Any] | None:
    """
    Crea y guarda una lección si el pick falló.
    Idempotente por (game_id, tipo=fallo_postmortem).
    """
    cfg = cfg or {}
    if pred.get("resultado") != "fallo":
        return None
    if not _pred_permite_leccion(pred):
        return None

    game_id = pred.get("game_id")
    if _ya_existe_leccion(memoria, game_id, TIPO_FALLO):
        return None

    detalle = _postmortem_groq(pred, cfg, juego) if cfg.get("usar_ia_veto", True) else None
    if not detalle:
        detalle = _heuristica_leccion(pred, juego)

    item = _base_item(
        pred,
        tipo=TIPO_FALLO,
        patron=str(detalle.get("patron") or "otro"),
        leccion=str(detalle.get("leccion") or ""),
        motivo=str(detalle.get("motivo") or ""),
        cuando=cuando,
        confianza=int(detalle.get("confianza") or 3),
        fuente=str(detalle.get("fuente") or "heuristica"),
    )
    item["modelo"] = detalle.get("modelo")
    item["capas"] = analisis_capas_inteligencia(pred)
    return _append_leccion(memoria, item)


def _heuristica_leccion_positiva(pred: dict, juego: dict | None = None) -> dict[str, Any] | None:
    """Clasifica aciertos reforzables."""
    try:
        prob = float(pred.get("probPick") or 0)
        edge = float(pred.get("edge") or 0)
        odds = float(pred.get("odds") or 0)
    except (TypeError, ValueError):
        return None
    fuente = str(pred.get("lineas_fuente") or (juego or {}).get("lineas_fuente") or "").lower()
    if fuente in ("modelo", "", "none"):
        return None

    capas = analisis_capas_inteligencia(pred)
    mente = pred.get("ia_mente") if isinstance(pred.get("ia_mente"), dict) else {}

    if pred.get("cuota_retry"):
        return {
            "patron": "retry_cuota_ok",
            "leccion": "Cuota llegó en retry T-45/T-30 y el pick acertó; priorizar refresh.",
            "motivo": "retry cuota + acierto",
            "confianza": 4,
            "fuente": "heuristica",
        }
    if odds >= 2.0 and 58 <= prob <= 62 and edge >= 6:
        return {
            "patron": "underdog_valor",
            "leccion": "Underdog con edge moderado y prob 58-62% funcionó; mantener filtro.",
            "motivo": "underdog valor + acierto",
            "confianza": 4,
            "fuente": "heuristica",
        }
    if str(mente.get("decision") or "").upper() in ("PASAR", "ESPERAR") and not pred.get("con_dinero"):
        return {
            "patron": "veto_acertado",
            "leccion": "Sin dinero pero pick acertó en papel; calibrar si conviene suavizar veto.",
            "motivo": "paper acierto sin dinero",
            "confianza": 3,
            "fuente": "heuristica",
        }
    if capas.get("capas"):
        return {
            "patron": "refuerzo_capas",
            "leccion": f"Acierto con capas {','.join(capas['capas'][:3])}; contexto alineado.",
            "motivo": "inteligencia + acierto",
            "confianza": 3,
            "fuente": "heuristica",
        }
    return None


def registrar_leccion_desde_acierto(
    memoria: dict,
    pred: dict,
    juego: dict | None = None,
    cuando: str | None = None,
) -> dict[str, Any] | None:
    """Lección positiva tras acierto (refuerzo)."""
    if pred.get("resultado") != "acierto":
        return None
    if not _pred_permite_leccion(pred):
        return None
    game_id = pred.get("game_id")
    if _ya_existe_leccion(memoria, game_id, TIPO_ACIERTO):
        return None
    detalle = _heuristica_leccion_positiva(pred, juego)
    if not detalle:
        return None
    item = _base_item(
        pred,
        tipo=TIPO_ACIERTO,
        patron=str(detalle.get("patron") or "refuerzo_capas"),
        leccion=str(detalle.get("leccion") or ""),
        motivo=str(detalle.get("motivo") or ""),
        cuando=cuando,
        confianza=int(detalle.get("confianza") or 3),
        fuente=str(detalle.get("fuente") or "heuristica"),
    )
    item["con_dinero"] = bool(pred.get("con_dinero"))
    item["capas"] = analisis_capas_inteligencia(pred)
    return _append_leccion(memoria, item)


def registrar_experiencia_negativa(
    memoria: dict,
    pred: dict,
    juego: dict | None = None,
    cuando: str | None = None,
) -> list[dict[str, Any]]:
    """
    Plan 5: experiencias negativas explícitas (además del fallo).
    - PASAR + acierto → oportunidad_perdida
    - PASAR + fallo → veto_acertado (refuerzo)
    - Sin cuota real + (fallo o dinero) → mala_practica_sin_mercado
    """
    creadas: list[dict[str, Any]] = []
    if pred.get("estado") != "liquidado":
        return creadas
    if pred.get("resultado") not in ("acierto", "fallo"):
        return creadas
    if not _pred_permite_leccion(pred):
        return creadas

    game_id = pred.get("game_id")
    resultado = pred.get("resultado")

    if _tuvo_pasar(pred) or (
        isinstance(pred.get("ia_mente"), dict)
        and (
            str(pred["ia_mente"].get("decision") or "").upper() in ("PASAR", "ESPERAR")
            or pred["ia_mente"].get("autoriza_dinero") is False
        )
    ):
        if resultado == "acierto" and not _ya_existe_leccion(memoria, game_id, TIPO_OPORTUNIDAD):
            item = _base_item(
                pred,
                tipo=TIPO_OPORTUNIDAD,
                patron="oportunidad_perdida",
                leccion="PASAR/ESPERAR canceló dinero y el pick ganó; calibrar veto en spots parecidos.",
                motivo="mente bloqueó dinero + acierto",
                cuando=cuando,
                confianza=4,
            )
            creadas.append(_append_leccion(memoria, item))
        elif resultado == "fallo" and not _ya_existe_leccion(memoria, game_id, TIPO_VETO_OK):
            item = _base_item(
                pred,
                tipo=TIPO_VETO_OK,
                patron="veto_acertado",
                leccion="Bloqueo de dinero acertó: el pick falló; mantener señales parecidas.",
                motivo="mente bloqueó dinero + fallo",
                cuando=cuando,
                confianza=5,
            )
            creadas.append(_append_leccion(memoria, item))

    if _sin_cuota_real(pred, juego):
        con_dinero = bool(pred.get("con_dinero"))
        if (resultado == "fallo" or con_dinero) and not _ya_existe_leccion(
            memoria, game_id, TIPO_SIN_CUOTA
        ):
            # Evitar spam: solo 1 lección sin_cuota por game_id (ya cubierto por idempotencia)
            item = _base_item(
                pred,
                tipo=TIPO_SIN_CUOTA,
                patron="mala_practica_sin_mercado",
                leccion="No apostar dinero sin cuota real (Pinnacle/DK); paper sin mercado no es edge.",
                motivo="sin cuota de mercado",
                cuando=cuando,
                confianza=5,
            )
            creadas.append(_append_leccion(memoria, item))

    return creadas


def registrar_experiencias_tras_liquidar(
    memoria: dict,
    pred: dict,
    cfg: dict | None = None,
    juego: dict | None = None,
    cuando: str | None = None,
) -> dict[str, Any]:
    """Hook único tras liquidar: post-mortem + positivas + stats mente."""
    out: dict[str, Any] = {"fallo": None, "positiva": None, "negativas": [], "mente_stats": None}
    if pred.get("resultado") == "fallo":
        out["fallo"] = registrar_leccion_desde_fallo(memoria, pred, cfg, juego, cuando)
    elif pred.get("resultado") == "acierto":
        out["positiva"] = registrar_leccion_desde_acierto(memoria, pred, juego, cuando)
    out["negativas"] = registrar_experiencia_negativa(memoria, pred, juego, cuando)
    try:
        from mente_aprendizaje import actualizar_stats_tras_liquidar

        out["mente_stats"] = actualizar_stats_tras_liquidar(memoria, pred, juego, cfg)
    except Exception as e:
        print(f"[MENTE-APRENDIZAJE] aviso: {e}")
    return out


def procesar_lecciones_de_dia(
    memoria: dict,
    dia: dict,
    cfg: dict | None = None,
    juegos_por_id: dict | None = None,
) -> int:
    """Genera lecciones para predicciones liquidadas del día aún no registradas."""
    cfg = cfg or {}
    juegos_por_id = juegos_por_id or {}
    n = 0
    fecha = dia.get("fecha")
    for pred in dia.get("predicciones") or []:
        if not isinstance(pred, dict) or pred.get("estado") != "liquidado":
            continue
        if pred.get("resultado") not in ("acierto", "fallo"):
            continue
        juego = juegos_por_id.get(str(pred.get("game_id") or ""))
        r = registrar_experiencias_tras_liquidar(memoria, pred, cfg, juego, cuando=fecha)
        if r.get("fallo"):
            n += 1
        n += len(r.get("negativas") or [])
    return n


def escanear_experiencias_negativas(memoria: dict, max_dias: int = 60) -> int:
    """Backfill plan 5 sobre histórico (solo heurística)."""
    cfg_h = {"usar_ia_veto": False}
    n = 0
    dias = list(memoria.get("dias") or [])[-max_dias:]
    for dia in dias:
        if not isinstance(dia, dict):
            continue
        n += procesar_lecciones_de_dia(memoria, dia, cfg=cfg_h)
    return n


def backfill_lecciones_si_vacio(memoria: dict, max_dias: int = 21) -> int:
    """
    Si aún no hay lecciones, genera heurísticas para fallos liquidados.
    Evita llamadas a Groq (solo patrón local). Idempotente: marca flag en memoria.
    """
    if memoria.get("lecciones_backfill_hecho"):
        return 0
    lec = asegurar_lista_lecciones(memoria)
    if lec:
        memoria["lecciones_backfill_hecho"] = True
        return 0

    # Solo heurística: no gastar cuota Groq en histórico.
    n = escanear_experiencias_negativas(memoria, max_dias=max_dias)
    memoria["lecciones_backfill_hecho"] = True
    return n


def backfill_negativas_si_falta(memoria: dict, max_dias: int = 60) -> int:
    """Una pasada de experiencias negativas si aún no se hizo."""
    if memoria.get("experiencias_negativas_backfill_hecho"):
        return 0
    n = escanear_experiencias_negativas(memoria, max_dias=max_dias)
    memoria["experiencias_negativas_backfill_hecho"] = True
    return n
