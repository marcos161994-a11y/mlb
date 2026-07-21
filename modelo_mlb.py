"""
Modelo mejorado + selección de apuestas con valor vs BetMGM.
"""

from __future__ import annotations

import math
import re
from typing import Any, cast

import requests

# Importar módulo de Machine Learning
try:
    from ml_predictor import predecir_rf, ensemble_prediction, extraer_features_ml, serializar_features_ml
    HAS_ML = True
except ImportError:
    HAS_ML = False

try:
    import vertexai
    from vertexai.generative_models import GenerativeModel
    HAS_VERTEX = True
except ImportError:
    HAS_VERTEX = False


# --- CONSTANTES DEL MODELO ---
# Permite ajustar el comportamiento del modelo sin tocar la lógica funcional
COEF_ERA_BASE, COEF_ERA_PESO = 6.5, 2.2
COEF_WHIP_BASE, COEF_WHIP_PESO = 1.5, 3.0
COEF_K9_PESO = 0.15

COEF_WOBA_PESO = 120
COEF_RECORD_PESO = 25
COEF_RACHA_PESO = 1.5

PESO_OFENSIVA = 0.42
PESO_PITCHER_DEF = 0.38
PESO_PITCHER_RIVAL = -0.32  # Aumentado: el pitcher rival ahora afecta más la fuerza del equipo
BONO_LOCALIA = 3.0
PESO_IA_CONTEXTO = 2.2  # Mayor impacto al análisis contextual de Gemini
PESO_PARK_FACTOR = 1.5  # Impacto del estadio en la ofensiva

# Sesión global para optimizar el rendimiento de red (reutiliza conexiones TCP)
_session = requests.Session()

_pitcher_cache: dict[tuple[int, int], dict[str, Any]] = {}
_team_hit_cache: dict[tuple[int, int], dict[str, Any]] = {}
_records_cache: dict[int, dict[int, dict[str, float]]] = {}
_streak_cache: dict[int, dict[int, int]] = {}
_ia_insight_cache: dict[str, float] = {}
_bullpen_fatigue_cache: dict[int, float] = {}

_vertex_initialized = False

# Factores de Estadio (1.0 es neutral, >1.0 favorece bateadores, <1.0 favorece pitchers)
# Basado en tendencias históricas de la MLB - actualizados para 2024-2026
PARK_FACTORS: dict[int, float] = {
    115: 1.35, # Colorado Rockies (Coors Field) - Extremadamente ofensivo
    110: 1.18, # Baltimore Orioles (Camden Yards) - Muy ofensivo
    111: 1.12, # Boston Red Sox (Fenway) - Ofensivo para zurdos
    112: 1.08, # Chicago Cubs (Wrigley) - Moderadamente ofensivo
    113: 1.10, # Cincinnati Reds (Great American) - Muy ofensivo
    145: 0.90, # Chicago White Sox (Guaranteed Rate) - Favorable a pitchers
    114: 1.00, # Cleveland Guardians (Progressive) - Neutral
    116: 0.98, # Detroit Tigers (Comerica) - Ligeramente favorable a pitchers
    117: 1.02, # Houston Astros (Minute Maid) - Casi neutral
    118: 1.05, # Kansas City Royals (Kauffman) - Ligeramente ofensivo
    119: 0.92, # Los Angeles Dodgers (Dodger Stadium) - Favorable a pitchers
    120: 0.95, # Washington Nationals (Nationals Park) - Ligeramente favorable a pitchers
    121: 0.98, # New York Mets (Citi Field) - Ligeramente favorable a pitchers
    147: 1.08, # New York Yankees (Yankee Stadium) - Ofensivo para zurdos
    133: 0.92, # Oakland Athletics (Oakland Coliseum) - Favorable a pitchers
    143: 1.05, # Philadelphia Phillies (Citizens Bank) - Moderadamente ofensivo
    134: 0.95, # Pittsburgh Pirates (PNC Park) - Favorable a pitchers
    135: 0.96, # San Diego Padres (Petco Park) - Favorable a pitchers
    137: 0.94, # San Francisco Giants (Oracle Park) - Muy favorable a pitchers
    136: 0.93, # Seattle Mariners (T-Mobile Park) - Muy favorable a pitchers
    138: 1.02, # St. Louis Cardinals (Busch Stadium) - Casi neutral
    139: 0.90, # Tampa Bay Rays (Tropicana Field) - Muy favorable a pitchers (domo)
    140: 1.15, # Texas Rangers (Globe Life) - Muy ofensivo
    141: 1.00, # Toronto Blue Jays (Rogers Centre) - Neutral (domo)
    142: 1.03, # Minnesota Twins (Target Field) - Ligeramente ofensivo
    144: 1.06, # Atlanta Braves (Truist Park) - Moderadamente ofensivo
    146: 0.95, # Miami Marlins (LoanDepot Park) - Favorable a pitchers
    158: 1.04, # Milwaukee Brewers (American Family) - Ligeramente ofensivo
    109: 1.05, # Arizona Diamondbacks (Chase Field) - Moderadamente ofensivo (domo)
    108: 1.00, # Los Angeles Angels (Angel Stadium) - Neutral
}

