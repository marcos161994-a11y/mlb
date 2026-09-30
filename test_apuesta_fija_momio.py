"""Stake fijo, pago a la americana y cadena de momios."""

from pathlib import Path

import pytest

from cadena_momios import (
    american_con_vig,
    aplicar_cadena_momios,
    aplicar_momio_estimado,
    profit_moneyline_americano,
)
from modelo_mlb import apostable_para_dinero, es_momio_estimado, seleccionar_favorables_del_dia
from servidor_mlb import liquidar_apuesta, resumen_predicciones_y_dinero


def test_pago_americano_ejemplos_del_dueno():
    assert profit_moneyline_americano(3, -150, "ganada") == pytest.approx(2.00)
    assert profit_moneyline_americano(3, 130, "ganada") == pytest.approx(3.90)
    assert profit_moneyline_americano(3, 100, "ganada") == pytest.approx(3.00)
    assert profit_moneyline_americano(3, -100, "ganada") == pytest.approx(3.00)
    assert profit_moneyline_americano(3, -110, "ganada") == pytest.approx(2.73)
    assert profit_moneyline_americano(3, -150, "perdida") == pytest.approx(-3.00)
    assert profit_moneyline_americano(3, 130, "push") == pytest.approx(0.0)
    assert profit_moneyline_americano(3, -110, "void") == pytest.approx(0.0)


def test_cincuenta_porciento_es_menos_110():
    assert american_con_vig(50) == -110
    assert american_con_vig(60) < -100
    assert american_con_vig(40) >= 100


def _final(ganador):
    return {
        "id": "9",
        "estado": "FINALIZADO",
        "ganador": ganador,
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "scoreAway": 5,
        "scoreHome": 2,
    }


def test_liquidar_usa_el_momio_congelado_no_uno_posterior():
    apuesta = {
        "pick": "New York Yankees ML",
        "estado": "pendiente",
        "odds": 9.99,
        "odds_american": -150,
        "fuente_momio": "draftkings",
        "stake": 3.0,
    }
    assert liquidar_apuesta(apuesta, _final("New York Yankees"), 3.0) is True
    assert apuesta["estado"] == "ganada"
    assert apuesta["profit"] == pytest.approx(2.00)
    assert apuesta["stake"] == pytest.approx(3.0)


def test_perdida_y_push_en_apuesta_nueva():
    perdida = {
        "pick": "Boston Red Sox ML",
        "estado": "pendiente",
        "odds_american": 130,
        "fuente_momio": "pinnacle",
        "stake": 3,
        "odds": 2.3,
    }
    assert liquidar_apuesta(perdida, _final("New York Yankees"), 99) is True
    assert perdida["profit"] == pytest.approx(-3.00)
    assert perdida["stake"] == pytest.approx(3)

    empate = {
        "pick": "Boston Red Sox ML",
        "estado": "pendiente",
        "odds_american": -110,
        "fuente_momio": "estimado",
        "stake": 3,
    }
    juego = {
        **_final("Boston Red Sox"),
        "ganador": None,
        "scoreAway": 4,
        "scoreHome": 4,
    }
    assert liquidar_apuesta(empate, juego, 3) is True
    assert empate["estado"] == "push"
    assert empate["profit"] == pytest.approx(0.0)


def test_historial_sin_fuente_momio_no_se_reescribe():
    apuesta = {
        "pick": "New York Yankees ML",
        "estado": "pendiente",
        "odds": 2.0,
        "odds_american": -150,
        "stake": 5.0,
    }
    assert liquidar_apuesta(apuesta, _final("New York Yankees"), 5.0) is True
    assert apuesta["profit"] == pytest.approx(5.0)

    final = _final("New York Yankees")
    marcador = (
        f"{final['visitante']} {final['scoreAway']} - {final['home']} {final['scoreHome']}"
    )
    ya = {
        "pick": "New York Yankees ML",
        "estado": "ganada",
        "odds": 1.8,
        "odds_american": -150,
        "profit": 4.0,
        "stake": 5.0,
        "marcador_final": marcador,
    }
    assert liquidar_apuesta(ya, final, 5.0) is False
    assert ya["profit"] == pytest.approx(4.0)
    assert ya["stake"] == pytest.approx(5.0)


