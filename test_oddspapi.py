"""Tests OddsPapi moneyline parsing (sin red)."""

from lineas_oddspapi import (
    _limpiar_key,
    _mejor_ml_fixture,
    buscar_lineas_partido,
    fingerprint_key,
    normalizar_nombre_equipo,
)


def test_mejor_ml_elige_mejor_cuota_v4():
    books = {
        "draftkings": {
            "markets": {
                "131": {
                    "outcomes": {
                        "131": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "home",
                                    "price": 1.80,
                                }
                            }
                        },
                        "132": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "away",
                                    "price": 2.10,
                                }
                            }
                        },
                    }
                }
            }
        },
        "fanduel": {
            "markets": {
                "131": {
                    "outcomes": {
                        "131": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "home",
                                    "price": 1.91,
                                }
                            }
                        },
                        "132": {
                            "players": {
                                "0": {
                                    "active": True,
                                    "bookmakerOutcomeId": "away",
                                    "price": 2.00,
                                }
                            }
                        },
                    }
                }
            }
        },
    }
    ml = _mejor_ml_fixture(books, ["draftkings", "fanduel"], api="v4")
    assert ml is not None
    assert ml["home"]["decimal"] == 1.91
    assert ml["home"]["casa"] == "fanduel"
    assert ml["away"]["decimal"] == 2.10
    assert ml["away"]["casa"] == "draftkings"


def test_mejor_ml_v5_oddquote():
    books = {
        "pinnacle": {
            "a": {
                "outcomeId": 131,
                "marketId": 131,
                "bookmakerOutcomeId": "1",
                "price": 1.85,
                "active": True,
            },
            "b": {
                "outcomeId": 132,
                "marketId": 131,
                "bookmakerOutcomeId": "2",
                "price": 2.05,
                "active": True,
            },
        },
        "draftkings": {
            "a": {
                "outcomeId": 131,
                "marketId": 131,
                "bookmakerOutcomeId": "home",
                "price": 1.90,
                "active": True,
            },
            "b": {
                "outcomeId": 132,
                "marketId": 131,
                "bookmakerOutcomeId": "away",
                "price": 1.95,
                "active": True,
            },
        },
    }
    ml = _mejor_ml_fixture(books, ["pinnacle", "draftkings"], api="v5")
    assert ml is not None
    assert ml["home"]["decimal"] == 1.90
    assert ml["home"]["casa"] == "draftkings"
    assert ml["away"]["decimal"] == 2.05
    assert ml["away"]["casa"] == "pinnacle"


def test_buscar_partido():
    mapa = {
        (
            normalizar_nombre_equipo("New York Yankees"),
            normalizar_nombre_equipo("Boston Red Sox"),
        ): {
            "away": {"decimal": 2.2, "american": 120, "casa": "pinnacle"},
            "home": {"decimal": 1.7, "american": -143, "casa": "pinnacle"},
        }
    }
    hit = buscar_lineas_partido(mapa, "New York Yankees", "Boston Red Sox")
    assert hit and hit["away"]["decimal"] == 2.2
    hit2 = buscar_lineas_partido(mapa, "Boston Red Sox", "New York Yankees")
    assert hit2 and hit2["home"]["decimal"] == 2.2


def test_limpiar_key_pegada_en_url():
    assert _limpiar_key('apiKey=abc123XYZ') == "abc123XYZ"
    assert _limpiar_key('  "abc123XYZ"  ') == "abc123XYZ"
    assert fingerprint_key("abcdefghijklmnop") == "abcd…mnop (len=16)"
    assert fingerprint_key("6f1f173c") == "*** (len=8)"


def test_redactar_secretos_no_deja_key_en_url():
    from lineas_oddspapi import redactar_secretos

    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    bruto = (
        "v4: 429 Client Error: Too Many Requests for url: "
        f"https://api.oddspapi.io/v4/fixtures?apiKey={uuid}&sportId=13"
    )
    limpio = redactar_secretos(bruto)
    assert uuid not in limpio
    assert "apiKey=***" in limpio
    assert "aaaaaaaa" not in limpio


def test_minutos_pausa_por_tipo_de_fallo():
    from lineas_oddspapi import (
        PAUSE_AUTH_MIN,
        PAUSE_RATE_MIN,
        fallo_abre_circuito,
        minutos_pausa_por_fallo,
    )

    assert minutos_pausa_por_fallo(401) == PAUSE_AUTH_MIN
    assert minutos_pausa_por_fallo(429) == PAUSE_RATE_MIN
    assert minutos_pausa_por_fallo(429, retry_after_segundos=120) == 5
    assert not fallo_abre_circuito({"http_status": 200, "mensaje": "sin moneyline MLB"})
    assert fallo_abre_circuito({"http_status": 401, "mensaje": "invalid_api_key"})
    assert fallo_abre_circuito({"http_status": 429, "mensaje": "rate limit"})


def test_circuito_abre_cierra_y_expira(monkeypatch, tmp_path):
    path = tmp_path / "oddspapi_circuit.json"
    monkeypatch.setattr("lineas_oddspapi._circuit_path", lambda: path)
    from datetime import datetime, timedelta
    import json

    from lineas_oddspapi import (
        abrir_circuito,
        cerrar_circuito,
        circuito_abierto,
        estado_circuito,
    )

    abrir_circuito("HTTP 401 invalid", http_status=401)
    assert circuito_abierto()
    st = estado_circuito()
    assert "pausa automática" in st["mensaje"]
    assert st.get("http_status") == 401
    cerrar_circuito()
    assert not circuito_abierto()
    assert not path.exists()

    path.write_text(
        json.dumps(
            {
                "hasta": (datetime.now() - timedelta(minutes=1)).isoformat(timespec="minutes"),
                "motivo": "viejo",
                "http_status": 429,
            }
        ),
        encoding="utf-8",
    )
    assert not circuito_abierto()
    assert not path.exists()


