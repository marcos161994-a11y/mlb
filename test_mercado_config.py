"""Config mercado + dinero real conservador."""

import json
from pathlib import Path


def test_config_mercado_activo():
    cfg = json.loads(Path("config_experimento.json").read_text(encoding="utf-8"))
    estr = cfg.get("estrategia") or {}
    lineas = cfg.get("lineas") or {}

    assert cfg.get("modo_solo_modelo") is False
    assert estr.get("requiere_betmgm") is True
    assert float(cfg.get("stake_por_juego") or 0) <= 3.0
    assert int(estr.get("max_apuestas_dia") or 99) <= 4
    assert lineas.get("proveedor") == "espn"
    assert lineas.get("fallback_internet") is True
    books = lineas.get("bookmakers") or ""
    assert "draftkings" in books
    assert "pinnacle" in books or "fanduel" in books
    retries = lineas.get("minutos_retry_cuotas") or []
    assert 45 in retries and 30 in retries
    assert (cfg.get("telegram") or {}).get("solo_apostables") is True


def test_mercado_requiere_cuotas():
    from servidor_mlb import _mercado_requiere_cuotas

    assert _mercado_requiere_cuotas({"modo_solo_modelo": False, "estrategia": {"requiere_betmgm": True}})
    assert not _mercado_requiere_cuotas({"modo_solo_modelo": True, "estrategia": {"requiere_betmgm": True}})
    assert not _mercado_requiere_cuotas({"modo_solo_modelo": False, "estrategia": {"requiere_betmgm": False}})


def test_precalentar_omite_modo_papel():
    from servidor_mlb import precalentar_cuotas_mercado

    out = precalentar_cuotas_mercado({"modo_solo_modelo": True, "estrategia": {"requiere_betmgm": True}})
    assert out.get("omitido") is True


def test_modelo_no_solo_con_mercado():
    from modelo_mlb import _modo_solo_modelo
    import servidor_mlb as srv

    cfg = srv.cargar_config()
    assert _modo_solo_modelo(cfg) is False
