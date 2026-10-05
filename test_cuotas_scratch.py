"""Tests unitarios: edge vs mercado + scratch/lineup (sin red)."""

from __future__ import annotations

from lineas_betmgm import american_a_decimal, normalizar_nombre_equipo
from lineup_scratch import analizar_scratch_lineup, pick_afectado_por_scratch, parsear_lineups_juego
from modelo_mlb import edge_pct, prob_implicita


def test_aliases_odds():
    assert normalizar_nombre_equipo("Oakland Athletics") == "athletics"
    assert normalizar_nombre_equipo("LA Dodgers") == "los angeles dodgers"
    assert normalizar_nombre_equipo("St. Louis Cardinals") == "st louis cardinals"


def test_american_decimal_roundtrip():
    assert american_a_decimal(-110) == 1.909
    assert american_a_decimal(150) == 2.5


def test_edge_vs_mercado():
    # Modelo 60% vs cuota -110 (~47.6% implícita) → edge claro
    dec = american_a_decimal(-110)
    e = edge_pct(60.0, dec)
    assert e > 6.0
    # Modelo 48% vs -110 → sin valor
    assert edge_pct(48.0, dec) < 6.0


def test_parsear_lineups():
    raw = {
        "lineups": {
            "awayPlayers": [{"id": 1, "fullName": "A"}, {"id": 2, "fullName": "B"}],
            "homePlayers": [{"id": 3, "fullName": "C"}],
        }
    }
    lu = parsear_lineups_juego(raw)
    assert lu["confirmado"] is True
    assert len(lu["away"]) == 2
    assert lu["home"][0]["id"] == 3


def test_scratch_sp_cambia():
    info = analizar_scratch_lineup(
        away_id=None,
        home_id=None,
        pitcher_away_id=10,
        pitcher_home_id=20,
        pitcher_away_nombre="Nuevo",
        pitcher_home_nombre="Igual",
        lineups={"away": [], "home": [], "confirmado": False},
        season=2026,
        pred_congelada={
            "pitcher_away_id": 99,
            "pitcher_home_id": 20,
            "pitcherAway": "Viejo",
            "pitcherHome": "Igual",
        },
        min_estrellas_fuera=2,
    )
    assert info["scratch_away"] is True
    assert info["scratch_home"] is False
    assert info["riesgo"] is True
    assert pick_afectado_por_scratch("Yankees ML", "Yankees", "Red Sox", info) is True


def _lineup_dos_fuera(**kwargs):
    import lineup_scratch as ls

    top = [
        {"id": 1, "nombre": "Lefty Uno", "ops": 0.900, "pa": 400, "batea": "L"},
        {"id": 2, "nombre": "Lefty Dos", "ops": 0.850, "pa": 380, "batea": "L"},
        {"id": 3, "nombre": "Righty", "ops": 0.820, "pa": 360, "batea": "R"},
        {"id": 4, "nombre": "Switch", "ops": 0.800, "pa": 340, "batea": "S"},
        {"id": 5, "nombre": "En Lineup", "ops": 0.780, "pa": 320, "batea": "R"},
    ]

    def _top(team_id, season, n=5):
        return top[:n]

    def _mano(pitcher_id, explicita=None):
        # Si el test pasó la mano, no hay red. Si no, tampoco: mano desconocida.
        from lineup_scratch import _codigo_mano

        return _codigo_mano(explicita)

    monkeypatch = kwargs["monkeypatch"]
    monkeypatch.setattr(ls, "top_bateadores_equipo", _top)
    monkeypatch.setattr(ls, "mano_de_pitcher", _mano)
    return analizar_scratch_lineup(
        away_id=100,
        home_id=200,
        pitcher_away_id=10,
        pitcher_home_id=20,
        pitcher_away_nombre="Starter Visitante",
        pitcher_home_nombre="Starter Local",
        pitcher_away_mano=kwargs.get("mano_away"),
        pitcher_home_mano=kwargs.get("mano_home"),
        lineups={
            "away": kwargs["away_lineup"],
            "home": [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}],
            "confirmado": True,
        },
        season=2026,
        min_estrellas_fuera=2,
    )