def test_circuito_abierto_no_llama_red(monkeypatch, tmp_path):
    path = tmp_path / "oddspapi_circuit.json"
    monkeypatch.setattr("lineas_oddspapi._circuit_path", lambda: path)
    from lineas_oddspapi import (
        abrir_circuito,
        invalidar_cache_oddspapi,
        obtener_lineas_oddspapi,
    )

    invalidar_cache_oddspapi()
    abrir_circuito("HTTP 401", http_status=401)

    def boom(*_a, **_k):
        raise AssertionError("no debe llamar OddsPapi con el circuito abierto")

    monkeypatch.setattr("lineas_oddspapi.requests.get", boom)
    mapa, meta = obtener_lineas_oddspapi(
        {"lineas": {"api_key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}
    )
    assert mapa == {}
    assert meta.get("circuito") is True
    assert "pausa" in (meta.get("mensaje") or "").lower()


def test_guardar_key_cierra_circuito(monkeypatch, tmp_path):
    monkeypatch.setattr("lineas_oddspapi._circuit_path", lambda: tmp_path / "c.json")
    monkeypatch.setattr("lineas_oddspapi.DATA_DIR", tmp_path)
    monkeypatch.setattr("lineas_oddspapi.KEY_FILE_DATA", tmp_path / "k.txt")
    monkeypatch.setattr("lineas_oddspapi.KEY_FILE", tmp_path / "k2.txt")
    from lineas_oddspapi import abrir_circuito, circuito_abierto, guardar_api_key

    abrir_circuito("401", http_status=401)
    assert circuito_abierto()
    guardar_api_key("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert not circuito_abierto()


def test_data_dir_gana_sobre_env_al_rotar(monkeypatch, tmp_path):
    """Rotar vía Action/API no debe quedar tapado por ODDSPAPI_API_KEY vieja en Render."""
    monkeypatch.setattr("lineas_oddspapi.DATA_DIR", tmp_path)
    monkeypatch.setattr("lineas_oddspapi.KEY_FILE_DATA", tmp_path / "k.txt")
    monkeypatch.setattr("lineas_oddspapi.KEY_FILE", tmp_path / "k2.txt")
    monkeypatch.setattr("lineas_oddspapi._circuit_path", lambda: tmp_path / "c.json")
    monkeypatch.setenv("ODDSPAPI_API_KEY", "ffffffff-1111-2222-3333-444444444444")
    from lineas_oddspapi import cargar_api_key, fingerprint_key, guardar_api_key

    nueva = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    info = guardar_api_key(nueva)
    assert info.get("aviso_env")
    key = cargar_api_key({})
    assert key == nueva
    assert fingerprint_key(key) == fingerprint_key(nueva)
    assert getattr(cargar_api_key, "last_source", "").startswith("oddspapi_api_key.txt")


def test_key_corta_no_se_guarda(monkeypatch, tmp_path):
    monkeypatch.setattr("lineas_oddspapi.DATA_DIR", tmp_path)
    monkeypatch.setattr("lineas_oddspapi.KEY_FILE_DATA", tmp_path / "k.txt")
    monkeypatch.setattr("lineas_oddspapi.KEY_FILE", tmp_path / "k2.txt")
    monkeypatch.setattr("lineas_oddspapi._circuit_path", lambda: tmp_path / "c.json")
    from lineas_oddspapi import guardar_api_key

    try:
        guardar_api_key("aaaa-bbbb-cccc")  # incompleta
        assert False, "debía fallar"
    except ValueError as e:
        assert "incompleta" in str(e).lower() or "36" in str(e)


def test_aplicar_con_circuito_usa_espn_sin_oddspapi(monkeypatch, tmp_path):
    monkeypatch.setattr("lineas_oddspapi._circuit_path", lambda: tmp_path / "c.json")
    from lineas_oddspapi import abrir_circuito

    abrir_circuito("429", http_status=429)
    called = {"papi": 0, "espn": 0}

    def fake_papi(juegos, cfg):
        called["papi"] += 1
        raise AssertionError("OddsPapi no debe aplicarse con circuito abierto")

    def fake_espn(juegos, cfg=None, solo_vacios=True):
        called["espn"] += 1
        for j in juegos:
            j["odds_away_decimal"] = 2.1
            j["odds_home_decimal"] = 1.8
            j["lineas_fuente"] = "draftkings"
        return juegos, {"ok": True, "partidos_aplicados": len(juegos), "mensaje": "espn"}

    monkeypatch.setattr("lineas_oddspapi.aplicar_lineas_oddspapi", fake_papi)
    monkeypatch.setattr("lineas_espn.aplicar_lineas_espn", fake_espn)
    from lineas_betmgm import aplicar_lineas_a_juegos

    out, meta = aplicar_lineas_a_juegos(
        [{"visitante": "A", "home": "B"}],
        {"lineas": {"proveedor": "oddspapi", "fallback_internet": True}},
    )
    assert called["papi"] == 0
    assert called["espn"] == 1
    assert meta.get("circuito") is True
    assert meta.get("fallback_espn") is True
    assert meta.get("ok") is True
    assert out[0]["odds_home_decimal"] == 1.8
    assert "pausa" in (meta.get("mensaje") or "").lower()
