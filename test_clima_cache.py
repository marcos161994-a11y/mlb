import json

import pytest

import clima


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            err = Exception(f"{self.status_code} Client Error")
            err.response = self
            raise err

    def json(self):
        return self._payload


def _bloque(temp=80.0, viento=5.0, humedad=75):
    return {
        "current": {
            "temperature_2m": temp,
            "wind_speed_10m": viento,
            "relative_humidity_2m": humedad,
        }
    }


@pytest.fixture(autouse=True)
def _limpio(tmp_path, monkeypatch):
    monkeypatch.setattr(clima, "CACHE_PATH", tmp_path / "clima_cache.json")
    clima._clima_cache.clear()
    clima._dia_cache.clear()
    clima._cache_leido = False
    clima._pausa_hasta = 0.0
    yield
    clima._pausa_hasta = 0.0


def test_cache_en_disco_sobrevive_al_reinicio(monkeypatch):
    llamadas = []

    def _get(url, params=None, timeout=None):
        llamadas.append(params)
        return _Resp(_bloque())

    monkeypatch.setattr(clima._session, "get", _get)
    primero = clima.obtener_clima_estadio(147, "2026-09-29T18:05:00")
    assert primero["humedad"] == 75
    assert len(llamadas) == 1

    # Render reinicia el proceso: la memoria se va, el archivo queda.
    clima._clima_cache.clear()
    clima._dia_cache.clear()
    clima._cache_leido = False
    segundo = clima.obtener_clima_estadio(147, "2026-09-29T18:05:00")
    assert segundo["humedad"] == 75
    assert len(llamadas) == 1


def test_cache_viejo_no_se_carga(monkeypatch):
    clima.CACHE_PATH.write_text(
        json.dumps(
            {"entradas": {"147:2020-05-01T18": {"dia": "2020-05-01", "dato": {"ok": True}}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        clima._session, "get", lambda *a, **k: _Resp(_bloque(humedad=40))
    )
    dato = clima.obtener_clima_estadio(147, "2020-05-01T18:05:00")
    assert dato["humedad"] == 40


def test_429_pausa_y_no_vuelve_a_pedir(monkeypatch):
    llamadas = []

    def _get(url, params=None, timeout=None):
        llamadas.append(params)
        return _Resp({"error": True}, status=429)

    monkeypatch.setattr(clima._session, "get", _get)
    primero = clima.obtener_clima_estadio(147, "2026-09-29T18:05:00")
    assert primero["ok"] is False
    assert primero["sin_dato"] is True
    segundo = clima.obtener_clima_estadio(121, "2026-09-29T19:05:00")
    assert segundo["ok"] is False
    assert len(llamadas) == 1


def test_tras_429_se_reusa_lo_ultimo_del_dia(monkeypatch):
    estado = {"falla": False}

    def _get(url, params=None, timeout=None):
        if estado["falla"]:
            return _Resp({}, status=429)
        return _Resp(_bloque(humedad=82))

    monkeypatch.setattr(clima._session, "get", _get)
    assert clima.obtener_clima_estadio(147, "2026-09-29T13:05:00")["humedad"] == 82
    estado["falla"] = True
    tarde = clima.obtener_clima_estadio(147, "2026-09-29T19:05:00")
    assert tarde["ok"] is True
    assert tarde["humedad"] == 82
    assert tarde["fuente"] == "open-meteo-cache"


def test_precarga_agrupa_el_slate_en_una_peticion(monkeypatch):
    llamadas = []

    def _get(url, params=None, timeout=None):
        llamadas.append(params)
        n = len(str(params["latitude"]).split(","))
        return _Resp([_bloque(humedad=60 + i) for i in range(n)])

    monkeypatch.setattr(clima._session, "get", _get)
    juegos = [
        {"home_id": 147, "inicio_juego": "2026-09-29T18:05:00"},
        {"home_id": 121, "inicio_juego": "2026-09-29T18:05:00"},
        {"home_id": 111, "inicio_juego": "2026-09-29T18:05:00"},
        {"home_id": 139, "inicio_juego": "2026-09-29T18:05:00"},  # domo
    ]
    resumen = clima.precargar_clima_dia(juegos)
    assert resumen["pedidos"] == 1
    assert resumen["guardados"] == 3
    assert len(str(llamadas[0]["latitude"]).split(",")) == 3

    # Ya precargado: pedir juego por juego no vuelve a salir a internet.
    for juego in juegos[:3]:
        dato = clima.obtener_clima_estadio(juego["home_id"], juego["inicio_juego"])
        assert dato["ok"] is True
    assert len(llamadas) == 1


def test_precarga_separa_por_hora_de_inicio(monkeypatch):
    horas = []

    def _get(url, params=None, timeout=None):
        horas.append(params.get("start_hour"))
        n = len(str(params["latitude"]).split(","))
        return _Resp([_bloque() for _ in range(n)])

    monkeypatch.setattr(clima._session, "get", _get)
    resumen = clima.precargar_clima_dia(
        [
            {"home_id": 147, "inicio_juego": "2026-09-29T13:05:00"},
            {"home_id": 121, "inicio_juego": "2026-09-29T19:05:00"},
        ]
    )
    assert resumen["pedidos"] == 2
    assert len(set(horas)) == 2


def test_domo_no_pide_nada(monkeypatch):
    def _explota(*a, **k):
        raise AssertionError("no debe salir a internet")

    monkeypatch.setattr(clima._session, "get", _explota)
    dato = clima.obtener_clima_estadio(139, "2026-09-29T18:05:00")
    assert dato["fuente"] == "domo"
