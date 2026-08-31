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
    assert lineas.get("proveedor") == "oddspapi"
    assert lineas.get("fallback_internet") is True
    assert (cfg.get("telegram") or {}).get("solo_apostables") is True


def test_modelo_no_solo_con_mercado():
    from modelo_mlb import _modo_solo_modelo
    import servidor_mlb as srv

    cfg = srv.cargar_config()
    assert _modo_solo_modelo(cfg) is False
