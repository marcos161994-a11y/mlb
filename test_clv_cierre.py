"""CLV de cierre: puntos sin vig, % de precio, sin_cierre y persistencia.

La medición no mueve el stake ni reescribe el precio de la apuesta.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import clv_mlb as clv
import memoria_fusion
from memoria_store import SqliteStore


AHORA = datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc)


def _libro(casa: str, ml_away: int, ml_home: int, *, minutos: int = 1) -> dict:
    from lineas_betmgm import american_a_decimal

    cuando = (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat()
    return {
        "casa": casa,
        "provider": casa,
        "ml_away": ml_away,
        "ml_home": ml_home,
        "away": american_a_decimal(ml_away),
        "home": american_a_decimal(ml_home),
        "fetched_at": cuando,
        "estado_cuota": "pre",
        "stale": False,
        "en_vivo": False,
    }


def _juego(gid: str, mins: float, *, estado: str = "PROGRAMADO") -> dict:
    inicio = AHORA + timedelta(minutes=mins)
    return {
        "id": gid,
        "estado": estado,
        "visitante": "Away Team",
        "home": "Home Team",
        "fecha": "2026-09-30",
        "inicio_juego": inicio.isoformat(),
        "pick": "Away Team ML",
    }


def _apuesta(**extra) -> dict:
    base = {
        "game_id": "g1",
        "visitante": "Away Team",
        "home": "Home Team",
        "pick": "Away Team ML",
        "odds": 2.5,
        "odds_american": 150,
        "fuente_momio": "fanduel",
        "casa": "fanduel",
        "lineas_fuente": "fanduel",
        "stake": 3.0,
        "estado": "pendiente",
        "inicio_juego": (AHORA + timedelta(minutes=3)).isoformat(),
    }
    base.update(extra)
    return base


def test_clv_positivo_si_el_precio_era_mejor_que_el_cierre():
    # +150 contra un cierre +120 / -125. Sin vig el lado vale 45%; la entrada implica 40%.
    med = clv.medir_clv_precios(2.5, 2.2, 1.8)
    assert med is not None
    assert med["novig"] is True
    assert med["clv_pp"] == 5.0
    assert med["clv_pct"] == 13.64
    assert med["clv_pp"] > 0 and med["clv_pct"] > 0


def test_clv_negativo_si_el_cierre_paga_mas():
    med = clv.medir_clv_precios(1.8, 2.0, 1.9)
    assert med is not None
    assert med["novig"] is True
    assert med["clv_pct"] == -10.0
    assert med["clv_pp"] < 0


def test_sin_el_otro_lado_no_quita_el_vig():
    med = clv.medir_clv_precios(2.5, 2.2, None)
    assert med is not None
    assert med["novig"] is False
    assert med["clv_pct"] == 13.64
    # 1/2.2 - 1/2.5, en puntos, sin repartir el overround.
    assert med["clv_pp"] == round((1 / 2.2 - 1 / 2.5) * 100, 2)


def test_captura_en_t5_usa_la_casa_de_la_apuesta_y_no_toca_el_stake():
    apuesta = _apuesta()
    juego = _juego("g1", 3)
    vistos = []

    def fetcher(juego_in):
        vistos.append(juego_in["id"])
        juego_in["lineas_libros"] = [
            _libro("draftkings", 110, -130),
            _libro("fanduel", 120, -125),
        ]
        return juego_in

    mem = {"dias": [{"fecha": "2026-09-30", "apuestas": [apuesta], "predicciones": []}]}
    info = clv.sincronizar_clv(mem, [juego], ahora=AHORA, fetcher=fetcher)
    assert info["cambios"] >= 1
    assert vistos == ["g1"]
    assert apuesta["stake"] == 3.0
    assert apuesta["odds"] == 2.5
    assert apuesta["odds_american"] == 150
    clv_reg = apuesta["clv"]
    assert isinstance(clv_reg, dict)
    assert clv_reg["casa_cierre"] == "fanduel"
    assert clv_reg["misma_casa"] is True
    assert clv_reg["clv_pp"] == 5.0
    assert clv_reg["clv_pct"] == 13.64
    assert clv_reg["novig"] is True
    assert clv_reg["precio_apuesta"] == 2.5
    assert clv_reg["serie"] == "dinero"
    assert "clv_motivo" not in apuesta


def test_si_la_casa_no_cotiza_usa_la_siguiente_y_la_anota():
    apuesta = _apuesta(fuente_momio="pinnacle", casa="pinnacle", lineas_fuente="pinnacle")
    juego = _juego("g1", 2)

    def fetcher(juego_in):
        juego_in["lineas_libros"] = [_libro("draftkings", 130, -140)]
        return juego_in

    mem = {"dias": [{"fecha": "2026-09-30", "apuestas": [apuesta], "predicciones": []}]}
    clv.sincronizar_clv(mem, [juego], ahora=AHORA, fetcher=fetcher)
    assert apuesta["clv"]["casa_cierre"] == "draftkings"
    assert apuesta["clv"]["casa_apuesta"] == "pinnacle"
    assert apuesta["clv"]["misma_casa"] is False
    assert apuesta["odds"] == 2.5


def test_sin_cierre_no_inventa_precio_cuando_la_ventana_ya_paso():
    apuesta = _apuesta(odds=1.91, odds_american=-110)
    juego = _juego("g1", -30, estado="FINALIZADO")
    llamado = []

    def fetcher(juego_in):
        llamado.append(juego_in["id"])
        juego_in["lineas_libros"] = [_libro("fanduel", 150, -160)]
        return juego_in

    mem = {"dias": [{"fecha": "2026-09-30", "apuestas": [apuesta], "predicciones": []}]}
    clv.sincronizar_clv(mem, [juego], ahora=AHORA, fetcher=fetcher)
    assert llamado == []
    assert apuesta["clv"] == "sin_cierre"
    assert apuesta["clv_motivo"]
    assert "precio" in apuesta["clv_motivo"] or "cierre" in apuesta["clv_motivo"]
    assert apuesta["odds"] == 1.91
    assert apuesta["stake"] == 3.0
    assert "clv_pp" not in apuesta


def test_precio_viejo_no_cuenta_como_cierre():
    apuesta = _apuesta()
    juego = _juego("g1", 3)

    def fetcher(juego_in):
        juego_in["lineas_libros"] = [_libro("fanduel", 120, -125, minutos=40)]
        return juego_in

    mem = {"dias": [{"fecha": "2026-09-30", "apuestas": [apuesta], "predicciones": []}]}
    clv.sincronizar_clv(mem, [juego], ahora=AHORA, fetcher=fetcher)
    assert "clv" not in apuesta
    assert apuesta["odds"] == 2.5
    assert apuesta["stake"] == 3.0


def test_catchup_en_t2_si_mide_y_en_t40_todavia_no():
    cerca = _apuesta(game_id="cerca")
    lejos = _apuesta(game_id="lejos", inicio_juego=(AHORA + timedelta(minutes=40)).isoformat())
    pedidos = []

    def fetcher(juego_in):
        pedidos.append(juego_in["id"])
        juego_in["lineas_libros"] = [_libro("fanduel", 120, -125)]
        return juego_in

    mem = {
        "dias": [
            {
                "fecha": "2026-09-30",
                "apuestas": [cerca, lejos],
                "predicciones": [],
            }
        ]
    }
    juegos = [_juego("cerca", 2), _juego("lejos", 40)]
    clv.sincronizar_clv(mem, juegos, ahora=AHORA, fetcher=fetcher)
    assert pedidos == ["cerca"]
    assert isinstance(cerca["clv"], dict)
    assert "clv" not in lejos
    assert lejos["odds"] == 2.5


def test_pick_sin_apuesta_es_otra_serie():
    pred = {
        "game_id": "p1",
        "visitante": "Away Team",
        "home": "Home Team",
        "pick": "Away Team ML",
        "odds": 2.1,
        "odds_american": 110,
        "fuente_momio": "draftkings",
        "lineas_fuente": "draftkings",
        "con_dinero": False,
        "estado": "pendiente",
        "inicio_juego": (AHORA + timedelta(minutes=4)).isoformat(),
    }
    juego = _juego("p1", 4)

    def fetcher(juego_in):
        juego_in["lineas_libros"] = [_libro("draftkings", 100, -120)]
        return juego_in

    mem = {"dias": [{"fecha": "2026-09-30", "apuestas": [], "predicciones": [pred]}]}
    clv.sincronizar_clv(mem, [juego], ahora=AHORA, fetcher=fetcher)
    assert pred["clv"]["serie"] == "sin_apuesta"
    assert pred["clv"]["clv_pp"] > 0
    resumen = clv.resumen_clv_publico(mem)
    assert resumen["n"] == 0
    assert resumen["sin_apuesta"]["n"] == 1
    assert resumen["sin_apuesta"]["promedio_pp"] == pred["clv"]["clv_pp"]


def test_sin_precio_de_entrada_no_fabrica_clv():
    pred = {
        "game_id": "s1",
        "visitante": "Away Team",
        "home": "Home Team",
        "pick": "Away Team ML",
        "odds": 1.91,
        "fuente_momio": "sin_momio_real",
        "estado_registro": "registrado sin apuesta",
        "sin_momio_real": True,
        "estado": "pendiente",
    }
    juego = _juego("s1", -15, estado="FINALIZADO")
    mem = {"dias": [{"fecha": "2026-09-30", "apuestas": [], "predicciones": [pred]}]}
    clv.sincronizar_clv(mem, [juego], ahora=AHORA, fetcher=lambda j: j)
    assert pred["clv"] == "sin_cierre"
    assert pred["clv_motivo"] == "sin precio de entrada real"
    assert pred["odds"] == 1.91


def test_backfill_solo_usa_un_cierre_guardado_en_la_ventana():
    bueno = _apuesta(
        game_id="bueno",
        estado="ganada",
        clv_pin_cierre_away=2.2,
        clv_pin_cierre_home=1.8,
        clv_fuente="casa_unica_fanduel",
        clv_cierre_en=(AHORA - timedelta(minutes=3)).isoformat(),
        inicio_juego=AHORA.isoformat(),
    )
    temprano = _apuesta(
        game_id="temprano",
        estado="perdida",
        clv_pin_cierre_away=2.2,
        clv_pin_cierre_home=1.8,
        clv_fuente="casa_unica_fanduel",
        clv_cierre_en=(AHORA - timedelta(minutes=40)).isoformat(),
        inicio_juego=AHORA.isoformat(),
    )
    mediana = _apuesta(
        game_id="mediana",
        estado="ganada",
        clv_pin_cierre_away=2.2,
        clv_pin_cierre_home=1.8,
        clv_fuente="mediana_3_casas",
        clv_cierre_en=(AHORA - timedelta(minutes=2)).isoformat(),
        inicio_juego=AHORA.isoformat(),
    )
    mem = {"dias": [{"fecha": "2026-09-30", "apuestas": [bueno, temprano, mediana], "predicciones": []}]}
    assert clv.rellenar_clv_historico(mem) == 3
    assert bueno["clv"]["clv_pp"] == 5.0
    assert bueno["clv"]["casa_cierre"] == "fanduel"
    assert temprano["clv"] == "sin_cierre"
    assert mediana["clv"] == "sin_cierre"
    assert clv.rellenar_clv_historico(mem) == 0


def test_las_ocho_liquidadas_quedan_sin_cierre():
    raw = json.loads(Path("memoria_auditoria.json").read_text(encoding="utf-8"))
    mem = {"dias": raw["dias"], "capital": raw.get("capital"), "capital_inicial": raw.get("capital_inicial")}
    antes = []
    for dia in mem["dias"]:
        for ap in dia.get("apuestas") or []:
            if ap.get("estado") in ("ganada", "perdida", "push"):
                antes.append((ap["game_id"], ap["odds"], ap["stake"], ap["estado"]))
    assert len(antes) == 8
    assert clv.rellenar_clv_historico(mem) == 8
    vistos = []
    for dia in mem["dias"]:
        for ap in dia.get("apuestas") or []:
            if ap.get("estado") in ("ganada", "perdida", "push"):
                vistos.append(ap)
                assert ap["clv"] == "sin_cierre"
                assert ap["clv_motivo"] == "sin precio de cierre guardado"
                assert "clv_pp" not in ap
        for pred in dia.get("predicciones") or []:
            assert "clv" not in pred
    assert [(a["game_id"], a["odds"], a["stake"], a["estado"]) for a in vistos] == antes
    assert clv.rellenar_clv_historico(mem) == 0
    resumen = clv.resumen_clv_publico(mem)
    assert resumen["n"] == 0
    assert resumen["sin_cierre"] == 8
    assert resumen["promedio_pp"] is None
    assert resumen["bate_cierre_pct"] is None
    assert resumen["ultimos"] == []


def test_resumen_promedio_bate_cierre_y_ultimos():
    def fila(pp, pct, ts):
        return {
            "stake": 3,
            "estado": "ganada",
            "pick": f"P {ts}",
            "game_id": ts,
            "fuente_momio": "draftkings",
            "clv": {
                "clv_pp": pp,
                "clv_pct": pct,
                "precio_cierre": 1.9,
                "precio_apuesta": 2.0,
                "casa_cierre": "draftkings",
                "timestamp": ts,
                "novig": True,
                "serie": "dinero",
            },
        }

    mem = {
        "dias": [
            {
                "fecha": "2026-09-01",
                "apuestas": [fila(2.0, 4.0, "2026-09-01T20:00:00+00:00"), fila(-1.0, -2.0, "2026-09-01T21:00:00+00:00")],
                "predicciones": [],
            }
        ]
    }
    resumen = clv.resumen_clv_publico(mem)
    assert resumen["n"] == 2
    assert resumen["promedio_pp"] == 0.5
    assert resumen["promedio_pct"] == 1.0
    assert resumen["bate_cierre_pct"] == 50.0
    assert resumen["ultimos"][0]["timestamp"].startswith("2026-09-01T21")
    assert len(resumen["ultimos"]) == 2


def test_persistencia_sqlite_y_merge_conservan_el_cierre(tmp_path):
    medido = _apuesta(
        game_id="m1",
        estado="ganada",
        clv={
            "precio_apuesta": 2.5,
            "precio_cierre": 2.2,
            "casa_cierre": "fanduel",
            "clv_pp": 5.0,
            "clv_pct": 13.64,
            "timestamp": "2026-09-30T17:55:00+00:00",
            "novig": True,
            "serie": "dinero",
        },
    )
    vacio = _apuesta(game_id="m1", estado="ganada")
    vacio.pop("clv", None)
    store = SqliteStore(tmp_path / "memoria.sqlite")
    doc = {
        "capital": 100,
        "capital_inicial": 100,
        "dia_actual": 1,
        "dias": [{"fecha": "2026-09-30", "dia": 1, "apuestas": [medido], "predicciones": []}],
    }
    store.guardar(doc)
    cargado = store.cargar()
    assert cargado["dias"][0]["apuestas"][0]["clv"]["clv_pp"] == 5.0
    assert cargado["dias"][0]["apuestas"][0]["stake"] == 3.0

    base = {
        "capital_inicial": 100,
        "capital": 100,
        "dias": [{"fecha": "2026-09-30", "apuestas": [vacio], "predicciones": []}],
    }
    extra = {
        "capital_inicial": 100,
        "dias": [{"fecha": "2026-09-30", "apuestas": [medido], "predicciones": []}],
    }
    unido = memoria_fusion.fusionar_memoria(base, extra)
    assert unido["dias"][0]["apuestas"][0]["clv"]["clv_pp"] == 5.0

    peor = _apuesta(game_id="m1", estado="ganada", clv="sin_cierre", clv_motivo="sin precio de cierre guardado")
    mejor = memoria_fusion.fusionar_memoria(
        {"capital_inicial": 100, "dias": [{"fecha": "2026-09-30", "apuestas": [peor], "predicciones": []}]},
        {"capital_inicial": 100, "dias": [{"fecha": "2026-09-30", "apuestas": [medido], "predicciones": []}]},
    )
    assert mejor["dias"][0]["apuestas"][0]["clv"]["precio_cierre"] == 2.2

    al_reves = memoria_fusion.fusionar_memoria(
        {"capital_inicial": 100, "dias": [{"fecha": "2026-09-30", "apuestas": [medido], "predicciones": []}]},
        {"capital_inicial": 100, "dias": [{"fecha": "2026-09-30", "apuestas": [peor], "predicciones": []}]},
    )
    assert al_reves["dias"][0]["apuestas"][0]["clv"]["clv_pp"] == 5.0


def test_fetcher_usa_la_cadena_y_el_scheduler_programa_t5(monkeypatch):
    import servidor_mlb as srv

    llamados = []

    def falso(juegos, cfg):
        llamados.append((juegos[0].get("visitante"), juegos[0].get("fecha"), "espn" in str(cfg)))
        juegos[0]["lineas_libros"] = [_libro("draftkings", -110, -110)]
        juegos[0]["fuente_momio"] = "draftkings"
        return juegos, {"ok": True}

    monkeypatch.setattr("cadena_momios.aplicar_cadena_momios", falso)
    juego = _juego("z", 3)
    juego["fecha"] = "2026-09-30"
    out = srv._fetcher_cierre_cadena(juego, {"lineas": {}})
    assert llamados and llamados[0][0] == "Away Team"
    assert out["lineas_libros"][0]["casa"] == "draftkings"
    assert juego.get("lineas_libros") is None

    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Puerto_Rico")
    inicio = dt.now(tz) + timedelta(hours=3)
    bloqueo = inicio - timedelta(minutes=60)
    slate = {
        "id": "777001",
        "estado": "PROGRAMADO",
        "visitante": "A",
        "home": "B",
        "inicio_juego": inicio.isoformat(),
        "hora_bloqueo": bloqueo.isoformat(),
        "hora_bloqueo_txt": "x",
        "hora_inicio_txt": "y",
    }
    monkeypatch.setattr(srv, "obtener_juegos_fecha", lambda *_a, **_k: [slate])
    monkeypatch.setattr(srv, "cargar_config", lambda: {"timezone": "America/Puerto_Rico", "minutos_antes_juego": 60, "lineas": {}})
    monkeypatch.setattr(srv, "ahora_simulado", lambda: dt.now(tz))
    added = []

    class FakeScheduler:
        def get_jobs(self):
            return []

        def remove_job(self, _jid):
            pass

        def add_job(self, func, trigger, id, replace_existing=True):
            added.append(id)

    monkeypatch.setattr(srv, "scheduler", FakeScheduler())
    srv.programar_bloqueos_por_juego()
    assert "cierre_clv_777001" in added
    assert "congelar_juego_777001_90" in added


def test_resultados_y_health_exponen_el_bloque(monkeypatch):
    import servidor_mlb as srv
    from resultados_mlb import calcular_resultados

    apuesta = _apuesta(
        estado="ganada",
        profit=4.5,
        clv={
            "clv_pp": 1.5,
            "clv_pct": 3.0,
            "precio_cierre": 2.2,
            "precio_apuesta": 2.5,
            "casa_cierre": "fanduel",
            "timestamp": "2026-09-30T17:55:00+00:00",
            "novig": True,
            "serie": "dinero",
        },
    )
    sin = _apuesta(game_id="g2", estado="perdida", profit=-3, clv="sin_cierre", clv_motivo="sin precio de cierre guardado")
    mem = {
        "capital_inicial": 100,
        "capital": 101.5,
        "stake_por_juego": 3,
        "dias": [
            {
                "fecha": "2026-09-30",
                "dia": 1,
                "apuestas": [apuesta, sin],
                "predicciones": [],
            }
        ],
    }
    out = calcular_resultados(mem)
    assert out["clv"]["n"] == 1
    assert out["clv"]["promedio_pp"] == 1.5
    assert out["clv"]["bate_cierre_pct"] == 100.0
    assert out["clv"]["sin_cierre"] == 1
    assert out["clv"]["ultimos"][0]["casa_cierre"] == "fanduel"
    assert out["resumen"]["n"] == 2

    monkeypatch.setattr(srv, "cargar_memoria", lambda *a, **k: mem)
    cuerpo = srv.api_health()
    assert cuerpo["clv"]["n"] == 1
    assert cuerpo["clv"]["sin_cierre"] == 1
    assert len(cuerpo["clv"]["ultimos"]) == 1
    assert "congelacion" in cuerpo


def test_panel_muestra_la_seccion_de_clv():
    html = Path("QuantumMLB.html").read_text(encoding="utf-8")
    assert "Valor de cierre (CLV)" in html
    assert "bloqueClv" in html
    assert "Bate el cierre" in html
    assert "Picks registrados sin apuesta" in html
