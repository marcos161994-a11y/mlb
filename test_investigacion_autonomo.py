"""Ajustes autónomos de acierto: FIP/suerte, getaway, veto y bitácora."""

from datetime import datetime, timezone

import factores_humanos as fh
from factores_humanos import analizar_factores_humanos
from mente_bitacora import cargar_bitacora, resumen_bitacora
from mente_mlb import mente_conclusion
from modelo_mlb import (
    _score_pitcher_crudo,
    ajuste_suerte_fip_era,
    score_pitcher,
    shrink_muestra_pitcher,
)


CFG = {
    "usar_mente": True,
    "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    "stake_por_juego": 5,
    "estrategia": {
        "min_stake_pct": 1,
        "max_stake_pct": 5,
        "favorito_inflado": {"activo": False},
    },
}


def test_suerte_fip_penaliza_era_de_moda():
    lucky = {"era": 2.10, "fip": 3.40, "xfip": 3.40, "whip": 1.10, "k9": 8.5, "bb9": 2.8, "k_pct": 22, "bb_pct": 7, "ip": 80}
    unlucky = {**lucky, "era": 4.30, "fip": 3.40}
    assert ajuste_suerte_fip_era(lucky) < 0
    assert ajuste_suerte_fip_era(unlucky) > 0
    gap_crudo = _score_pitcher_crudo(lucky) - _score_pitcher_crudo(unlucky)
    gap_final = score_pitcher(lucky) - score_pitcher(unlucky)
    assert gap_final < gap_crudo


def test_shrink_novato_hacia_liga():
    elite = {"era": 2.2, "fip": 2.3, "xfip": 2.4, "whip": 0.95, "k9": 11, "bb9": 2.0, "k_pct": 30, "bb_pct": 6, "ip": 12}
    mismo_con_muestra = {**elite, "ip": 90}
    assert score_pitcher(elite) < score_pitcher(mismo_con_muestra)
    assert shrink_muestra_pitcher(10.0, {"ip": 10}, liga=0.0) < 10.0


def test_getaway_penaliza_visita():
    fecha = datetime(2026, 8, 12, 23, 0, tzinfo=timezone.utc)
    fh._team_sched_cache[f"119:{fecha.strftime('%Y-%m-%d')}"] = []
    fh._team_sched_cache[f"147:{fecha.strftime('%Y-%m-%d')}"] = []
    info = analizar_factores_humanos(
        {
            "away_id": 119,
            "home_id": 147,
            "inicio_juego": fecha.isoformat(),
            "series_game_number": 2,
            "games_in_series": 2,
            "day_night": "day",
            "officials": [],
        }
    )
    assert info["serie"]["getaway"] is True
    assert info["ajuste_away"] <= -0.20


def test_mente_pasa_era_de_suerte():
    juego = {
        "id": "suerte",
        "visitante": "Away",
        "home": "Home",
        "pick": "Home ML",
        "probPick": 60,
        "edge": 7.0,
        "odds": 1.90,
        "lineas_fuente": "draftkings",
        "pitcherAwayEra": 4.2,
        "pitcherHomeEra": 2.0,
        "pitcherAwayFip": 4.1,
        "pitcherHomeFip": 3.0,
    }
    c = mente_conclusion(juego, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert any("suerte" in r.lower() or "fip" in r.lower() for r in c["razones"])


def test_mente_pasa_alta_conv_sin_fip():
    juego = {
        "id": "alta-fip",
        "visitante": "Away",
        "home": "Home",
        "pick": "Home ML",
        "probPick": 67,
        "edge": 8.0,
        "odds": 1.70,
        "lineas_fuente": "draftkings",
        "pitcherAwayEra": 3.4,
        "pitcherHomeEra": 4.1,
        "pitcherAwayFip": 3.20,
        "pitcherHomeFip": 4.20,
    }
    c = mente_conclusion(juego, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert any("fip" in r.lower() for r in c["razones"])


def test_bitacora_tiene_investigacion_y_cambios():
    data = cargar_bitacora()
    assert data["ok"] is True
    assert len(data["entradas"]) >= 4
    tipos = {e.get("tipo") for e in data["entradas"]}
    assert "investigacion" in tipos
    assert "cambio" in tipos
    meta = resumen_bitacora()
    assert meta["total"] == len(data["entradas"])
    assert any("modelo_mlb.py" in (e.get("archivos") or []) for e in data["entradas"])


def test_servidor_expone_bitacora():
    from pathlib import Path

    src = Path("servidor_mlb.py").read_text(encoding="utf-8")
    assert '@app.get("/api/mente-bitacora")' in src
    html = Path("diagrama/index.html").read_text(encoding="utf-8")
    assert "abrirBitacora" in html
    assert "bitacora-panel" in html
    assert Path("diagrama/bitacora.json").exists()
