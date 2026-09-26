"""La bóveda guarda en disco y la mente del servidor no importa CrewAI."""

from pathlib import Path

import taller.mente_pc as mente_pc
from taller.boveda import guardar, ultimas


def test_boveda_recuerda(tmp_path, monkeypatch):
    monkeypatch.setattr("taller.boveda.BOVEDA", tmp_path)
    monkeypatch.setattr("taller.boveda.MEMORIA", tmp_path / "memoria.jsonl")
    guardar("Humedad alta sigue activa", tipo="reporte")
    notas = ultimas(3)
    assert notas[-1]["texto"] == "Humedad alta sigue activa"
    assert "chroma" not in notas[-1]


def test_pasada_guarda_sin_ejecutar(tmp_path, monkeypatch):
    monkeypatch.setattr("taller.boveda.BOVEDA", tmp_path)
    monkeypatch.setattr("taller.boveda.MEMORIA", tmp_path / "memoria.jsonl")
    resumen = tmp_path / "resumen.txt"
    resumen.write_text("Habilidad activa: Humedad alta\n", encoding="utf-8")
    monkeypatch.setattr(mente_pc, "correr", lambda: resumen)
    mente_pc.main()
    notas = ultimas(1)
    assert notas[-1]["texto"].startswith("Pasada de las ")
    assert "Humedad alta" in notas[-1]["texto"]
    assert (tmp_path / "memoria.jsonl").exists()


def test_servidor_no_lleva_crewai():
    src = Path("mente_mlb.py").read_text(encoding="utf-8")
    assert "def mente_conclusion" in src
    assert "crewai" not in src.lower()
    assert "OPENAI_API_KEY" not in src
    pc = Path("taller/mente_pc.py").read_text(encoding="utf-8")
    assert "exec(" not in pc
    assert "openinterpreter" not in pc.lower()
    agenda = Path("taller/programar.bat").read_text(encoding="utf-8")
    assert "08:00" in agenda
    assert "23:30" in agenda
    assert Path("taller/pasar.bat").exists()
    raiz = Path("requirements.txt").read_text(encoding="utf-8").lower()
    assert "crewai" not in raiz
    assert "chromadb" not in raiz
