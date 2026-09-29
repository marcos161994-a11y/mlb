import clv_mlb as clv
from lineas_espn import aplicar_lineas_espn, parsear_scoreboard_espn


def _odds(provider, away_ml, home_ml):
    return {
        "provider": {"name": provider},
        "awayTeamOdds": {"moneyLine": away_ml},
        "homeTeamOdds": {"moneyLine": home_ml},
    }


def _payload(*bloques):
    return {
        "events": [
            {
                "id": "401",
                "competitions": [
                    {
                        "competitors": [
                            {"homeAway": "away", "team": {"displayName": "New York Yankees"}},
                            {"homeAway": "home", "team": {"displayName": "Boston Red Sox"}},
                        ],
                        "odds": list(bloques),
                    }
                ],
            }
        ]
    }


def test_scoreboard_guarda_todas_las_casas():
    mapa = parsear_scoreboard_espn(
        _payload(
            _odds("DraftKings", -110, -110),
            _odds("Pinnacle", -105, -105),
            _odds("BetMGM", -115, -105),
        )
    )
    fila = next(iter(mapa.values()))
    casas = [b["casa"] for b in fila["libros"]]
    assert casas == ["draftkings", "pinnacle", "betmgm"]
    assert fila["away"]["casa"] == "draftkings"


def test_casa_repetida_no_se_duplica():
    mapa = parsear_scoreboard_espn(
        _payload(_odds("DraftKings", -110, -110), _odds("DraftKings", -112, -108))
    )
    fila = next(iter(mapa.values()))
    assert len(fila["libros"]) == 1


def test_casa_sin_moneyline_no_rompe_el_resto():
    mapa = parsear_scoreboard_espn(
        _payload(
            {"provider": {"name": "ESPN BET"}},
            _odds("Pinnacle", -105, -105),
        )
    )
    fila = next(iter(mapa.values()))
    assert [b["casa"] for b in fila["libros"]] == ["pinnacle"]


def test_aplicar_lineas_pasa_los_libros_al_juego(monkeypatch):
    mapa = parsear_scoreboard_espn(
        _payload(_odds("DraftKings", -110, -110), _odds("Pinnacle", -104, -106))
    )
    monkeypatch.setattr("lineas_espn.obtener_lineas_espn", lambda *a, **k: (mapa, {"ok": True}))
    juegos = [{"visitante": "New York Yankees", "home": "Boston Red Sox"}]
    juegos, _meta = aplicar_lineas_espn(juegos)
    casas = [b["casa"] for b in juegos[0]["lineas_libros"]]
    assert casas == ["draftkings", "pinnacle"]


def test_clv_usa_pinnacle_cuando_esta():
    juego = {
        "pick": "Yankees ML",
        "visitante": "Yankees",
        "home": "Red Sox",
        "lineas_libros": [
            {"casa": "draftkings", "away": 2.00, "home": 1.85},
            {"casa": "pinnacle", "away": 2.10, "home": 1.80},
        ],
    }
    away, home, fuente = clv.cuotas_cierre(juego)
    assert (away, home, fuente) == (2.10, 1.80, "pinnacle")


def test_clv_cae_a_la_mediana_sin_pinnacle():
    juego = {
        "lineas_libros": [
            {"casa": "draftkings", "away": 2.00, "home": 1.85},
            {"casa": "betmgm", "away": 2.10, "home": 1.80},
            {"casa": "fanduel", "away": 2.20, "home": 1.75},
        ]
    }
    away, home, fuente = clv.cuotas_cierre(juego)
    assert (away, home) == (2.10, 1.80)
    assert fuente == "mediana_3_casas"


def test_una_sola_casa_cierra_con_su_propio_precio():
    juego = {"lineas_libros": [{"casa": "draftkings", "away": 2.00, "home": 1.85}]}
    away, home, fuente = clv.cuotas_cierre(juego)
    assert (away, home) == (2.00, 1.85)
    assert fuente == "casa_unica_draftkings"


def test_sin_los_dos_lados_no_hay_cierre():
    assert clv.cuotas_cierre({"odds": 1.95})[2] == "sin_cierre"
    assert clv.cuotas_cierre({"lineas_libros": [{"casa": "dk", "away": 2.0}]})[2] == "sin_cierre"


def test_clv_marca_movimiento_a_favor_y_en_contra():
    def _clv(cierre_away):
        reg = {"pick": "Yankees ML", "visitante": "Yankees", "home": "Red Sox", "odds": 2.00}
        juego = {
            "pick": "Yankees ML",
            "visitante": "Yankees",
            "home": "Red Sox",
            "lineas_libros": [{"casa": "draftkings", "away": cierre_away, "home": 1.85}],
        }
        clv.actualizar_clv_registro(reg, juego, fase="entrada")
        clv.actualizar_clv_registro(reg, juego, fase="cierre")
        return reg["clv_pct"]

    # El precio del pick se acorta tras la entrada: la línea se movió a favor.
    assert _clv(1.80) > 0
    # El precio se alarga: apostamos peor que el cierre.
    assert _clv(2.30) < 0


def test_registro_guarda_clv_con_varias_casas():
    reg = {
        "pick": "Yankees ML",
        "visitante": "Yankees",
        "home": "Red Sox",
        "odds": 1.95,
    }
    juego = {
        "pick": "Yankees ML",
        "visitante": "Yankees",
        "home": "Red Sox",
        "lineas_libros": [
            {"casa": "draftkings", "away": 1.90, "home": 1.95},
            {"casa": "betmgm", "away": 1.88, "home": 1.98},
        ],
    }
    assert clv.actualizar_clv_registro(reg, juego, fase="entrada") is True
    assert clv.actualizar_clv_registro(reg, juego, fase="cierre") is True
    assert reg["clv_fuente"] == "mediana_2_casas"
    assert reg["clv_pct"] is not None
