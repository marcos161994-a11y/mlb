"""Tests de cuotas públicas ESPN (parseo sin red + smoke opcional)."""

from lineas_betmgm import american_a_decimal, buscar_lineas_partido
from lineas_espn import parsear_eventos_espn
from modelo_mlb import tiene_cuota_mercado, marcar_estudio_sin_mercado, apostable_con_mercado


FIXTURE = {
    "sports": [
        {
            "leagues": [
                {
                    "events": [
                        {
                            "id": "1",
                            "competitors": [
                                {"homeAway": "away", "displayName": "Seattle Mariners"},
                                {"homeAway": "home", "displayName": "New York Yankees"},
                            ],
                            "odds": {
                                "provider": {"name": "DraftKings"},
                                "away": {"moneyLine": 152},
                                "home": {"moneyLine": -163},
                                "overUnder": 8.5,
                                "overOdds": -110,
                                "underOdds": -110,
                                "awayTeamOdds": {
                                    "moneyLine": 152,
                                    "team": {"displayName": "Seattle Mariners"},
                                },
                                "homeTeamOdds": {
                                    "moneyLine": -163,
                                    "team": {"displayName": "New York Yankees"},
                                },
                            },
                        },
                        {
                            "id": "2",
                            "competitors": [
                                {"homeAway": "away", "displayName": "Boston Red Sox"},
                                {"homeAway": "home", "displayName": "Toronto Blue Jays"},
                            ],
                            "odds": {"details": "sin ML"},
                        },
                    ]
                }
            ]
        }
    ]
}


def test_parsear_scoreboard_espn_moneyline_close():
    from lineas_espn import parsear_scoreboard_espn

    payload = {
        "events": [
            {
                "id": "401878657",
                "competitions": [
                    {
                        "competitors": [
                            {
                                "homeAway": "away",
                                "team": {"displayName": "San Francisco Giants"},
                            },
                            {
                                "homeAway": "home",
                                "team": {"displayName": "Atlanta Braves"},
                            },
                        ],
                        "odds": [
                            {
                                "provider": {"name": "DraftKings"},
                                "moneyline": {
                                    "away": {"close": {"odds": "+174"}},
                                    "home": {"close": {"odds": "-187"}},
                                },
                                "overUnder": 9.0,
                                "overOdds": -108,
                                "underOdds": -111,
                            }
                        ],
                    }
                ],
            }
        ]
    }
    mapa = parsear_scoreboard_espn(payload)
    fila = buscar_lineas_partido(mapa, "San Francisco Giants", "Atlanta Braves")
    assert fila is not None
    assert fila["away"]["american"] == 174
    assert fila["home"]["american"] == -187
    assert fila["total"]["linea"] == 9.0


def test_parsear_moneyline_espn():
    mapa = parsear_eventos_espn(FIXTURE)
    assert len(mapa) == 1
    fila = buscar_lineas_partido(mapa, "Seattle Mariners", "New York Yankees")
    assert fila is not None
    assert fila["home"]["american"] == -163
    assert fila["away"]["american"] == 152
    assert abs(fila["home"]["decimal"] - american_a_decimal(-163)) < 0.001
    assert fila["home"]["casa"] == "draftkings"
    assert fila["total"]["linea"] == 8.5
    assert fila["total"]["over_american"] == -110
    assert fila["total"]["under_american"] == -110


def test_aplicar_total_aunque_ml_ya_exista(monkeypatch):
    from lineas_espn import aplicar_lineas_espn, parsear_eventos_espn

    mapa = parsear_eventos_espn(FIXTURE)
    monkeypatch.setattr(
        "lineas_espn.obtener_lineas_espn",
        lambda timeout=12.0: (mapa, {"ok": True, "partidos": 1, "mensaje": "test"}),
    )
    juegos = [
        {
            "visitante": "Seattle Mariners",
            "home": "New York Yankees",
            "odds_away_decimal": 2.5,
            "odds_home_decimal": 1.6,
            "lineas_fuente": "draftkings",
        }
    ]
    juegos, meta = aplicar_lineas_espn(juegos, {}, solo_vacios=True)
    assert juegos[0]["total_linea"] == 8.5
    assert juegos[0]["lineas_total"]["linea"] == 8.5
    assert meta.get("totales_aplicados") == 1


def test_aplicar_espn_solo_vacios():
    juegos = [
        {
            "visitante": "Seattle Mariners",
            "home": "New York Yankees",
            "odds_away_decimal": None,
            "odds_home_decimal": None,
            "lineas_fuente": "modelo",
        }
    ]
    # Inyecta mapa parseado sin pegarle a la red
    from lineas_espn import parsear_eventos_espn as _p
    mapa = _p(FIXTURE)
    from lineas_betmgm import buscar_lineas_partido as buscar
    lineas = buscar(mapa, juegos[0]["visitante"], juegos[0]["home"])
    assert lineas
    juegos[0]["odds_away_american"] = lineas["away"]["american"]
    juegos[0]["odds_away_decimal"] = lineas["away"]["decimal"]
    juegos[0]["odds_home_american"] = lineas["home"]["american"]
    juegos[0]["odds_home_decimal"] = lineas["home"]["decimal"]
    juegos[0]["lineas_fuente"] = "draftkings"
    assert tiene_cuota_mercado(juegos[0]) is True