def test_cadena_elige_la_siguiente_casa_y_no_inventa():
    juegos = [
        {
            "visitante": "New York Yankees",
            "home": "Boston Red Sox",
            "lineas_libros": [
                {
                    "casa": "draftkings",
                    "away": 1.909,
                    "home": 1.909,
                    "ml_away": -110,
                    "ml_home": -110,
                }
            ],
        }
    ]
    def fetch_espn(js, _cfg):
        return js, {"ok": True, "mensaje": "stub"}

    def fetch_header():
        raise AssertionError("no debía pedir el header: DraftKings ya cotiza")

    def fetch_action(_cfg, _juegos):
        raise AssertionError("no debía pedir Action Network")

    cfg = {
        "lineas": {
            "bookmakers": "pinnacle,draftkings,fanduel",
            "cadena_momios": ["espn_scoreboard", "espn_header", "action_network"],
        }
    }
    out, meta = aplicar_cadena_momios(
        juegos, cfg, fetch_espn=fetch_espn, fetch_header=fetch_header, fetch_action=fetch_action
    )
    juego = out[0]
    assert juego["fuente_momio"] == "draftkings"
    assert juego["paso_momio"] == "espn_scoreboard"
    assert juego["origen_momio"] == "espn_scoreboard:draftkings"
    assert juego["odds_away_american"] == -110
    assert meta["reales"] == 1
    fallos = " ".join(i["fuente"] for i in juego["momio_intentos"] if not i["ok"])
    assert "pinnacle" in fallos
    assert "fanduel" not in fallos


def test_header_y_action_network_entran_si_el_anterior_no_cotiza():
    from lineas_betmgm import _match_key

    key = _match_key("New York Yankees", "Boston Red Sox")
    partido = {"visitante": "New York Yankees", "home": "Boston Red Sox"}
    cfg = {"lineas": {"bookmakers": "pinnacle,draftkings,fanduel,betmgm"}}

    def vacio(js, _cfg):
        return js, {"ok": False, "mensaje": "ESPN scoreboard caído"}

    header = {
        key: {
            "away": {"casa": "fanduel", "american": -105, "decimal": 1.952},
            "home": {"casa": "fanduel", "american": -115, "decimal": 1.87},
        }
    }

    def fetch_action(_cfg, _juegos):
        raise AssertionError("header ya cotizó; no hace falta Action Network")

    out, _meta = aplicar_cadena_momios(
        [dict(partido)],
        cfg,
        fetch_espn=vacio,
        fetch_header=lambda: (header, {"ok": True, "mensaje": "header"}),
        fetch_action=fetch_action,
    )
    assert out[0]["origen_momio"] == "espn_header:fanduel"
    assert out[0]["paso_momio"] == "espn_header"

    action = {
        key: {
            "libros": [
                {
                    "casa": "betmgm",
                    "provider": "BetMGM",
                    "ml_away": 130,
                    "ml_home": -150,
                    "away": 2.3,
                    "home": 1.667,
                }
            ]
        }
    }
    out2, _meta2 = aplicar_cadena_momios(
        [dict(partido)],
        cfg,
        fetch_espn=vacio,
        fetch_header=lambda: ({}, {"ok": False, "mensaje": "ESPN header vacío"}),
        fetch_action=lambda _cfg, _juegos: (action, {"ok": True, "mensaje": "action"}),
    )
    assert out2[0]["origen_momio"] == "action_network:betmgm"
    assert out2[0]["paso_momio"] == "action_network"
    assert out2[0]["odds_away_american"] == 130
    fallos = " ".join(i["fuente"] for i in out2[0]["momio_intentos"] if not i["ok"])
    assert "espn_scoreboard" in fallos
    assert "espn_header" in fallos