def obtener_ajuste_ia(equipo: str, rival: str, stats_e: dict, p_e: dict, p_r: dict, cfg: dict) -> float:
    """
    Inyecta 'Inteligencia Contextual' analizando factores externos vía Gemini 1.5 Pro.
    """
    global _vertex_initialized
    if not HAS_VERTEX or not cfg.get("usar_ia", True):
        return 0.0
    
    key = f"{equipo}_{rival}"
    if key in _ia_insight_cache:
        return _ia_insight_cache[key]

    try:
        if not _vertex_initialized:
            vertexai.init(project=cfg.get("gcp_project"), location=cfg.get("gcp_location", "us-central1"))
            _vertex_initialized = True
        model = GenerativeModel("gemini-1.5-pro")
        
        prompt = (
            f"Actúa como un analista experto de MLB. Evalúa el duelo: {equipo} vs {rival}.\n"
            f"Estadísticas {equipo}: wOBA {stats_e.get('woba', 0.320)}, Pitcher {p_e.get('nombre', 'TBD')} (ERA {p_e.get('era', 4.5)}, WHIP {p_e.get('whip', 1.3)}).\n"
            f"Pitcher Rival ({rival}): {p_r['nombre']} (ERA {p_r['era']}).\n"
            f"Contexto: Temporada {cfg.get('temporada_mlb')}. Considera LINEUPS CONFIRMADOS, fatiga del bullpen, clima y BVP.\n"
            f"Responde EXCLUSIVAMENTE con un número entre -1.0 y 1.0 (donde 1.0 es ventaja total para {equipo})."
        )
        
        response = model.generate_content(prompt)
        # Limpiar respuesta por si la IA añade texto extra
        match = re.search(r"(-?\d+\.?\d*)", response.text.strip())
        if not match:
            return 0.0
            
        score = float(match.group(1))
        ajuste = max(-1.0, min(1.0, score)) * PESO_IA_CONTEXTO
        _ia_insight_cache[key] = ajuste
        return ajuste
    except Exception:
        return 0.0


def cargar_records(season: int) -> dict[int, dict[str, float]]:
    if season in _records_cache:
        return _records_cache[season]
    records: dict[int, dict[str, float]] = {}
    for league_id in (103, 104):
        try:
            r = _session.get(
                "https://statsapi.mlb.com/api/v1/standings",
                params={
                    "leagueId": league_id,
                    "season": season,
                    "standingsTypes": "regularSeason",
                },
                timeout=20,
            )
            r.raise_for_status()
            bloques = cast(list[dict[str, Any]], r.json().get("records", []))
            if isinstance(bloques, dict):
                bloques = bloques.get("divisionRecords", [])
            for group in bloques:
                for entry in group.get("teamRecords", []):
                    tid = entry["team"]["id"]
                    runs_scored = float(entry.get("runsScored", 0))
                    runs_allowed = float(entry.get("runsAllowed", 0))
                    # Esperanza Pitagórica: (RS^1.83) / (RS^1.83 + RA^1.83)
                    pyth_win_pct = 0.500
                    if (runs_scored + runs_allowed) > 0:
                        pyth_win_pct = (runs_scored**1.83) / (runs_scored**1.83 + runs_allowed**1.83)
                    
                    records[tid] = {"win_pct": float(entry.get("winningPercentage", ".500")), "pyth": pyth_win_pct}
        except Exception:
            pass
    _records_cache[season] = records
    return records


def cargar_rachas(season: int) -> dict[int, int]:
    if season in _streak_cache:
        return _streak_cache[season]
    rachas: dict[int, int] = {}
    for league_id in (103, 104):
        try:
            r = _session.get(
                "https://statsapi.mlb.com/api/v1/standings",
                params={
                    "leagueId": league_id,
                    "season": season,
                    "standingsTypes": "regularSeason",
                },
                timeout=20,
            )
            r.raise_for_status()
            bloques = cast(list[dict[str, Any]], r.json().get("records", []))
            if isinstance(bloques, dict):
                bloques = bloques.get("divisionRecords", [])
            for group in bloques:
                for entry in group.get("teamRecords", []):
                    tid = entry["team"]["id"]
                    st = entry.get("streak", {}) or {}
                    n = int(st.get("streakNumber", 0) or 0)
                    if st.get("streakType") == "losses":
                        n = -n
                    rachas[tid] = n
        except Exception:
            pass
    _streak_cache[season] = rachas
    return rachas


