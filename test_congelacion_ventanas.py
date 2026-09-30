"""Congelado redundante: T-90/T-60/T-30/T-10, catch-up y sin pick tras el primer pitch."""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import servidor_mlb as s


TZ = ZoneInfo("America/Puerto_Rico")
CFG = {
    "timezone": "America/Puerto_Rico",
    "temporada_mlb": 2026,
    "stake_por_juego": 3,
    "minutos_antes_juego": 60,
    "ventanas_congelacion": [90, 60, 30, 10],
    "estrategia": {},
    "usar_mente": False,
}


def _juego(gid, mins, ahora, estado="PROGRAMADO", pick="Home ML", odds=1.8):
    inicio = ahora + timedelta(minutes=mins)
    return {
        "id": gid,
        "estado": estado,
        "visitante": "Away",
        "home": "Home",
        "pick": pick,
        "probPick": 61,
        "odds": odds,
        "odds_american": -125,
        "inicio_juego": inicio.isoformat(),
        "lineas_fuente": "draftkings",
    }


def test_cron_despierta_y_reintenta_antes_de_congelar():
    text = Path(".github/workflows/cloud-cron.yml").read_text(encoding="utf-8")
    health = text.find("Despertar servicio")
    freeze = text.find("auto-bloqueo-externo")
    assert health != -1 and freeze != -1 and health < freeze
    assert "X-Cron-Secret" in text
    assert "No se pudo llamar al congelado tras reintentos" in text
    assert "ventanas_perdidas" in text


def test_ventanas_default_y_orden():
    assert s.ventanas_congelacion({}) == [90, 60, 30, 10]
    assert s.ventanas_congelacion({"ventanas_congelacion": "30, 90, 10, 60"}) == [90, 60, 30, 10]
    assert s.horizonte_congelacion_min({}) == 90


def test_se_puede_congelar_solo_dentro_del_horizonte_y_antes_del_pitch():
    ahora = datetime(2026, 9, 30, 18, 0, tzinfo=TZ)
    cfg = CFG
    assert s.juego_se_puede_congelar(_juego("a", 75, ahora), cfg, ahora) == (True, "T-90")
    assert s.juego_se_puede_congelar(_juego("b", 40, ahora), cfg, ahora) == (True, "T-60")
    assert s.juego_se_puede_congelar(_juego("c", 8, ahora), cfg, ahora) == (True, "T-10")
    ok, motivo = s.juego_se_puede_congelar(_juego("d", 120, ahora), cfg, ahora)
    assert ok is False
    assert "T-90" in motivo
    ok, motivo = s.juego_se_puede_congelar(
        _juego("e", -3, ahora, estado="PROGRAMADO"), cfg, ahora
    )
    assert ok is False
    assert "pitch" in motivo
    ok, _ = s.juego_se_puede_congelar(_juego("f", -8, ahora, estado="EN VIVO"), cfg, ahora)
    assert ok is False
    ok, _ = s.juego_se_puede_congelar(_juego("g", -40, ahora, estado="FINALIZADO"), cfg, ahora)
    assert ok is False


def test_catchup_congela_lo_no_empezado_y_no_repite(monkeypatch):
    ahora = datetime.now(TZ)
    mem = {"dias": [], "stake_por_juego": 3, "dia_actual": 1, "experimento_activo": True}

    monkeypatch.setattr(s, "cargar_memoria", lambda: mem)
    monkeypatch.setattr(s, "guardar_memoria", lambda m: mem.update(m) or m)
    monkeypatch.setattr(s, "cargar_config", lambda: dict(CFG))
    monkeypatch.setattr(s, "generar_briefing_juego", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(s, "mente_conclusion", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(s, "actualizar_clv_registro", lambda *_a, **_k: None)
    monkeypatch.setattr(s, "apostable_con_mercado", lambda *_a, **_k: False)
    monkeypatch.setattr(s, "tiene_cuota_mercado", lambda *_a, **_k: False)
    monkeypatch.setattr(s, "stake_virtual_prediccion", lambda *_a, **_k: 3.0)

    juegos = [
        _juego("cerca", 40, ahora, odds=1.91),
        _juego("temprano", 75, ahora, odds=2.1),
        _juego("lejos", 180, ahora),
        _juego("reloj", -4, ahora, estado="PROGRAMADO"),
        _juego("vivo", -6, ahora, estado="EN VIVO"),
        _juego("final", -90, ahora, estado="FINALIZADO"),
    ]
    monkeypatch.setattr(s, "obtener_juegos_fecha", lambda *_a, **_k: juegos)

    out = s.registrar_predicciones_del_dia(forzar=False)
    assert out["predicciones_nuevas"] == 2
    preds = {p["game_id"]: p for p in mem["dias"][0]["predicciones"]}
    assert set(preds) == {"cerca", "temprano"}
    assert preds["cerca"]["ventana_congelacion"] == "T-60"
    assert preds["temprano"]["ventana_congelacion"] == "T-90"
    assert preds["cerca"]["odds_congelada"] == 1.91
    assert preds["cerca"]["congelado_en_gracia"] is False
    assert out["congelacion"]["ventanas_perdidas"] == 3
    assert out["congelacion"]["ventanas_recuperadas"] == 1

    juegos[0]["odds"] = 2.4
    juegos[0]["pick"] = "Away ML"
    out2 = s.registrar_predicciones_del_dia(forzar=True)
    assert out2["predicciones_nuevas"] == 0
    assert len(mem["dias"][0]["predicciones"]) == 2
    otra = next(p for p in mem["dias"][0]["predicciones"] if p["game_id"] == "cerca")
    assert otra["pick"] == "Home ML"
    assert otra["odds_congelada"] == 1.91
    assert out2["congelacion"]["ventanas_perdidas"] == 3

    health = s.resumen_congelacion_health(CFG)
    assert health["ventanas_perdidas"] == 3
    assert health["ventanas_min"] == [90, 60, 30, 10]
    assert health["ultima_alerta"]
    cuerpo = s.api_health()
    assert cuerpo["congelacion"]["ventanas_perdidas"] == 3
    assert cuerpo["ok"] is True