def test_si_todas_las_casas_fallan_se_estima_y_no_se_salta():
    juegos = [{"visitante": "New York Yankees", "home": "Boston Red Sox", "probAway": 60, "probHome": 40}]

    def fetch_espn(js, _cfg):
        return js, {"ok": False, "mensaje": "ESPN scoreboard caído"}

    def fetch_header():
        return {}, {"ok": False, "mensaje": "ESPN header vacío"}

    def fetch_action(_cfg, _juegos):
        return {}, {"ok": False, "mensaje": "Action Network vacío"}

    out, _meta = aplicar_cadena_momios(
        juegos,
        {"lineas": {"bookmakers": "pinnacle,draftkings"}},
        fetch_espn=fetch_espn,
        fetch_header=fetch_header,
        fetch_action=fetch_action,
    )
    juego = out[0]
    assert juego.get("fuente_momio") != "estimado"
    motivos = " ".join(i["motivo"] for i in juego["momio_intentos"])
    assert "ODDS_API_KEY" not in motivos
    assert "scoreboard" in motivos.lower() or "ESPN" in motivos
    assert "action network" in motivos.lower()

    aplicar_momio_estimado(juego, 60, 40, intentos=juego["momio_intentos"])
    assert juego["fuente_momio"] == "sin_momio_real"
    assert juego["paso_momio"] == "sin_casa"
    assert juego["origen_momio"] == "sin_momio_real"
    assert juego["lineas_fuente"] == "sin_momio_real"
    assert juego["estado_registro"] == "registrado sin apuesta"
    assert juego["odds_away_american"] == american_con_vig(60)
    assert es_momio_estimado(juego) is False
    juego["pick"] = "New York Yankees ML"
    juego["probPick"] = 60
    juego["apostable"] = True
    juego["odds_american"] = juego["odds_away_american"]
    juego["estado"] = "PROGRAMADO"
    assert apostable_para_dinero(juego) is False

    reales = {
        "id": "real",
        "estado": "PROGRAMADO",
        "apostable": True,
        "edge": 8,
        "fuente_momio": "draftkings",
        "lineas_fuente": "draftkings",
        "odds_american": -120,
        "odds_away_decimal": 1.83,
        "odds_home_decimal": 2.05,
    }
    juego["id"] = "est"
    juego["edge"] = 0
    cupo = seleccionar_favorables_del_dia(
        [juego, reales],
        {"estrategia": {"max_apuestas_dia": 1}},
    )
    por_id = {j["id"]: j["apostable"] for j in cupo}
    assert por_id["real"] is True
    assert por_id["est"] is False


def test_roi_de_cuota_real_no_mezcla_el_estimado():
    memoria = {
        "stake_por_juego": 3,
        "dias": [
            {
                "fecha": "2026-09-01",
                "predicciones": [],
                "apuestas": [
                    {
                        "estado": "ganada",
                        "profit": 2.0,
                        "stake": 3,
                        "fuente_momio": "draftkings",
                        "odds_american": -150,
                    },
                    {
                        "estado": "ganada",
                        "profit": 3.9,
                        "stake": 3,
                        "fuente_momio": "estimado",
                        "lineas_fuente": "estimado",
                        "odds_american": 130,
                    },
                ],
            }
        ],
    }
    split = resumen_predicciones_y_dinero(memoria)
    assert split["dinero"]["neto"] == pytest.approx(2.0)
    assert split["dinero"]["ganadas"] == 1
    assert split["dinero_estimado"]["neto"] == pytest.approx(3.9)
    assert split["dinero_estimado"]["roi_pct"] == pytest.approx(130.0)


def test_panel_muestra_stake_momio_y_estimado():
    html = Path("QuantumMLB.html").read_text(encoding="utf-8")
    assert "function textoStakeOdds" in html
    assert "function payoutSiGana" in html
    assert "Sin momio real, solo registrado" in html
    assert "Cuota real" in html
    assert "apuesta_fija" in html
    assert "si gana" in html
    assert "origen_momio" in html
    assert "no entra al ROI" in html
    assert "Estimada" in html