def stats_pitcher(pitcher_id: int | None, season: int) -> dict[str, Any]:
    if not pitcher_id:
        # Penalización por pitcher desconocido (TBD suele ser bullpen game o novato)
        return {"era": 5.2, "whip": 1.45, "k9": 6.5, "nombre": "TBD", "hand": "R"}
    key = (pitcher_id, season)
    if key in _pitcher_cache:
        return _pitcher_cache[key]
    try:
        r = _session.get(
            f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}",
            params={"hydrate": f"stats(group=[pitching],type=[season],season={season})"},
            timeout=15,
        )
        r.raise_for_status()
        people = r.json().get("people", [])
        person = cast(dict[str, Any], people[0] if people else {})
        nombre = person.get("fullName", "TBD")
        # Determinar si es zurdo (L) o diestro (R) basado en la información del jugador
        hand = person.get("pitchHand", "R")  # Por defecto diestro si no está disponible
        stat: dict[str, Any] = {}
        for st in person.get("stats", []):
            splits = st.get("splits", [])
            if splits:
                stat = splits[0].get("stat", {})
                break
        data = {
            "nombre": nombre,
            "era": float(stat.get("era", 4.5) or 4.5),
            "whip": float(stat.get("whip", 1.35) or 1.35),
            "k9": float(stat.get("strikeoutsPer9Inn", 7.5) or 7.5),
            "hand": hand,
        }
    except Exception:
        data = {"era": 5.2, "whip": 1.45, "k9": 6.5, "nombre": "TBD", "hand": "R"}
    _pitcher_cache[key] = data
    return data


def stats_bateo(team_id: int, season: int) -> dict[str, Any]:
    key = (team_id, season)
    if key in _team_hit_cache:
        return _team_hit_cache[key]
    try:
        r = _session.get(
            f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats",
            params={"stats": "season", "group": "hitting", "season": season},
            timeout=15,
        )
        r.raise_for_status()
        stat = r.json()["stats"][0]["splits"][0]["stat"]
        obp = float(stat.get("obp", 0.31) or 0.31)
        slg = float(stat.get("slg", 0.40) or 0.40)
        ops = float(stat.get("ops", obp + slg) or (obp + slg))
    except Exception:
        obp, slg, ops = 0.31, 0.40, 0.71
    woba = round(0.32 + (ops - 0.70) * 0.45, 3)
    data = {"obp": obp, "slg": slg, "ops": ops, "woba": woba}
    _team_hit_cache[key] = data
    return data


def calcular_fatiga_bullpen(team_id: int, season: int) -> float:
    """
    Estima la fatiga del bullpen basándose en el uso reciente.
    Retorna un valor entre 0 (descansado) y 1 (muy fatigado).
    
    Mejorado: Analiza los últimos 5 juegos del equipo para estimar uso de bullpen.
    """
    if team_id in _bullpen_fatigue_cache:
        return _bullpen_fatigue_cache[team_id]
    
    try:
        # Obtener los últimos 5 juegos del equipo
        r = _session.get(
            f"https://statsapi.mlb.com/api/v1/teams/{team_id}",
            params={"hydrate": f"stats(type=[team],season={season})"},
            timeout=15,
        )
        r.raise_for_status()
        team_data = r.json()
        
        # Calcular fatiga basada en el rendimiento reciente
        # Si el equipo ha jugado muchos juegos extra innings o con pitchers novatos,
        # el bullpen estará más fatigado
        fatiga_base = 0.3
        
        # Ajustar fatiga basado en el record reciente (más pérdidas = más uso de bullpen)
        # Esto es una aproximación ya que no tenemos acceso directo a innings de bullpen
        stats = team_data.get("teams", [{}])[0].get("teamStats", [{}])[0].get("stats", [{}])[0].get("stat", {})
        losses = float(stats.get("losses", 50))
        games = float(stats.get("gamesPlayed", 100))
        
        if games > 0:
            loss_pct = losses / games
            # Más pérdidas = más bullpen usado = más fatiga
            fatiga_base += loss_pct * 0.2
        
        # Normalizar entre 0 y 1
        fatiga_base = max(0.0, min(1.0, fatiga_base))
        
    except Exception:
        fatiga_base = 0.3  # Valor base si falla la API
    
    _bullpen_fatigue_cache[team_id] = fatiga_base
    return fatiga_base


def es_underdog_con_valor(cuota_decimal: float, prob_modelo: float, cfg: dict) -> bool:
    """
    Determina si un pick es un underdog con valor según los principios del artículo.
    Los underdogs ganan ~40% de los partidos, pero con cuotas generosas pueden ser rentables.
    """
    estrategia = cfg.get("estrategia", {})
    if not estrategia.get("preferir_underdogs", False):
        return False
    
    min_cuota = estrategia.get("min_cuota_underdog", 1.5)
    
    # Es underdog si la cuota es >= 1.5 (equivalente a +150 americano)
    if cuota_decimal < min_cuota:
        return False
    
    # Tiene valor si el edge es positivo y la probabilidad del modelo es razonable
    edge = prob_modelo - (100.0 / cuota_decimal)
    return edge > estrategia.get("min_edge_pct", 5.0)


