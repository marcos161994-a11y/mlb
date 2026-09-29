from datetime import date

import pytest

import bullpen


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _schedule(*game_pks):
    return {
        "dates": [
            {
                "games": [
                    {"gamePk": pk, "status": {"codedGameState": "F"}} for pk in game_pks
                ]
            }
        ]
    }


def _boxscore(away_id, away_ip, home_id, home_ip):
    def lado(team_id, ips):
        pitchers = list(range(1, len(ips) + 1))
        players = {
            f"ID{pid}": {"stats": {"pitching": {"inningsPitched": ip}}}
            for pid, ip in zip(pitchers, ips)
        }
        return {"team": {"id": team_id}, "pitchers": pitchers, "players": players}

    return {"teams": {"away": lado(away_id, away_ip), "home": lado(home_id, home_ip)}}


@pytest.fixture(autouse=True)
def _limpio(tmp_path, monkeypatch):
    monkeypatch.setattr(bullpen, "CACHE_PATH", tmp_path / "bullpen_cache.json")
    bullpen._estado.update({"dia": "", "juegos": {}, "pendientes": None})
    bullpen._cache_leido = False
    yield


def _responder(monkeypatch, mapa_box, calendario, contador=None):
    def _get(url, params=None, timeout=None):
        if contador is not None:
            contador.append(url)
        if "/schedule" in url:
            return _Resp(calendario)
        pk = int(url.rstrip("/").split("/")[-2])
        return _Resp(mapa_box[pk])

    monkeypatch.setattr(bullpen._session, "get", _get)


def test_innings_a_float_lee_los_tercios():
    assert bullpen.innings_a_float("5.2") == pytest.approx(5 + 2 / 3)
    assert bullpen.innings_a_float("1.1") == pytest.approx(1 + 1 / 3)
    assert bullpen.innings_a_float("2.0") == 2.0
    assert bullpen.innings_a_float(None) == 0.0


def test_relevo_descuenta_al_abridor(monkeypatch):
    _responder(
        monkeypatch,
        {700: _boxscore(147, ["6.0", "1.0", "1.0"], 121, ["5.0", "2.0", "2.0"])},
        _schedule(700),
    )
    bullpen.refrescar(hasta=date.today())
    assert bullpen.innings_relevo(147) == 2.0
    assert bullpen.innings_relevo(121) == 4.0


def test_fatiga_va_de_descansado_a_exprimido(monkeypatch):
    _responder(
        monkeypatch,
        {
            700: _boxscore(147, ["8.0", "1.0"], 121, ["2.0", "7.0"]),
            701: _boxscore(147, ["8.0", "1.0"], 121, ["3.0", "6.0"]),
        },
        _schedule(700, 701),
    )
    bullpen.refrescar(hasta=date.today())
    descansado = bullpen.fatiga_bullpen(147)
    exprimido = bullpen.fatiga_bullpen(121)
    assert descansado == 0.0
    assert exprimido == 0.89
    assert exprimido >= 0.7 > descansado


def test_sin_datos_devuelve_none():
    assert bullpen.fatiga_bullpen(147) is None


def test_el_presupuesto_corta_y_el_resto_sigue_despues(monkeypatch):
    urls = []
    cajas = {pk: _boxscore(147, ["5.0", "2.0"], 121, ["5.0", "2.0"]) for pk in (700, 701, 702)}
    _responder(monkeypatch, cajas, _schedule(700, 701, 702), urls)
    monkeypatch.setattr(bullpen.time, "time", lambda: 10_000.0 + 100.0 * len(urls))

    primero = bullpen.refrescar(hasta=date.today(), presupuesto_seg=20)
    assert primero["leidos"] == 1
    assert primero["faltan"] == 2

    segundo = bullpen.refrescar(hasta=date.today(), presupuesto_seg=20)
    assert segundo["leidos"] == 1
    assert segundo["faltan"] == 1


def test_lo_leido_sobrevive_al_reinicio(monkeypatch):
    _responder(
        monkeypatch,
        {700: _boxscore(147, ["6.0", "3.0"], 121, ["6.0", "3.0"])},
        _schedule(700),
    )
    bullpen.refrescar(hasta=date.today())
    assert bullpen.innings_relevo(147) == 3.0

    bullpen._estado.update({"dia": "", "juegos": {}, "pendientes": None})
    bullpen._cache_leido = False
    monkeypatch.setattr(
        bullpen._session, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sin red"))
    )
    assert bullpen.innings_relevo(147) == 3.0
