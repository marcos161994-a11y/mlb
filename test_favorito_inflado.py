"""Regla favorito inflado: prob alta + edge bajo → solo papel (lección de pérdidas reales)."""

from mente_mlb import mente_conclusion
from modelo_mlb import bloqueado_favorito_inflado


CFG = {
    "usar_mente": True,
    "mente": {"modo": "normal", "min_confianza": 3, "requiere_mercado": True},
    "stake_por_juego": 3,
    "estrategia": {
        "min_edge_pct": 6.0,
        "min_prob_modelo": 58.0,
        "favorito_inflado": {
            "activo": True,
            "umbral_prob": 62.0,
            "min_edge_pct": 15.0,
        },
    },
}


def _juego(prob, edge, gid="x"):
    return {
        "id": gid,
        "visitante": "Away Team",
        "home": "Home Team",
        "pick": "Away Team ML",
        "probPick": prob,
        "edge": edge,
        "odds": 2.05,
        "odds_away_decimal": 2.05,
        "lineas_fuente": "draftkings",
    }


def test_bloquea_perdidas_reales():
    """Padres 62.3%/+13.5, Pirates 62.3%/+14.2, Brewers 62.5%/+8.4 → solo papel."""
    for prob, edge in ((62.3, 13.5), (62.3, 14.2), (62.5, 8.4)):
        ok, msg = bloqueado_favorito_inflado(_juego(prob, edge), CFG)
        assert ok is True
        assert "solo papel" in msg.lower() or "exige edge" in msg.lower()


def test_permite_ganancias_reales():
    """Royals 60.7%/+9.7, D-backs 58.6%/+8.1, Braves 55.9%/+10.2 → no bloqueo."""
    for prob, edge in ((60.7, 9.7), (58.6, 8.1), (55.9, 10.2)):
        ok, _ = bloqueado_favorito_inflado(_juego(prob, edge), CFG)
        assert ok is False


def test_permite_edge_alto():
    ok, _ = bloqueado_favorito_inflado(_juego(65.0, 16.0), CFG)
    assert ok is False


def test_mente_pasa_favorito_inflado():
    j = _juego(62.3, 13.5, gid="padres")
    c = mente_conclusion(j, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "PASAR"
    assert c["autoriza_dinero"] is False
    assert any("favorito inflado" in r.lower() for r in c.get("razones") or [])


def test_mente_apostar_bajo_umbral_prob():
    j = _juego(61.0, 9.0, gid="royals")
    c = mente_conclusion(j, CFG, {}, forzar=True, solo_local=True)
    assert c["decision"] == "APOSTAR"
    assert c["autoriza_dinero"] is True


def test_config_favorito_inflado_activo():
    import json
    from pathlib import Path

    cfg = json.loads(Path("config_experimento.json").read_text(encoding="utf-8"))
    fi = (cfg.get("estrategia") or {}).get("favorito_inflado") or {}
    assert fi.get("activo") is True
    assert float(fi.get("umbral_prob") or 0) == 62.0
    assert float(fi.get("min_edge_pct") or 0) == 15.0