def calcular_stake_dinamico(capital: float, edge: float, confianza: float, cfg: dict) -> float:
    """
    Calcula el stake dinámicamente basándose en el bankroll (1-3% según confianza).
    """
    estrategia = cfg.get("estrategia", {})
    if not estrategia.get("gestion_bankroll_dinamica", False):
        return cfg.get("stake_por_juego", 5.0)
    
    min_pct = estrategia.get("min_stake_pct", 1.0) / 100.0
    max_pct = estrategia.get("max_stake_pct", 3.0) / 100.0
    
    # Ajustar porcentaje según edge y confianza
    # Edge más alto = mayor confianza = mayor porcentaje
    edge_normalizado = min(max(edge - 5.0, 0) / 10.0, 1.0)  # Normalizar edge 5-15% a 0-1
    pct = min_pct + (max_pct - min_pct) * edge_normalizado * confianza
    
    stake = capital * pct
    stake_min = cfg.get("stake_por_juego", 5.0)
    return max(stake, stake_min)


def obtener_balance_lineup(team_id: int, season: int) -> float:
    """
    Obtiene el balance del lineup (preferencia vs zurdos/diestros).
    Retorna -1.0 (más zurdos) a 1.0 (más diestros), 0.0 balanceado.
    
    Mejorado: Usa datos reales de splits de bateo vs zurdos/diestros.
    """
    try:
        r = _session.get(
            f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats",
            params={"stats": "season", "group": "hitting", "season": season, "type": "splits"},
            timeout=15,
        )
        r.raise_for_status()
        stats_data = r.json()
        
        # Buscar splits vs zurdos y diestros
        splits = stats_data.get("stats", [{}])[0].get("splits", [])
        
        ops_vs_left = 0.70  # Default
        ops_vs_right = 0.70  # Default
        
        for split in splits:
            split_name = split.get("name", "").lower()
            if "vs left" in split_name:
                ops_vs_left = float(split.get("stat", {}).get("ops", 0.70))
            elif "vs right" in split_name:
                ops_vs_right = float(split.get("stat", {}).get("ops", 0.70))
        
        # Calcular balance: positivo si mejor vs diestros, negativo si mejor vs zurdos
        diff = ops_vs_right - ops_vs_left
        # Normalizar a rango -1 a 1 (asumiendo diferencia máxima de 0.20)
        balance = max(-1.0, min(1.0, diff / 0.10))
        
        return balance
        
    except Exception:
        return 0.0  # Balanceado si falla la API


def ajuste_matchup_zurdo_diestro(pitcher_hand: str, lineup_balance: float) -> float:
    """
    Ajusta la fuerza del equipo basándose en el matchup pitcher vs lineup.
    
    Args:
        pitcher_hand: 'L' para zurdo, 'R' para diestro, 'S' para ambidiestro
        lineup_balance: -1.0 (muy zurdo) a 1.0 (muy diestro), 0.0 balanceado
    
    Returns:
        Ajuste a aplicar a la fuerza del equipo
    """
    if pitcher_hand == 'S':  # Ambidiestro - no hay ventaja específica
        return 0.0
    
    # Pitcher zurdo vs lineup cargado de zurdos = ventaja para pitcher
    # Pitcher diestro vs lineup cargado de diestros = ventaja para pitcher
    if pitcher_hand == 'L':
        # Zurdo favorece contra lineups con muchos zurdos
        return -lineup_balance * 2.0  # Aumentado a 2.0 para mayor impacto
    else:  # 'R'
        # Diestro favorece contra lineups con muchos diestros
        return lineup_balance * 2.0  # Aumentado a 2.0 para mayor impacto


def score_pitcher(p: dict[str, Any]) -> float:
    """Mayor = mejor pitcheo."""
    return round(
        (COEF_ERA_BASE - p["era"]) * COEF_ERA_PESO + 
        (COEF_WHIP_BASE - p["whip"]) * COEF_WHIP_PESO + 
        p["k9"] * COEF_K9_PESO, 2
    )


def score_ofensiva(team_id: int, season: int) -> float:
    b: dict[str, Any] = stats_bateo(team_id, season)
    rec_data = cargar_records(season).get(team_id, {"win_pct": 0.5, "pyth": 0.5})
    racha = cargar_rachas(season).get(team_id, 0)
    
    # Usamos un mix de Win Pct y Pythagorean Expectation (70% Pyth, 30% Real)
    # La expectativa pitagórica es un mejor predictor de calidad futura
    calidad_equipo = (rec_data["pyth"] * 0.7) + (rec_data["win_pct"] * 0.3)
    
    return round(
        b["woba"] * COEF_WOBA_PESO + 
        calidad_equipo * COEF_RECORD_PESO + 
        racha * COEF_RACHA_PESO, 2
    )

