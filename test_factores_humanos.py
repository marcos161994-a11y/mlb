"""Tests: factores humanos (viaje, serie, umpire) sin red."""

from datetime import datetime, timezone

import factores_humanos as fh
from factores_humanos import (
    aplicar_ajustes_fuerza,
    analizar_factores_humanos,
    texto_para_ia,
)


def _cache_vacio(team_ids, fecha: datetime):
    for tid in team_ids:
        fh._team_sched_cache[f"{tid}:{fecha.strftime('%Y-%m-%d')}"] = []


def test_umpire_sesgo_conocido():
    fecha = datetime(2026, 8, 12, 23, 5, tzinfo=timezone.utc)
    _cache_vacio([119, 147], fecha)
    info = analizar_factores_humanos(
        {
            "away_id": 119,
            "home_id": 147,
            "inicio_juego": fecha.isoformat(),
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
    assert info["umpire"]["hp_nombre"] == "Pat Hoberg"
    assert info["umpire"]["sesgo_runs"] < 0
    assert info["serie"]["rubber"] is True
    assert "leverage_serie" in info["features_away"]


def test_perfil_b2b_fatiga():
    team_away = 119  # LAD
    team_home = 147  # NYY
    fecha = datetime(2026, 8, 12, 23, 0, tzinfo=timezone.utc)
    prev = {
        "gameDate": "2026-08-11T23:10:00Z",
        "officialDate": "2026-08-11",
        "venue": {"name": "Yankee Stadium"},
        "teams": {
            "away": {"team": {"id": 119, "name": "Dodgers", "abbreviation": "LAD"}},
            "home": {"team": {"id": 147, "name": "Yankees", "abbreviation": "NYY"}},
        },
    }
    fh._team_sched_cache[f"{team_away}:{fecha.strftime('%Y-%m-%d')}"] = [prev]
    fh._team_sched_cache[f"{team_home}:{fecha.strftime('%Y-%m-%d')}"] = [prev]

    info = analizar_factores_humanos(
        {
            "away_id": team_away,
            "home_id": team_home,
            "inicio_juego": fecha.isoformat(),
            "series_game_number": 2,
            "games_in_series": 3,
            "day_night": "night",
            "officials": [],
        }
    )
    assert info["ok"] is True
    assert info["away"]["back_to_back"] is True
    assert info["away"]["fatiga_viaje"] >= 0.3
    f_away, f_home = aplicar_ajustes_fuerza(50.0, 50.0, info)
    assert f_away < 50.0


def test_cambio_zona_este_oeste():
    """Ayer en LA, hoy en NY → Δzona grande para el visitante."""
    fecha = datetime(2026, 8, 12, 23, 0, tzinfo=timezone.utc)
    prev = {
        "gameDate": "2026-08-11T02:10:00Z",
        "officialDate": "2026-08-11",
        "venue": {"name": "Dodger Stadium"},
        "teams": {
            "away": {"team": {"id": 147, "name": "Yankees"}},
            "home": {"team": {"id": 119, "name": "Dodgers"}},
        },
    }
    fh._team_sched_cache[f"147:{fecha.strftime('%Y-%m-%d')}"] = [prev]
    fh._team_sched_cache[f"110:{fecha.strftime('%Y-%m-%d')}"] = []
    info = analizar_factores_humanos(
        {
            "away_id": 147,
            "home_id": 110,  # BAL ET
            "inicio_juego": fecha.isoformat(),
            "officials": [],
        }
    )
    assert info["away"]["cambio_zona"] >= 2
    assert info["away"]["fatiga_viaje"] >= 0.4


def test_texto_ia_y_features_ml():
    info = {
        "ok": True,
        "resumen": "AWAY: B2B · Δzona 3h",
        "alertas": ["Visita fatigada"],
        "ajuste_away": -1.5,
        "ajuste_home": 0.0,
    }
    txt = texto_para_ia(info)
    assert "Factores humanos" in txt
    assert "ALERTA" in txt

    from ml_predictor import FEATURE_COLUMNS, FEATURE_SCHEMA_VERSION, extraer_features_ml

    assert FEATURE_SCHEMA_VERSION >= 4
    assert "fatiga_viaje" in FEATURE_COLUMNS
    feats = extraer_features_ml(
        {
            "es_local": False,
            "park_factor": 1.0,
            "fatiga_viaje": 0.6,
            "dias_descanso": 0,
            "cambio_zona": 3,
            "leverage_serie": 1.0,
            "umpire_runs": -0.2,
        },
        {"era": 3.5, "whip": 1.1, "k9": 9.0},
        {"woba": 0.32, "ops": 0.75, "win_pct": 0.55},
        {},
    )
    assert feats["fatiga_viaje"] == 0.6
    assert feats["cambio_zona"] == 3.0