def test_platoon_zurdo_vs_lhp_no_cuenta_como_scratch(monkeypatch):
    """Dos zurdos sentados contra un LHP no llegan al umbral de scratch."""
    info = _lineup_dos_fuera(
        monkeypatch=monkeypatch,
        mano_away="R",
        mano_home="L",
        # El visitante enfrenta al local zurdo. Solo se sientan los dos zurdos.
        away_lineup=[
            {"id": 3, "nombre": "Righty"},
            {"id": 4, "nombre": "Switch"},
            {"id": 5, "nombre": "En Lineup"},
        ],
    )
    assert info["riesgo"] is False
    assert info["estrellas_fuera_away"] == []
    assert len(info["platoon_fuera_away"]) == 2
    assert info["ajuste_away"] == 0.0
    assert "no cuenta" in (info["resumen"] or "").lower()
    assert pick_afectado_por_scratch("Visitante ML", "Visitante", "Local", info) is False


def test_zurdo_vs_rhp_sigue_siendo_scratch(monkeypatch):
    info = _lineup_dos_fuera(
        monkeypatch=monkeypatch,
        mano_away="L",
        mano_home="R",
        away_lineup=[
            {"id": 3, "nombre": "Righty"},
            {"id": 4, "nombre": "Switch"},
            {"id": 5, "nombre": "En Lineup"},
        ],
    )
    assert info["riesgo"] is True
    assert len(info["estrellas_fuera_away"]) == 2
    assert info["platoon_fuera_away"] == []
    assert info["ajuste_away"] == -min(3.5, 1.2 * 2)


def test_platoon_mas_un_diestro_no_llega_al_umbral(monkeypatch):
    """Un zurdo de platoon no se suma a un diestro ausente."""
    info = _lineup_dos_fuera(
        monkeypatch=monkeypatch,
        mano_home="L",
        away_lineup=[
            {"id": 2, "nombre": "Lefty Dos"},
            {"id": 4, "nombre": "Switch"},
            {"id": 5, "nombre": "En Lineup"},
        ],
    )
    nombres = {j["nombre"] for j in info["estrellas_fuera_away"]}
    platoon = {j["nombre"] for j in info["platoon_fuera_away"]}
    assert nombres == {"Righty"}
    assert platoon == {"Lefty Uno"}
    assert info["riesgo"] is False


def test_ambidiestro_vs_lhp_si_cuenta(monkeypatch):
    """Un switch no descansa por platoon contra un zurdo."""
    info = _lineup_dos_fuera(
        monkeypatch=monkeypatch,
        mano_home="L",
        away_lineup=[
            {"id": 1, "nombre": "Lefty Uno"},
            {"id": 2, "nombre": "Lefty Dos"},
            {"id": 5, "nombre": "En Lineup"},
        ],
    )
    nombres = {j["nombre"] for j in info["estrellas_fuera_away"]}
    assert nombres == {"Righty", "Switch"}
    assert info["riesgo"] is True


def test_sin_mano_el_ausente_sigue_contando(monkeypatch):
    """Si no se sabe la mano, no se descarta la ausencia como platoon."""
    info = _lineup_dos_fuera(
        monkeypatch=monkeypatch,
        away_lineup=[
            {"id": 3, "nombre": "Righty"},
            {"id": 4, "nombre": "Switch"},
            {"id": 5, "nombre": "En Lineup"},
        ],
    )
    assert info["riesgo"] is True
    assert len(info["estrellas_fuera_away"]) == 2
    assert info["platoon_fuera_away"] == []


def test_estrellas_fuera_bloquea_pick():
    info = {
        "ok": True,
        "scratch_away": False,
        "scratch_home": False,
        "estrellas_fuera_away": [{"id": 1}, {"id": 2}],
        "estrellas_fuera_home": [],
        "min_estrellas_fuera": 2,
        "riesgo": True,
    }
    assert pick_afectado_por_scratch("Mets ML", "Mets", "Phillies", info) is True
    assert pick_afectado_por_scratch("Phillies ML", "Mets", "Phillies", info) is False


def test_mano_stub_no_se_trata_como_conocida():
    from modelo_mlb import _mano_pitcher_conocida

    assert _mano_pitcher_conocida({"hand": "R", "metricas_fuente": "error"}) is None
    assert _mano_pitcher_conocida({"hand": "R", "metricas_fuente": "default"}) is None
    assert _mano_pitcher_conocida({"hand": "L", "metricas_fuente": "basic"}) == "L"


def test_prob_implicita():
    assert abs(prob_implicita(2.0) - 50.0) < 0.01