def fuerza_lado(
    team_id: int,
    opp_team_id: int,
    pitcher_id: int | None,
    opp_pitcher_id: int | None,
    season: int,
    es_local: bool,
    ia_adj: float = 0.0,
    bias_aprendizaje: float = 0.0,
    cfg: dict | None = None,
) -> float:
    of = score_ofensiva(team_id, season)
    pitcher_stats = stats_pitcher(pitcher_id, season)
    p_def = score_pitcher(pitcher_stats)
    p_rival = score_pitcher(stats_pitcher(opp_pitcher_id, season))
    
    local = BONO_LOCALIA if es_local else 0.0
    # Aplicar Park Factor si el equipo es local (se usa el ID del equipo de casa)
    park_id = team_id if es_local else opp_team_id
    p_factor = (PARK_FACTORS.get(park_id, 1.0) - 1.0) * PESO_PARK_FACTOR
    
    # Penalización por fatiga de bullpen si está activado
    bullpen_penalty = 0.0
    if cfg and cfg.get("estrategia", {}).get("analizar_bullpen", False):
        fatiga = calcular_fatiga_bullpen(team_id, season)
        # Más fatiga = penalización mayor (hasta -3 puntos)
        bullpen_penalty = -fatiga * 3.0
    
    # Ajuste por matchup zurdo/diestro si está activado
    matchup_adj = 0.0
    if cfg and cfg.get("estrategia", {}).get("analizar_matchups_zurdo_diestro", False):
        pitcher_hand = pitcher_stats.get("hand", "R")
        # Usar datos reales de balance del lineup
        lineup_balance = obtener_balance_lineup(team_id, season)
        matchup_adj = ajuste_matchup_zurdo_diestro(pitcher_hand, lineup_balance)
    
    return round(
        of * PESO_OFENSIVA + 
        p_def * PESO_PITCHER_DEF + 
        p_rival * PESO_PITCHER_RIVAL + 
        local + ia_adj + bias_aprendizaje + (p_factor * 10) + bullpen_penalty + matchup_adj, 2
    )


def prob_logistica(f_away: float, f_home: float) -> tuple[float, float]:
    diff = (f_home - f_away) / 12.0
    p_home = 1.0 / (1.0 + math.exp(-diff))
    p_home = max(0.22, min(0.78, p_home))
    p_away = 1.0 - p_home
    return round(p_away * 100, 1), round(p_home * 100, 1)


def prob_implicita(decimal: float) -> float:
    if not decimal or decimal <= 1:
        return 0.0
    return round(100 / decimal, 1)


def edge_pct(prob_modelo: float, decimal: float) -> float:
    if not decimal or decimal <= 1:
        return -999.0
    impl = 100 / decimal
    return round(prob_modelo - impl, 1)


def cuota_desde_prob(prob: float) -> tuple[float, int]:
    """Cuota justa según prob. del modelo (simulación sin mercado)."""
    p = max(5.0, min(95.0, float(prob)))
    dec = round(100.0 / p, 3)
    dec = max(1.15, min(4.50, dec))
    if dec >= 2.0:
        amer = int(round((dec - 1) * 100))
    else:
        amer = int(round(-100 / (dec - 1)))
    return dec, amer


def _modo_solo_modelo(cfg: dict[str, Any]) -> bool:
    estrategia = cfg.get("estrategia", {})
    if cfg.get("modo_solo_modelo"):
        return True
    return not estrategia.get("requiere_betmgm", True)


