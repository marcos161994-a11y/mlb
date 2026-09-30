"""Fecha del partido, rango, frescura, lado y liquidación del momio real."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import servidor_mlb as srv
from cadena_momios import (
    aplicar_cadena_momios,
    campos_precio_congelado,
    lado_del_pick,
    momio_del_pick,
    parsear_momio_americano,
    profit_moneyline_americano,
    reset_rechazos,
)
from lineas_betmgm import anexar_linea, buscar_lineas_partido
from lineas_espn import cruzar_momio_apuesta, parsear_eventos_espn, parsear_scoreboard_espn
from servidor_mlb import (
    _cruzar_momios_espn,
    estado_desde_status_mlb,
    liquidar_apuesta,
)


def _evento_scoreboard(eid, away, home, ml_away, ml_home, fecha, inicio):
    return {
        "id": eid,
        "date": inicio,
        "competitions": [
            {
                "date": inicio,
                "competitors": [
                    {"homeAway": "away", "team": {"displayName": away}},
                    {"homeAway": "home", "team": {"displayName": home}},
                ],
                "odds": [
                    {
                        "provider": {"name": "DraftKings"},
                        "moneyline": {
                            "away": {"close": {"odds": ml_away}},
                            "home": {"close": {"odds": ml_home}},
                        },
                    }
                ],
            }
        ],
    }


def _precio_vivo(**extra):
    ahora = datetime.now(timezone.utc)
    juego = {
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "pick_lado": "away",
        "fuente_momio": "draftkings",
        "lineas_fuente": "draftkings",
        "odds_away_american": 130,
        "odds_home_american": -150,
        "estado": "PROGRAMADO",
        "inicio_juego": (ahora + timedelta(hours=2)).isoformat(),
        "momio_fetched_at": ahora.isoformat(),
    }
    juego.update(extra)
    return juego


@pytest.mark.parametrize(
    ("raw", "esperado"),
    [
        (-100, -100),
        (100, 100),
        ("+100", 100),
        ("-100", -100),
        ("EVEN", 100),
        ("ev", 100),
        ("PK", 100),
        ("+130", 130),
        ("130.0", 130),
        (-110, -110),
        (-1000, -1000),
        (1000, 1000),
        (None, None),
        (0, None),
        (-1, None),
        (-5, None),
        (50, None),
        (-50, None),
        (5000, None),
        (-1500, None),
        ("", None),
    ],
)
def test_rango_del_momio_americano(raw, esperado):
    reset_rechazos()
    assert parsear_momio_americano(raw) == esperado


def test_serie_no_toma_el_momio_de_manana():
    hoy = parsear_scoreboard_espn(
        {"events": [_evento_scoreboard("hoy", "Chicago White Sox", "Houston Astros", "+102", "-123", "2026-09-30", "2026-09-30T23:10:00Z")]},
        fecha_slate="20260930",
    )
    manana = parsear_scoreboard_espn(
        {"events": [_evento_scoreboard("manana", "Chicago White Sox", "Houston Astros", "+124", "-148", "2026-10-01", "2026-10-01T23:10:00Z")]},
        fecha_slate="20261001",
    )
    mapa = {}
    for parcial in (hoy, manana):
        for clave, fila in parcial.items():
            anexar_linea(mapa, clave, fila)
    visita, local = "Chicago White Sox", "Houston Astros"
    assert buscar_lineas_partido(mapa, visita, local)["away"]["american"] == 102
    assert buscar_lineas_partido(mapa, visita, local, fecha="2026-09-30")["away"]["american"] == 102
    assert buscar_lineas_partido(mapa, visita, local, fecha="2026-10-01")["away"]["american"] == 124
    assert buscar_lineas_partido(mapa, visita, local, fecha="2026-10-02") is None
    assert buscar_lineas_partido(mapa, visita, local, fecha="2026-10-01", evento_id="hoy") is None


def test_doble_cartelera_elige_por_hora_y_no_adivina():
    mapa = {}
    for eid, inicio, ml in (
        ("g1", "2026-09-30T17:05:00Z", "+110"),
        ("g2", "2026-09-30T23:10:00Z", "-135"),
    ):
        parcial = parsear_scoreboard_espn(
            {"events": [_evento_scoreboard(eid, "New York Yankees", "Boston Red Sox", ml, "-120", "2026-09-30", inicio)]},
            fecha_slate="20260930",
        )
        for clave, fila in parcial.items():
            anexar_linea(mapa, clave, fila)
    visita, local = "New York Yankees", "Boston Red Sox"
    assert buscar_lineas_partido(mapa, visita, local, fecha="2026-09-30") is None
    primero = buscar_lineas_partido(mapa, visita, local, fecha="2026-09-30", inicio="2026-09-30T17:20:00Z")
    segundo = buscar_lineas_partido(mapa, visita, local, fecha="2026-09-30", inicio="2026-09-30T23:00:00Z")
    assert primero["away"]["american"] == 110
    assert segundo["away"]["american"] == -135
    assert buscar_lineas_partido(mapa, visita, local, fecha="2026-09-30", evento_id="g1")["espn_id"] == "g1"


def test_header_filtra_por_fecha():
    header = {
        "sports": [
            {
                "leagues": [
                    {
                        "events": [
                            {
                                "id": "h1",
                                "date": "2026-09-30T23:10:00Z",
                                "competitors": [
                                    {"homeAway": "away", "displayName": "Chicago White Sox"},
                                    {"homeAway": "home", "displayName": "Houston Astros"},
                                ],
                                "odds": {
                                    "provider": {"name": "DraftKings"},
                                    "away": {"moneyLine": 102},
                                    "home": {"moneyLine": -123},
                                },
                            },
                            {
                                "id": "h2",
                                "date": "2026-10-01T23:10:00Z",
                                "competitors": [
                                    {"homeAway": "away", "displayName": "Chicago White Sox"},
                                    {"homeAway": "home", "displayName": "Houston Astros"},
                                ],
                                "odds": {
                                    "provider": {"name": "DraftKings"},
                                    "away": {"moneyLine": 124},
                                    "home": {"moneyLine": -148},
                                },
                            },
                        ]
                    }
                ]
            }
        ]
    }
    mapa = parsear_eventos_espn(header)
    assert buscar_lineas_partido(mapa, "Chicago White Sox", "Houston Astros", fecha="2026-09-30")["away"]["american"] == 102
    assert buscar_lineas_partido(mapa, "Chicago White Sox", "Houston Astros", fecha="2026-10-01")["away"]["american"] == 124


def test_cadena_no_apuesta_el_precio_de_otro_dia():
    from lineas_betmgm import _match_key

    key = _match_key("Chicago White Sox", "Houston Astros")
    mapa = {}
    anexar_linea(
        mapa,
        key,
        {
            "espn_id": "hoy",
            "fecha": "2026-09-30",
            "inicio": "2026-09-30T23:10:00Z",
            "estado_cuota": "pre",
            "away": {"casa": "draftkings", "american": 102, "decimal": 2.02},
            "home": {"casa": "draftkings", "american": -123, "decimal": 1.81},
        },
    )
    anexar_linea(
        mapa,
        key,
        {
            "espn_id": "manana",
            "fecha": "2026-10-01",
            "inicio": "2026-10-01T23:10:00Z",
            "estado_cuota": "pre",
            "away": {"casa": "draftkings", "american": 124, "decimal": 2.24},
            "home": {"casa": "draftkings", "american": -148, "decimal": 1.68},
        },
    )
    juego = {
        "visitante": "Chicago White Sox",
        "home": "Houston Astros",
        "fecha": "2026-09-30",
        "inicio_juego": "2026-09-30T23:10:00Z",
        "id": "mlb-hoy",
    }
    out, _meta = aplicar_cadena_momios(
        [juego],
        {"lineas": {"bookmakers": "draftkings"}},
        fetch_espn=lambda js, _cfg: (js, {"ok": False, "mensaje": "scoreboard vacío"}),
        fetch_header=lambda: (mapa, {"ok": True, "mensaje": "header"}),
        fetch_action=lambda _cfg, _js: (_ for _ in ()).throw(AssertionError("action")),
    )
    assert out[0]["odds_away_american"] == 102
    assert out[0]["fuente_momio"] == "draftkings"


def test_precio_viejo_stale_o_en_vivo_no_es_apuesta():
    fresco = momio_del_pick(_precio_vivo(), "New York Yankees ML", 55)
    assert fresco["apto_para_apuesta"] is True
    assert fresco["odds_american"] == 130

    viejo = _precio_vivo(momio_fetched_at=(datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat())
    assert momio_del_pick(viejo, "New York Yankees ML", 55)["apto_para_apuesta"] is False

    stale = _precio_vivo(momio_stale=True)
    precio_stale = momio_del_pick(stale, "New York Yankees ML", 55)
    assert precio_stale["apto_para_apuesta"] is False
    assert precio_stale["fuente_momio"] == "sin_momio_real"

    vivo = _precio_vivo(estado="EN VIVO", momio_en_vivo=True, estado_cuota="in")
    assert momio_del_pick(vivo, "New York Yankees ML", 55)["apto_para_apuesta"] is False


def test_lado_por_id_no_por_sox_y_away_none_no_revienta():
    juego = _precio_vivo(
        visitante="Boston Red Sox",
        home="Chicago White Sox",
        away_id=111,
        home_id=145,
        away_abbr="BOS",
        home_abbr="CWS",
        pick_team_id=145,
        pick_abbr="CWS",
        pick_lado="home",
        odds_away_american=130,
        odds_home_american=-150,
    )
    assert lado_del_pick(juego, "White Sox ML") == "home"
    precio = momio_del_pick(dict(juego), "White Sox ML", 60)
    assert precio["odds_american"] == -150
    assert precio["apto_para_apuesta"] is True

    ambiguo = _precio_vivo(
        visitante="Boston Red Sox",
        home="Chicago White Sox",
        away_abbr="BOS",
        home_abbr="CWS",
        odds_away_american=130,
        odds_home_american=-150,
        pick_lado="",
        pick_team_id="",
        pick_abbr="",
    )
    assert lado_del_pick(ambiguo, "Sox ML") == ""
    assert momio_del_pick(dict(ambiguo), "Sox ML", 60)["apto_para_apuesta"] is False

    roto = _precio_vivo(pick_lado="away", odds_away_american=None, odds_home_american=-120)
    precio_roto = momio_del_pick(roto, "New York Yankees ML", 55)
    assert precio_roto["apto_para_apuesta"] is False
    assert precio_roto["fuente_momio"] == "sin_momio_real"


def test_liquidar_usa_el_payout_congelado_y_los_cierres():
    ahora = datetime.now(timezone.utc)
    juego = _precio_vivo(
        odds_away_american=-150,
        odds_home_american=130,
        momio_fetched_at=ahora.isoformat(),
    )
    precio = momio_del_pick(juego, "New York Yankees ML", 60)
    campos = campos_precio_congelado(precio, 3)
    apuesta = {"pick": "New York Yankees ML", "stake": 3, "estado": "pendiente", **campos}
    final = {
        "id": "1",
        "estado": "FINALIZADO",
        "ganador": "New York Yankees",
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
        "scoreAway": 4,
        "scoreHome": 1,
    }
    assert liquidar_apuesta(apuesta, final, 3) is True
    assert apuesta["profit"] == apuesta["payout_si_gana"]
    assert apuesta["profit"] == profit_moneyline_americano(3, -150, "ganada")

    perdida = {"pick": "New York Yankees ML", "stake": 3, "estado": "pendiente", **campos}
    final_loss = {**final, "ganador": "Boston Red Sox", "scoreAway": 1, "scoreHome": 6}
    assert liquidar_apuesta(perdida, final_loss, 3) is True
    assert perdida["profit"] == -3

    empate = {"pick": "New York Yankees ML", "stake": 3, "estado": "pendiente", **campos, "fuente_momio": "draftkings"}
    assert liquidar_apuesta(empate, {**final, "estado": "POSPUESTO"}, 3) is True
    assert empate["estado"] == "push"
    assert empate["profit"] == 0
    cancelada = {"pick": "New York Yankees ML", "stake": 3, "estado": "pendiente", **campos}
    assert liquidar_apuesta(cancelada, {**final, "estado": "CANCELADO"}, 3) is True
    assert cancelada["estado"] == "push"
    oficial = {"pick": "New York Yankees ML", "stake": 3, "estado": "pendiente", **campos}
    assert liquidar_apuesta(oficial, {**final, "estado": "SUSPENDIDO_OFICIAL"}, 3) is True
    assert oficial["profit"] == 0

    colgada = {"pick": "New York Yankees ML", "stake": 3, "estado": "pendiente", **campos}
    assert liquidar_apuesta(colgada, {**final, "estado": "SUSPENDIDO"}, 3) is False
    assert colgada["estado"] == "pendiente"
    assert colgada.get("profit") is None

    vieja = {"pick": "New York Yankees ML", "stake": 5, "estado": "pendiente", "odds": 1.8}
    assert liquidar_apuesta(vieja, {**final, "estado": "POSPUESTO"}, 5) is False
    assert vieja["estado"] == "pendiente"
    assert liquidar_apuesta(vieja, final, 5) is True
    assert vieja["profit"] == pytest.approx(4.0)

    assert estado_desde_status_mlb({"detailedState": "Postponed"}) == "POSPUESTO"
    assert estado_desde_status_mlb({"codedGameState": "C", "detailedState": "Canceled"}) == "CANCELADO"
    assert estado_desde_status_mlb({"codedGameState": "T", "detailedState": "Suspended"}) == "SUSPENDIDO"
    assert estado_desde_status_mlb({"abstractGameState": "Final", "detailedState": "Suspended: Final"}) == "SUSPENDIDO_OFICIAL"


def test_cruce_espn_marca_mas_de_20_y_no_bloquea():
    apuesta = {
        "pick": "New York Yankees ML",
        "pick_lado": "away",
        "game_id": "401",
        "espn_id": "401",
        "odds_american": 102,
        "visitante": "New York Yankees",
        "home": "Boston Red Sox",
    }

    def cierre(gid):
        assert gid == "401"
        return {"pickcenter": [{"moneyline": {"away": {"close": {"odds": "+130"}}}}]}

    lejos = cruzar_momio_apuesta(apuesta, fetch=cierre)
    assert lejos["ok"] is True
    assert lejos["alerta"] is True
    assert lejos["diff"] == 28

    def cerca(_gid):
        return {"pickcenter": [{"moneyline": {"away": {"close": {"odds": "+112"}}}}]}

    assert cruzar_momio_apuesta(apuesta, fetch=cerca)["alerta"] is False

    def rompe(_gid):
        raise TimeoutError("espn")

    fallo = cruzar_momio_apuesta(apuesta, fetch=rompe)
    assert fallo["ok"] is False

    dia = {"apuestas": [dict(apuesta)]}

    def estalla(*_a, **_k):
        raise RuntimeError("espn caído")

    import lineas_espn

    original = lineas_espn.cruzar_momio_apuesta
    lineas_espn.cruzar_momio_apuesta = estalla
    try:
        assert _cruzar_momios_espn(dia) is True
    finally:
        lineas_espn.cruzar_momio_apuesta = original
    assert dia["apuestas"][0]["cruce_espn"]["ok"] is False


@asynccontextmanager
async def _sin_motor(_app):
    yield


def test_health_cuenta_sin_momio_real_y_conserva_congelacion(monkeypatch):
    reset_rechazos()
    parsear_momio_americano(5000)
    memoria = {
        "capital_inicial": 100,
        "capital": 100,
        "dias": [
            {
                "fecha": srv.fecha_str(),
                "predicciones": [
                    {"fuente_momio": "draftkings", "casa_momio": "draftkings"},
                    {"fuente_momio": "sin_momio_real", "estado_registro": "registrado sin apuesta"},
                    {"fuente_momio": "pinnacle", "casa_momio": "pinnacle"},
                ],
                "apuestas": [
                    {
                        "estado": "pendiente",
                        "pick": "Boston Red Sox ML",
                        "fuente_momio": "draftkings",
                        "odds_american": 0,
                        "bloqueado_en": (datetime.now(srv.tz_experimento()) - timedelta(hours=30)).isoformat(),
                        "cruce_momio_alerta": True,
                    }
                ],
            }
        ],
    }
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.setattr(srv.app.router, "lifespan_context", _sin_motor)
    srv.MEMORIA_PATH.write_text(json.dumps(memoria), encoding="utf-8")
    srv._invalidar_cache_memoria()
    with TestClient(srv.app) as client:
        body = client.get("/api/health").json()
    odds = body["odds"]
    assert odds["reales"] == 2
    assert odds["por_casa"]["draftkings"] == 1
    assert odds["sin_momio_real"] == 1
    assert odds["alerta_sin_momio_real"] is True
    assert odds["sin_momio_real_pct"] == pytest.approx(33.3)
    assert odds["rechazados"] >= 1
    assert odds["cruce_espn_alertas"] == 1
    assert odds["pendientes_24h"] == 1
    assert odds["sin_precio_congelado"] == 1
    assert body["congelacion"]["ventanas_min"]
    texto = json.dumps(body, ensure_ascii=False)
    assert "odds_api" not in odds
    assert "key_presente" not in odds
    assert "key_preview" not in odds
    assert "ODDS_API_KEY" not in texto
    assert "the-odds-api" not in texto.lower()


def test_proveedor_betmgm_cae_en_espn_sin_clave(monkeypatch):
    import copy

    monkeypatch.setattr(srv.app.router, "lifespan_context", _sin_motor)
    real = srv.cargar_config()

    def espn():
        return {}, {"ok": True, "partidos": 4, "mensaje": "4 partidos ESPN"}

    monkeypatch.setattr("lineas_espn.obtener_lineas_espn", espn)

    for legado in ("betmgm", "the-odds-api"):
        def cfg(nombre=legado):
            out = copy.deepcopy(real)
            out.setdefault("lineas", {})["proveedor"] = nombre
            return out

        monkeypatch.setattr(srv, "cargar_config", cfg)
        body = srv.api_odds_status()
        assert body["proveedor"] == "espn"
        assert body["ok"] is True
        assert body["fallback_espn"] is True
        assert body["partidos"] == 4
        assert "key_presente" not in body
        assert "key_preview" not in body
        assert "fuente" not in body
        assert "ayuda" not in body
        texto = json.dumps(body, ensure_ascii=False)
        assert "ODDS_API_KEY" not in texto
        assert "the-odds-api.com" not in texto
        health = srv.api_health()["odds"]
        assert health["proveedor"] == "espn"
        assert "odds_api" not in health
        texto_health = json.dumps(health, ensure_ascii=False)
        assert "ODDS_API_KEY" not in texto_health
        assert "the-odds-api" not in texto_health.lower()
        assert "clave" not in texto_health.lower()


def test_sin_momio_y_auditoria_persisten_en_la_base(monkeypatch):
    from cadena_momios import (
        anotar_fetch_ok,
        edad_ultimo_fetch_seg,
        registrar_rechazo,
        reset_auditoria_momios,
        resumen_rechazos,
    )

    reset_auditoria_momios()
    anotar_fetch_ok()
    registrar_rechazo("5000", "fuera de -1000..+1000")
    monkeypatch.setattr(srv, "generar_briefing_juego", lambda *_a, **_k: {"ok": True})

    memoria = srv.cargar_memoria()
    dia = srv.asegurar_dia_operativo(memoria, "2026-09-30")
    inicio = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    juego = {
        "id": "g-sin",
        "visitante": "Chicago White Sox",
        "home": "Houston Astros",
        "pick": "Houston Astros ML",
        "estado": "PROGRAMADO",
        "inicio_juego": inicio,
        "probPick": 61,
        "odds": 1.91,
        "odds_american": -110,
        "fuente_momio": "sin_momio_real",
        "lineas_fuente": "sin_momio_real",
        "estado_registro": "registrado sin apuesta",
        "sin_momio_real": True,
        "momio_fallos": "espn: sin momio de esta fecha",
        "apostable": False,
    }
    assert srv.guardar_prediccion(dia, juego, stake_virtual=3) is True
    dia["apuestas"].append(
        {
            "game_id": "g-real",
            "pick": "Houston Astros ML",
            "estado": "ganada",
            "stake": 3,
            "profit": 2.0,
            "odds_american": -150,
            "fuente_momio": "draftkings",
            "payout_si_gana": 2.0,
            "cruce_espn": {"ok": True, "diff": 28, "alerta": True, "espn_odds": -122},
            "cruce_momio_alerta": True,
        }
    )
    srv.guardar_memoria(memoria)

    reset_auditoria_momios()
    srv._invalidar_cache_memoria()
    cargado = srv.cargar_memoria(force=True)
    preds = [p for d in cargado["dias"] for p in d.get("predicciones") or [] if p.get("game_id") == "g-sin"]
    apuestas = [a for d in cargado["dias"] for a in d.get("apuestas") or [] if a.get("game_id") == "g-real"]
    pred = preds[0]
    assert pred["sin_momio_real"] is True
    assert pred["estado_registro"] == "registrado sin apuesta"
    assert pred["fuente_momio"] == "sin_momio_real"
    assert "espn" in pred["momio_fallos"]
    apuesta = apuestas[0]
    assert apuesta["cruce_momio_alerta"] is True
    assert apuesta["cruce_espn"]["diff"] == 28
    assert apuesta["payout_si_gana"] == 2.0
    aud = cargado["auditoria_momios"]
    assert aud["ultimo_fetch_ok"]
    assert any(item["raw"] == "5000" for item in aud["rechazos"])
    assert resumen_rechazos()["razones"]["fuera de -1000..+1000"] >= 1
    assert edad_ultimo_fetch_seg() is not None
    bruto = srv._store().cargar()
    assert bruto["auditoria_momios"]["ultimo_fetch_ok"] == aud["ultimo_fetch_ok"]
    guardada = next(
        p
        for d in bruto["dias"]
        for p in d.get("predicciones") or []
        if p.get("game_id") == "g-sin"
    )
    assert guardada["sin_momio_real"] is True