def test_sin_mercado_no_es_valor():
    j = {"visitante": "A", "home": "B"}
    marcar_estudio_sin_mercado(j, pick="B ML", prob=71.8, min_prob=58)
    assert j["apostable"] is False
    assert j["edge"] == 0
    assert j["lineas_fuente"] == "modelo"
    assert tiene_cuota_mercado(j) is False
    assert apostable_con_mercado(j) is False
    assert "no es valor" in (j["motivo_apuesta"] or "").lower() or "sin cuota" in (j["motivo_apuesta"] or "").lower()


def test_72_con_cuota_real_no_inventa_edge():
    """Con -163 de casa, el edge es modelo vs implícita, no 72-50."""
    from modelo_mlb import edge_pct, prob_implicita

    dec = american_a_decimal(-163)
    impl = prob_implicita(dec)
    e = edge_pct(71.8, dec)
    assert impl > 60  # favorito de mercado, no -254 inventado
    assert e < 15  # ya no es el falso +21.8
    assert abs(e - (71.8 - impl)) < 0.2


def _evento_scoreboard(away, home, ml_away, ml_home, eid):
    return {
        "id": eid,
        "competitions": [
            {
                "competitors": [
                    {"homeAway": "away", "team": {"displayName": away}},
                    {"homeAway": "home", "team": {"displayName": home}},
                ],
                "odds": [
                    {
                        "provider": {"name": "DraftKings"},
                        "awayTeamOdds": {"moneyLine": ml_away},
                        "homeTeamOdds": {"moneyLine": ml_home},
                    }
                ],
            }
        ],
    }


def test_scoreboard_no_cambia_de_dia_a_las_ocho_de_la_noche(monkeypatch):
    """A las 8:30pm en la isla ya es el día siguiente en UTC (Render)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from lineas_espn import _fetch_mapa_espn

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            fijo = datetime(2026, 9, 30, 0, 30, tzinfo=ZoneInfo("UTC"))
            if tz is None:
                return fijo.replace(tzinfo=None)
            return fijo.astimezone(tz)

    por_fecha = {
        "20260929": {"events": [_evento_scoreboard("New York Yankees", "Boston Red Sox", 150, -170, "hoy")]},
        "20260930": {
            "events": [
                _evento_scoreboard("New York Yankees", "Boston Red Sox", -120, 100, "manana"),
                _evento_scoreboard("New York Mets", "Philadelphia Phillies", -110, -110, "otro"),
            ]
        },
        "20260928": {
            "events": [_evento_scoreboard("Los Angeles Dodgers", "San Diego Padres", 130, -150, "ayer")]
        },
    }
    pedidas = []

    def _get(url, params=None, timeout=None):
        fecha = str((params or {}).get("dates"))
        pedidas.append(fecha)
        return _Resp(por_fecha.get(fecha, {"events": []}))

    monkeypatch.setattr("lineas_espn.datetime", _Reloj)
    monkeypatch.setattr("lineas_espn.requests.get", _get)
    mapa, _api, err = _fetch_mapa_espn(5)
    assert err is None
    assert pedidas[0] == "20260929"
    yankees = buscar_lineas_partido(mapa, "New York Yankees", "Boston Red Sox")
    mets = buscar_lineas_partido(mapa, "New York Mets", "Philadelphia Phillies")
    dodgers = buscar_lineas_partido(mapa, "Los Angeles Dodgers", "San Diego Padres")
    assert yankees["away"]["american"] == 150
    assert mets is not None
    assert dodgers["away"]["american"] == 130


def test_espn_en_vivo_si_hay_red():
    from lineas_espn import obtener_lineas_espn

    _mapa, meta = obtener_lineas_espn(timeout=12.0)
    if not meta.get("ok"):
        return
    assert int(meta.get("partidos") or 0) >= 1


def test_espn_usa_cache_disco_si_red_falla(monkeypatch, tmp_path):
    monkeypatch.setattr("lineas_espn._espn_disk_path", lambda: tmp_path / "espn.json")
    from lineas_espn import (
        _guardar_disco,
        invalidar_cache_espn,
        obtener_lineas_espn,
        parsear_eventos_espn,
    )

    mapa = parsear_eventos_espn(FIXTURE)
    _guardar_disco(mapa)
    invalidar_cache_espn()

    def boom(*_a, **_k):
        raise RuntimeError("red caída")

    monkeypatch.setattr("lineas_espn.requests.get", boom)
    monkeypatch.setattr("lineas_espn._session.get", boom)
    m2, meta = obtener_lineas_espn()
    assert meta.get("ok") is True
    assert meta.get("cache_disco") is True
    assert len(m2) == 1
