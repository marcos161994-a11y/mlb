"""Tests del historial oficial (L10 + pitcher vs rival)."""

from historico_oficial import (
    analizar_historico_oficial,
    aplicar_ajustes_fuerza,
    limpiar_caches,
    texto_para_ia,
)
from mente_mlb import construir_briefing, mente_conclusion


def setup_function():
    limpiar_caches()


def test_analizar_con_mocks(monkeypatch):
    monkeypatch.setattr(
        "historico_oficial.cargar_l10",
        lambda season, timeout=10.0: {
            136: {"ok": True, "wins": 2, "losses": 8, "pct": 0.2, "marca": "2-8"},
            147: {"ok": True, "wins": 8, "losses": 2, "pct": 0.8, "marca": "8-2"},
        },
    )

    def fake_pvr(pitcher_id, rival_id, season, timeout=10.0):
        if pitcher_id == 1:
            return {
                "ok": True,
                "gs": 2,
                "ip": 8.0,
                "era": 7.5,
                "calidad": "malo",
                "motivo": "ERA 7.5 en 8.0 IP vs rival",
                "ops_contra": 0.95,
            }
        return {
            "ok": True,
            "gs": 2,
            "ip": 12.0,
            "era": 2.25,
            "calidad": "bueno",
            "motivo": "ERA 2.25 en 12.0 IP vs rival",
            "ops_contra": 0.55,
        }

    monkeypatch.setattr("historico_oficial.pitcher_vs_rival", fake_pvr)

    info = analizar_historico_oficial(
        {
            "away_id": 136,
            "home_id": 147,
            "pitcher_away_id": 1,
            "pitcher_home_id": 2,
            "fecha": "2026-08-12",
        },
        season=2026,
    )
    assert info["ok"]
    assert info["l10_away"]["forma"] == "fria"
    assert info["l10_home"]["forma"] == "caliente"
    assert info["pitcher_vs_rival_away"]["calidad"] == "malo"
    assert info["pitcher_vs_rival_home"]["calidad"] == "bueno"
    assert "l10_fria_away" in info["alertas"]
    assert info["riesgo"] is True
    assert "L10" in info["resumen"]
    fa, fh = aplicar_ajustes_fuerza(50.0, 50.0, info)
    assert fa < 50.0  # visita fría + SP malo
    assert fh > 50.0  # local caliente + SP bueno
    assert "Historial oficial" in texto_para_ia(info)


def test_mente_pasa_por_l10_fria_del_pick():
    juego = {
        "id": "h1",
        "visitante": "Mariners",
        "home": "Yankees",
        "pick": "Mariners ML",
        "probPick": 60,
        "edge": 7.0,
        "odds": 2.1,
        "lineas_fuente": "oddspapi",
        "historico_oficial": {
            "ok": True,
            "riesgo": True,
            "resumen": "L10 visita 2-8",
            "alertas": ["l10_fria_away"],
            "l10_away": {"ok": True, "wins": 2, "losses": 8, "marca": "2-8", "forma": "fria"},
            "l10_home": {"ok": True, "wins": 6, "losses": 4, "marca": "6-4", "forma": "buena"},
            "pitcher_vs_rival_away": {"calidad": "neutro"},
            "pitcher_vs_rival_home": {"calidad": "neutro"},
        },
    }
    cfg = {
        "usar_mente": True,
        "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    }
    c = mente_conclusion(juego, cfg, {}, forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert c["autoriza_dinero"] is False
    assert any("L10" in r or "fria" in r.lower() or "Historial" in r for r in c["razones"])


def test_mente_no_castiga_l10_fria_del_rival():
    """Si el rival está frío, eso favorece al pick: no debe PASAR por eso."""
    juego = {
        "id": "h2",
        "visitante": "Mariners",
        "home": "Yankees",
        "pick": "Yankees ML",
        "probPick": 61,
        "edge": 9.0,
        "odds": 1.9,
        "lineas_fuente": "oddspapi",
        "pitcherAway": "X",
        "pitcherHome": "Y",
        "historico_oficial": {
            "ok": True,
            "riesgo": True,
            "resumen": "L10 visita 2-8",
            "alertas": ["l10_fria_away"],
            "l10_away": {"ok": True, "wins": 2, "losses": 8, "marca": "2-8", "forma": "fria"},
            "l10_home": {"ok": True, "wins": 5, "losses": 5, "marca": "5-5", "forma": "neutra"},
            "pitcher_vs_rival_away": {"calidad": "neutro"},
            "pitcher_vs_rival_home": {"calidad": "neutro"},
        },
    }
    cfg = {
        "usar_mente": True,
        "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    }
    c = mente_conclusion(juego, cfg, {}, forzar=True, solo_local=True)
    assert c["decision"] == "APOSTAR"
    assert c["autoriza_dinero"] is True


def test_briefing_incluye_historico():
    b = construir_briefing(
        {
            "pick": "X ML",
            "probPick": 58,
            "edge": 5,
            "lineas_fuente": "oddspapi",
            "historico_oficial": {
                "ok": True,
                "riesgo": True,
                "resumen": "L10 visita 2-8 · SP visita malo",
                "alertas": ["l10_fria_away", "pvr_malo_away"],
                "l10_away": {"marca": "2-8"},
                "l10_home": {"marca": "6-4"},
                "pitcher_vs_rival_away": {"calidad": "malo"},
                "pitcher_vs_rival_home": {"calidad": "neutro"},
            },
        }
    )
    assert b["ok"]
    assert b["pilares"]["historico"]["ok"]
    assert "historico" in b["alertas"] or "l10_fria_away" in b["alertas"]
    assert "2-8" in (b["resumen"] or "") or "L10" in (b["resumen"] or "")