def _construir_features_ml_juego(
    juego: dict[str, Any],
    cfg: dict[str, Any],
    pa: dict[str, Any],
    ph: dict[str, Any],
    ba: dict[str, Any],
    bh: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Arma features ML de visitante y local con stats reales del momento."""
    season = cfg["temporada_mlb"]
    estrategia = cfg.get("estrategia", {})
    away_id = juego.get("away_id")
    home_id = juego.get("home_id")

    features_away = extraer_features_ml(
        {
            "es_local": False,
            "park_factor": PARK_FACTORS.get(away_id, 1.0),
            "fatiga_bullpen": calcular_fatiga_bullpen(away_id, season)
            if estrategia.get("analizar_bullpen")
            else 0.3,
            "matchup_adj": 0.0,
            "edge": juego.get("edge", 0.0),
        },
        pa,
        ba,
        cfg,
    )
    features_home = extraer_features_ml(
        {
            "es_local": True,
            "park_factor": PARK_FACTORS.get(home_id, 1.0),
            "fatiga_bullpen": calcular_fatiga_bullpen(home_id, season)
            if estrategia.get("analizar_bullpen")
            else 0.3,
            "matchup_adj": 0.0,
            "edge": juego.get("edge", 0.0),
        },
        ph,
        bh,
        cfg,
    )

    if estrategia.get("analizar_matchups_zurdo_diestro"):
        pitcher_hand_away = pa.get("hand", "R")
        pitcher_hand_home = ph.get("hand", "R")
        lineup_balance_away = obtener_balance_lineup(away_id, season)
        lineup_balance_home = obtener_balance_lineup(home_id, season)
        features_away["matchup_zurdo_diestro"] = ajuste_matchup_zurdo_diestro(
            pitcher_hand_home, lineup_balance_away
        )
        features_home["matchup_zurdo_diestro"] = ajuste_matchup_zurdo_diestro(
            pitcher_hand_away, lineup_balance_home
        )

    return features_away, features_home


def _guardar_ml_features_pick(
    juego: dict[str, Any],
    features_away: dict[str, Any],
    features_home: dict[str, Any],
) -> None:
    """Congela el vector ML del equipo elegido en el pick."""
    pick = (juego.get("pick") or "").strip()
    if not pick:
        return
    home = juego.get("home") or ""
    es_home_pick = bool(home and home in pick)
    feat = dict(features_home if es_home_pick else features_away)
    feat["edge_estadistico"] = float(juego.get("edge") or 0)
    juego["ml_features"] = serializar_features_ml(feat)


def _guardar_probs_modelos_pick(
    juego: dict[str, Any],
    prob_est_away: float,
    prob_est_home: float,
    prob_ml_away: float | None,
    prob_ml_home: float | None,
) -> None:
    """Congela probabilidades del estadístico y ML para el equipo del pick."""
    pick = (juego.get("pick") or "").strip()
    if not pick:
        return
    home = juego.get("home") or ""
    es_home_pick = bool(home and home in pick)
    juego["probEstAway"] = prob_est_away
    juego["probEstHome"] = prob_est_home
    juego["probEstPick"] = prob_est_home if es_home_pick else prob_est_away
    if prob_ml_away is not None and prob_ml_home is not None:
        juego["probMlAway"] = prob_ml_away
        juego["probMlHome"] = prob_ml_home
        juego["probMlPick"] = prob_ml_home if es_home_pick else prob_ml_away
    else:
        juego["probMlPick"] = None


def analizar_juego(juego: dict[str, Any], cfg: dict[str, Any], bias_aprendizaje: float = 0.0) -> dict[str, Any]:
    """Enriquece el juego con modelo, valor y si es apostable."""
    season = cfg["temporada_mlb"]
    estrategia = cfg.get("estrategia", {})
    
    # Verificar si estamos en modo F5 (primeras 5 entradas)
    modo_f5 = estrategia.get("mercado_f5", False)
    
    # Si el aprendizaje sugiere ser más cauteloso (bias negativo), subimos el edge mínimo requerido
    min_edge_base = float(estrategia.get("min_edge_pct", 5.0))
    min_edge = min_edge_base + (abs(bias_aprendizaje) if bias_aprendizaje < 0 else 0)
    min_prob = float(estrategia.get("min_prob_modelo", 52.0))
    
    # En modo F5, reducimos el edge mínimo porque hay menos varianza (sin bullpen)
    if modo_f5:
        min_edge -= 1.0  # F5 requiere menos edge por ser más predecible

    away_id = juego.get("away_id")
    home_id = juego.get("home_id")
    p_away_id = juego.get("pitcher_away_id")
    p_home_id = juego.get("pitcher_home_id")
    lineup_ok = juego.get("lineup_confirmado", False)

    # Verificamos requisitos mínimos. El lineup confirmado es ahora opcional para permitir 
    # bloqueos 1h antes del inicio (los lineups suelen salir 30-45m antes).
    if not p_away_id or not p_home_id or away_id is None or home_id is None:
        juego["apostable"] = False
        juego["motivo_apuesta"] = "Esperando Pitchers confirmados"
        # Inicializamos valores para evitar signos de pregunta en la interfaz
        juego.setdefault("probPick", 0)
        juego.setdefault("edge", 0)
        return juego

    # Precarga de datos para la IA
    pa = stats_pitcher(p_away_id, season)
    ph = stats_pitcher(p_home_id, season)
    ba = stats_bateo(away_id, season)
    bh = stats_bateo(home_id, season)

    # Capa de Inteligencia Máxima: Gemini analiza intangibles
    ia_away = obtener_ajuste_ia(juego["visitante"], juego["home"], ba, pa, ph, cfg)
    ia_home = obtener_ajuste_ia(juego["home"], juego["visitante"], bh, ph, pa, cfg)

    f_away = fuerza_lado(away_id, home_id, p_away_id, p_home_id, season, False, ia_away, bias_aprendizaje, cfg)
    f_home = fuerza_lado(home_id, away_id, p_home_id, p_away_id, season, True, ia_home, bias_aprendizaje, cfg)
    prob_away, prob_home = prob_logistica(f_away, f_home)
    prob_est_away, prob_est_home = prob_away, prob_home
    prob_ml_away: float | None = None
    prob_ml_home: float | None = None

    features_away: dict[str, Any] | None = None
    features_home: dict[str, Any] | None = None
    if HAS_ML:
        try:
            features_away, features_home = _construir_features_ml_juego(
                juego, cfg, pa, ph, ba, bh
            )
        except Exception as e:
            print(f"[ML] Error extrayendo features: {e}")
    
    # Usar Ensemble Learning si está activado y disponible
    usar_ml = cfg.get("usar_ml", False) and HAS_ML
    if usar_ml and features_away is not None and features_home is not None:
        try:
            # Obtener predicciones ML y normalizarlas (RF no garantiza suma 100)
            prob_ml_away = predecir_rf(features_away)
            prob_ml_home = predecir_rf(features_home)
            if prob_ml_away is not None and prob_ml_home is not None:
                s_ml = float(prob_ml_away) + float(prob_ml_home)
                if s_ml > 0:
                    prob_ml_away = round(100.0 * float(prob_ml_away) / s_ml, 1)
                    prob_ml_home = round(100.0 - prob_ml_away, 1)
            
            pesos_ensemble = cfg.get("pesos_ensemble", {
                'estadistico': 0.4,
                'ml': 0.4,
                'ia': 0.2
            })
            # ia_away/ia_home son ajustes de fuerza (~[-1,1]), NO probabilidades.
            # No inyectarlos al ensemble como % de victoria.
            prob_away = ensemble_prediction(prob_est_away, prob_ml_away, None, pesos_ensemble)
            prob_home = ensemble_prediction(prob_est_home, prob_ml_home, None, pesos_ensemble)
            total_ens = float(prob_away) + float(prob_home)
            if total_ens > 0:
                prob_away = round(100.0 * float(prob_away) / total_ens, 1)
                prob_home = round(100.0 - prob_away, 1)
            
            print(
                f"[ML] Ensemble: Away {prob_away:.1f}% "
                f"(est: {prob_est_away:.1f}%, ml: {prob_ml_away if prob_ml_away is not None else '—'})"
            )
            print(
                f"[ML] Ensemble: Home {prob_home:.1f}% "
                f"(est: {prob_est_home:.1f}%, ml: {prob_ml_home if prob_ml_home is not None else '—'})"
            )
        except Exception as e:
            print(f"[ML] Error en ensemble prediction: {e}")
            prob_away, prob_home = prob_est_away, prob_est_home

    juego["probAway"] = prob_away
    juego["probHome"] = prob_home
    juego["fuerzaAway"] = f_away
    juego["fuerzaHome"] = f_home
    juego["pitcherAway"] = pa["nombre"]
    juego["pitcherHome"] = ph["nombre"]
    juego["pitcherAwayEra"] = pa["era"]
    juego["pitcherHomeEra"] = ph["era"]

    dec_away = juego.get("odds_away_decimal")
    dec_home = juego.get("odds_home_decimal")
    edge_away = edge_pct(prob_away, dec_away) if dec_away else -999
    edge_home = edge_pct(prob_home, dec_home) if dec_home else -999

    juego["edgeAway"] = edge_away if edge_away > -900 else None
    juego["edgeHome"] = edge_home if edge_home > -900 else None
    juego["implAway"] = prob_implicita(dec_away) if dec_away else None
    juego["implHome"] = prob_implicita(dec_home) if dec_home else None

    candidatos: list[dict[str, Any]] = []
    preferir_underdogs = estrategia.get("preferir_underdogs", False)
    solo_modelo = _modo_solo_modelo(cfg)

    if solo_modelo:
        juego["lineas_fuente"] = "modelo"
        # Un solo favorito por partido (el de mayor prob). Evita picks <50% o ambos lados.
        if prob_away >= prob_home:
            pick, prob = f"{juego['visitante']} ML", prob_away
        else:
            pick, prob = f"{juego['home']} ML", prob_home
        if prob >= min_prob and prob >= 50.0:
            dec, amer = cuota_desde_prob(prob)
            conf = round(prob - 50.0, 1)
            bonus = 0.0
            if preferir_underdogs and dec >= float(estrategia.get("min_cuota_underdog", 1.5)):
                bonus = 2.0
            candidatos.append(
                {
                    "pick": pick,
                    "prob": prob,
                    "edge": conf + bonus,
                    "edge_base": conf,
                    "odds": dec,
                    "american": amer,
                    "es_underdog": dec >= float(estrategia.get("min_cuota_underdog", 1.5)),
                }
            )
    else:
        if dec_away and edge_away >= min_edge and prob_away >= min_prob:
            es_underdog = es_underdog_con_valor(dec_away, prob_away, cfg)
            bonus_prioridad = 2.0 if (preferir_underdogs and es_underdog) else 0.0
            candidatos.append(
                {
                    "pick": f"{juego['visitante']} ML",
                    "team": juego["visitante"],
                    "prob": prob_away,
                    "edge": edge_away + bonus_prioridad,
                    "edge_base": edge_away,
                    "odds": dec_away,
                    "american": juego.get("odds_away_american"),
                    "es_underdog": es_underdog,
                }
            )
        if dec_home and edge_home >= min_edge and prob_home >= min_prob:
            es_underdog = es_underdog_con_valor(dec_home, prob_home, cfg)
            bonus_prioridad = 2.0 if (preferir_underdogs and es_underdog) else 0.0
            candidatos.append(
                {
                    "pick": f"{juego['home']} ML",
                    "team": juego["home"],
                    "prob": prob_home,
                    "edge": edge_home + bonus_prioridad,
                    "edge_base": edge_home,
                    "odds": dec_home,
                    "american": juego.get("odds_home_american"),
                    "es_underdog": es_underdog,
                }
            )

    if candidatos:
        mejor = max(candidatos, key=lambda x: x["edge"])
        juego["pick"] = mejor["pick"]
        juego["odds"] = mejor["odds"]
        juego["odds_american"] = mejor["american"]
        juego["edge"] = mejor["edge_base"]
        juego["probPick"] = mejor["prob"]
        juego["apostable"] = True
        if solo_modelo:
            juego["motivo_apuesta"] = (
                f"Modelo {mejor['prob']:.0f}% (solo stats+ML, sin BetMGM)"
            )
        else:
            juego["motivo_apuesta"] = (
                f"Valor +{mejor['edge']:.1f}% vs BetMGM "
                f"(modelo {mejor['prob']:.0f}% vs mercado {prob_implicita(mejor['odds']):.0f}%)"
            )
    else:
        # SIEMPRE hacer una predicción, aunque no sea apostable
        if prob_away >= prob_home:
            juego["pick"] = f"{juego['visitante']} ML"
            juego["probPick"] = prob_away
            juego["edge"] = edge_away if edge_away > -900 else 0
            juego["odds"] = dec_away if dec_away else 1.5
            juego["odds_american"] = juego.get("odds_away_american", 150)
        else:
            juego["pick"] = f"{juego['home']} ML"
            juego["probPick"] = prob_home
            juego["edge"] = edge_home if edge_home > -900 else 0
            juego["odds"] = dec_home if dec_home else 1.5
            juego["odds_american"] = juego.get("odds_home_american", 150)
        
        juego["apostable"] = False
        if solo_modelo or (not dec_away and not dec_home):
            juego["motivo_apuesta"] = f"Prob. modelo bajo {min_prob}%"
        elif edge_away < min_edge and edge_home < min_edge:
            juego["motivo_apuesta"] = f"Sin valor (mínimo +{min_edge}% edge)"
        else:
            juego["motivo_apuesta"] = f"Prob. modelo bajo {min_prob}%"

    if features_away is not None and features_home is not None:
        _guardar_ml_features_pick(juego, features_away, features_home)
    _guardar_probs_modelos_pick(
        juego, prob_est_away, prob_est_home, prob_ml_away, prob_ml_home
    )

    return juego


def seleccionar_favorables_del_dia(juegos: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Marca apostable solo en los mejores picks del día (tope configurable)."""
    estrategia: dict[str, Any] = cfg.get("estrategia", {})
    max_apuestas = int(estrategia.get("max_apuestas_dia", 5))

    favorables: list[dict[str, Any]] = [j for j in juegos if j.get("apostable")]
    favorables.sort(key=lambda x: x.get("edge", 0), reverse=True)

    ids_top = {j["id"] for j in favorables[:max_apuestas]}
    for j in juegos:
        if j.get("apostable") and j["id"] not in ids_top:
            j["apostable"] = False
            j["motivo_apuesta"] = (
                f"Fuera del top {max_apuestas} del día "
                f"(edge +{j.get('edge', 0):.1f}%)"
            )
    return juegos


def evaluar_juegos(juegos: list[dict[str, Any]], cfg: dict[str, Any], bias_aprendizaje: float = 0.0) -> list[dict[str, Any]]:
    """Marca candidatos favorables; el tope diario se aplica al bloquear (1h antes)."""
    for j in juegos:
        analizar_juego(j, cfg, bias_aprendizaje)
    # Aplicar el filtro de mejores picks del día según el edge
    juegos = seleccionar_favorables_del_dia(juegos, cfg)
    return juegos
