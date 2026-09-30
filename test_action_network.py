"""Action Network como cuarto eslabón. HTTP mockeado; el fixture es una respuesta real."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cadena_momios import (
    _CADENA_DEFAULT,
    action_network_activo,
    aplicar_cadena_momios,
    importar_auditoria_momios,
    momio_del_pick,
    reset_auditoria_momios,
    resumen_fuentes,
    volcar_auditoria_en,
)
from lineas_action_network import (
    TIMEOUT_SEG,
    parsear_scoreboard_action,
    reset_cache_action_network,
)
from lineas_betmgm import buscar_lineas_partido, fecha_slate_desde_instante
from servidor_mlb import _salud_momios, fecha_str

_FIXTURE = Path("fixtures/action_network_mlb_scoreboard.json")
_CFG = {"lineas": {"bookmakers": "pinnacle,draftkings,fanduel,betmgm"}}


@pytest.fixture(autouse=True)
def _aislar_cache():
    reset_cache_action_network()
    reset_auditoria_momios()
    yield
    reset_cache_action_network()
    reset_auditoria_momios()


def _payload_real() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _partido_real(game_id: int = 302581) -> dict:
    game = next(g for g in _payload_real()["games"] if g["id"] == game_id)
    return deepcopy(game)


def _marcar(game: dict, inicio: datetime, *, minutos: float) -> dict:
    game["status"] = "scheduled"
    game["real_status"] = "scheduled"
    game["start_time"] = inicio.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    marca = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
    for odd in game.get("odds") or []:
        odd["inserted"] = marca
    return game


def _tablero(*games: dict) -> dict:
    return {"league": {"id": 8, "sport": "baseball", "name": "mlb"}, "games": list(games)}


def _juego(game: dict, inicio: datetime, *, fecha: str | None = None) -> dict:
    equipos = {str(t["id"]): t["full_name"] for t in game["teams"]}
    return {
        "id": "mlb-test",
        "visitante": equipos[str(game["away_team_id"])],
        "home": equipos[str(game["home_team_id"])],
        "fecha": fecha if fecha is not None else fecha_slate_desde_instante(inicio),
        "inicio_juego": inicio.astimezone(timezone.utc).isoformat(),
        "estado": "PROGRAMADO",
    }


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None, *, bloqueado: bool = False):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self._bloqueado = bloqueado

    def json(self):
        if self._bloqueado:
            raise ValueError("html")
        return self._payload


def _mock(monkeypatch, payload: dict | None = None, *, status: int = 200, bloqueado: bool = False):
    llamadas: list[dict] = []

    def get(url, params=None, headers=None, timeout=None, **_kwargs):
        llamadas.append({"url": url, "params": params or {}, "headers": headers or {}, "timeout": timeout})
        return _Resp(status, payload, bloqueado=bloqueado)

    monkeypatch.setattr("lineas_action_network.requests.get", get)
    return llamadas


def _sin_previos():
    return dict(
        fetch_espn=lambda js, _cfg: (js, {"ok": False, "mensaje": "ESPN scoreboard caído"}),
        fetch_header=lambda: ({}, {"ok": False, "mensaje": "ESPN header vacío"}),
    )


def _motivos(juego: dict) -> str:
    return " ".join(
        f"{item.get('fuente')} {item.get('motivo')}" for item in juego.get("momio_intentos") or []
    )


def test_la_cadena_default_termina_en_action_network():
    assert _CADENA_DEFAULT == ("espn_scoreboard", "espn_header", "action_network")
    assert action_network_activo({}) is True
    assert action_network_activo({"lineas": {}}) is True
    assert action_network_activo({"lineas": {"action_network": False}}) is False


def test_fixture_real_separa_casas_y_no_mezcla_el_dia_utc():
    mapa = parsear_scoreboard_action(_payload_real())
    fila = buscar_lineas_partido(
        mapa,
        "Chicago White Sox",
        "Houston Astros",
        fecha="2026-09-30",
        inicio="2026-09-30T21:00:00.000Z",
    )
    assert fila is not None
    por_casa = {libro["casa"]: libro for libro in fila["libros"]}
    assert por_casa["draftkings"]["ml_away"] == 128
    assert por_casa["draftkings"]["ml_home"] == -155
    assert por_casa["draftkings"]["provider"] == "DraftKings"
    assert por_casa["fanduel"]["ml_away"] == 122
    assert "consensus" not in por_casa
    assert "open" not in por_casa
    assert buscar_lineas_partido(
        mapa,
        "Chicago White Sox",
        "Houston Astros",
        fecha="2026-10-01",
        inicio="2026-09-30T21:00:00.000Z",
    ) is None
    # 00:00 UTC del día siguiente sigue siendo el slate de Nueva York del 30.
    yankees = buscar_lineas_partido(
        mapa,
        "Boston Red Sox",
        "New York Yankees",
        fecha="2026-09-30",
        inicio="2026-10-01T00:00:00.000Z",
    )
    assert yankees is not None
    assert yankees["fecha"] == "2026-09-30"
    assert buscar_lineas_partido(
        mapa,
        "Boston Red Sox",
        "New York Yankees",
        fecha="2026-10-01",
        inicio="2026-10-01T00:00:00.000Z",
    ) is None


def test_exito_usa_la_casa_en_orden_y_cuenta_en_salud(monkeypatch):
    game = _partido_real()
    for odd in game["odds"]:
        if odd["book_id"] == 68:
            odd["ml_home"] = "EVEN"
    inicio = datetime.now(timezone.utc) + timedelta(hours=3)
    _marcar(game, inicio, minutos=1)
    llamadas = _mock(monkeypatch, _tablero(game))
    out, meta = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    juego = out[0]
    assert juego["fuente_momio"] == "action_network"
    assert juego["paso_momio"] == "action_network"
    assert juego["lineas_fuente"] == "action_network"
    assert juego["casa_momio"] == "DraftKings"
    assert juego["origen_momio"] == "action_network:draftkings"
    assert juego["odds_away_american"] == 128
    assert juego["odds_home_american"] == 100
    assert meta["reales"] == 1
    precio = momio_del_pick(juego, "Chicago White Sox ML", 55)
    assert precio["apto_para_apuesta"] is True
    assert precio["fuente_momio"] == "action_network"
    assert precio["casa"] == "DraftKings"
    assert precio["odds_american"] == 128
    motivos = _motivos(juego)
    assert "pinnacle" in motivos
    assert "fanduel" not in motivos.split("action_network:draftkings")[-1]
    assert llamadas and "api.actionnetwork.com/web/v1/scoreboard/mlb" in llamadas[0]["url"]
    assert llamadas[0]["params"]["period"] == "game"
    assert "bookIds" in llamadas[0]["params"]
    assert llamadas[0]["timeout"] <= TIMEOUT_SEG
    assert "Mozilla" in llamadas[0]["headers"]["User-Agent"]
    fuentes = resumen_fuentes()
    assert fuentes["action_network"] == {"ok": 1, "fallo": 0}
    memoria = {
        "dias": [
            {
                "fecha": fecha_str(),
                "predicciones": [
                    {
                        "fuente_momio": juego["fuente_momio"],
                        "lineas_fuente": juego["lineas_fuente"],
                        "casa_momio": juego["casa_momio"],
                    }
                ],
            }
        ]
    }
    salud = _salud_momios(memoria)
    assert salud["reales"] == 1
    assert salud["por_casa"]["DraftKings"] == 1
    assert salud["fuentes"]["action_network"]["ok"] == 1
    volcar_auditoria_en(memoria)
    assert memoria["auditoria_momios"]["fuentes"]["action_network"]["ok"] == 1
    reset_auditoria_momios()
    importar_auditoria_momios(memoria["auditoria_momios"])
    assert resumen_fuentes()["action_network"]["ok"] == 1


def test_fecha_equivocada_no_toma_la_linea_de_manana(monkeypatch):
    hoy = _partido_real()
    manana = _partido_real()
    manana["id"] = 302582
    inicio_hoy = datetime.now(timezone.utc) + timedelta(hours=3)
    inicio_manana = datetime.now(timezone.utc) + timedelta(hours=27)
    _marcar(hoy, inicio_hoy, minutos=1)
    _marcar(manana, inicio_manana, minutos=1)
    for odd in manana["odds"]:
        if odd["book_id"] == 68:
            odd["ml_away"] = 200
            odd["ml_home"] = -240
    _mock(monkeypatch, _tablero(manana, hoy))
    fecha_hoy = fecha_slate_desde_instante(inicio_hoy)
    out, _meta = aplicar_cadena_momios([_juego(hoy, inicio_hoy)], _CFG, **_sin_previos())
    assert out[0]["odds_away_american"] == 128
    assert out[0]["fuente_momio"] == "action_network"
    fecha_manana = fecha_slate_desde_instante(inicio_manana)
    out_m, _meta_m = aplicar_cadena_momios(
        [_juego(manana, inicio_manana, fecha=fecha_manana)],
        _CFG,
        **_sin_previos(),
    )
    assert out_m[0]["odds_away_american"] == 200
    juego = _juego(hoy, inicio_hoy, fecha="2026-01-01")
    out_no, _meta_no = aplicar_cadena_momios([juego], _CFG, **_sin_previos())
    assert out_no[0].get("fuente_momio") != "action_network"
    assert out_no[0].get("odds_away_american") != 128
    assert out_no[0].get("odds_away_american") != 200
    assert "sin momio de esta fecha" in _motivos(out_no[0])
    assert fecha_hoy != fecha_manana


def test_precio_viejo_se_rechaza(monkeypatch):
    game = _partido_real()
    inicio = datetime.now(timezone.utc) + timedelta(hours=3)
    _marcar(game, inicio, minutos=20)
    _mock(monkeypatch, _tablero(game))
    out, _meta = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    juego = out[0]
    assert juego.get("fuente_momio") != "action_network"
    assert juego.get("odds_away_american") != 128
    assert "precio con" in _motivos(juego)
    assert "15" in _motivos(juego)
    precio = momio_del_pick(juego, "Chicago White Sox ML", 55)
    assert precio["apto_para_apuesta"] is False
    assert precio["fuente_momio"] == "sin_momio_real"
    assert precio["estado_registro"] == "registrado sin apuesta"
    assert resumen_fuentes()["action_network"]["fallo"] == 1
    assert resumen_fuentes()["action_network"]["ok"] == 0


def test_momio_entre_menos_100_y_mas_100_no_se_usa(monkeypatch):
    game = _partido_real()
    inicio = datetime.now(timezone.utc) + timedelta(hours=3)
    _marcar(game, inicio, minutos=1)
    for odd in game["odds"]:
        if odd["book_id"] in (68, 69):
            odd["ml_away"] = 50
            odd["ml_home"] = -110
    _mock(monkeypatch, _tablero(game))
    out, _meta = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    assert out[0].get("fuente_momio") != "action_network"
    assert out[0].get("odds_away_american") != 50
    assert "fuera de rango" in _motivos(out[0])


@pytest.mark.parametrize(
    ("status", "bloqueado", "texto"),
    [
        (403, False, "403"),
        (429, False, "429"),
        (200, True, "bloque"),
    ],
)
def test_bloqueo_cae_a_sin_momio_real(monkeypatch, status, bloqueado, texto):
    game = _partido_real()
    inicio = datetime.now(timezone.utc) + timedelta(hours=3)
    _marcar(game, inicio, minutos=1)
    _mock(monkeypatch, _tablero(game), status=status, bloqueado=bloqueado)
    out, _meta = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    juego = out[0]
    assert texto in _motivos(juego)
    assert juego.get("odds_away_american") != 128
    precio = momio_del_pick(juego, "Chicago White Sox ML", 55)
    assert precio["fuente_momio"] == "sin_momio_real"
    assert precio["apto_para_apuesta"] is False
    assert precio["estado_registro"] == "registrado sin apuesta"
    assert resumen_fuentes()["action_network"]["ok"] == 0
    assert resumen_fuentes()["action_network"]["fallo"] == 1
    memoria = {
        "dias": [
            {
                "fecha": fecha_str(),
                "predicciones": [
                    {
                        "fuente_momio": "sin_momio_real",
                        "estado_registro": "registrado sin apuesta",
                        "sin_momio_real": True,
                    }
                ],
            }
        ]
    }
    salud = _salud_momios(memoria)
    assert salud["sin_momio_real"] == 1
    assert salud["por_casa"] == {}
    assert salud["fuentes"]["action_network"]["fallo"] == 1


def test_no_llama_action_network_si_una_casa_anterior_ya_cotiza(monkeypatch):
    def prohibido(*_a, **_k):
        raise AssertionError("Action Network no debía consultarse")

    monkeypatch.setattr("lineas_action_network.requests.get", prohibido)
    juegos = [
        {
            "visitante": "Chicago White Sox",
            "home": "Houston Astros",
            "lineas_libros": [
                {"casa": "draftkings", "provider": "DraftKings", "ml_away": -110, "ml_home": -110}
            ],
        }
    ]

    def fetch_espn(js, _cfg):
        return js, {"ok": True, "mensaje": "stub"}

    out, _meta = aplicar_cadena_momios(
        juegos,
        _CFG,
        fetch_espn=fetch_espn,
        fetch_header=lambda: (_ for _ in ()).throw(AssertionError("header")),
    )
    assert out[0]["paso_momio"] == "espn_scoreboard"
    assert out[0]["fuente_momio"] == "draftkings"
    assert "action_network" not in resumen_fuentes()


def test_el_orden_de_la_cadena_llega_a_action_network_al_final(monkeypatch):
    game = _partido_real()
    inicio = datetime.now(timezone.utc) + timedelta(hours=3)
    _marcar(game, inicio, minutos=1)
    # FanDuel va primero en el JSON. La cadena igual prueba DraftKings antes.
    game["odds"] = sorted(game["odds"], key=lambda odd: 0 if odd["book_id"] == 69 else 1)
    llamadas = _mock(monkeypatch, _tablero(game))
    out, _meta = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    juego = out[0]
    assert juego["origen_momio"] == "action_network:draftkings"
    fuentes = [item["fuente"] for item in juego["momio_intentos"]]
    assert fuentes.index("espn_scoreboard") < fuentes.index("espn_header")
    assert any(nombre.startswith("action_network") for nombre in fuentes)
    assert max(i for i, nombre in enumerate(fuentes) if nombre.startswith("espn_header")) < min(
        i for i, nombre in enumerate(fuentes) if nombre.startswith("action_network")
    )
    assert "odds_api" not in fuentes
    assert len(llamadas) == 1
    out2, _meta2 = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    assert out2[0]["casa_momio"] == "DraftKings"
    assert len(llamadas) == 1


def test_la_bandera_apaga_el_paso(monkeypatch):
    def prohibido(*_a, **_k):
        raise AssertionError("Action Network está apagado")

    monkeypatch.setattr("lineas_action_network.requests.get", prohibido)
    game = _partido_real()
    inicio = datetime.now(timezone.utc) + timedelta(hours=3)
    cfg = {"lineas": {"bookmakers": "draftkings", "action_network": False}}
    out, meta = aplicar_cadena_momios([_juego(game, inicio)], cfg, **_sin_previos())
    assert meta["action_network"] == "Action Network desactivado"
    assert out[0].get("fuente_momio") != "action_network"
    assert "action_network" not in _motivos(out[0])
    precio = momio_del_pick(out[0], "Chicago White Sox ML", 55)
    assert precio["fuente_momio"] == "sin_momio_real"
    assert "action_network" not in resumen_fuentes()


def test_sin_hora_de_linea_no_inventa_frescura(monkeypatch):
    game = _partido_real()
    inicio = datetime.now(timezone.utc) + timedelta(hours=3)
    _marcar(game, inicio, minutos=1)
    for odd in game["odds"]:
        if odd["book_id"] == 68:
            odd["inserted"] = None
    _mock(monkeypatch, _tablero(game))
    out, _meta = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    juego = out[0]
    assert juego["fuente_momio"] == "action_network"
    assert juego["casa_momio"] == "FanDuel"
    assert juego["odds_away_american"] == 122
    assert juego["origen_momio"] == "action_network:fanduel"


def test_partido_ya_empezado_no_se_usa(monkeypatch):
    game = _partido_real()
    inicio = datetime.now(timezone.utc) - timedelta(hours=1)
    _marcar(game, inicio, minutos=1)
    game["status"] = "inprogress"
    game["real_status"] = "inprogress"
    _mock(monkeypatch, _tablero(game))
    out, _meta = aplicar_cadena_momios([_juego(game, inicio)], _CFG, **_sin_previos())
    assert out[0].get("fuente_momio") != "action_network"
    assert "vivo" in _motivos(out[0])
