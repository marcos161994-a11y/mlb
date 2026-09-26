"""El taller de la PC escribe el resumen y no toca las habilidades vivas."""

from pathlib import Path

import taller.noche as noche


def test_resumen_tiene_los_tres_pasos(tmp_path, monkeypatch):
    monkeypatch.setattr(noche, "SALIDA", tmp_path)
    monkeypatch.setattr(
        noche,
        "leer_reporte",
        lambda: {
            "habilidad": "Humedad alta",
            "motivo": "Falta de análisis de física climática",
            "prueba": "10 de 26",
            "limitaciones": [{"texto": "Limitación detectada: Falta de análisis de física climática"}],
        },
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    destino = noche.correr()
    texto = destino.read_text(encoding="utf-8")
    assert "1. La mente que apuesta" in texto
    assert "2. LangChain investiga" in texto
    assert "3. CrewAI propone" in texto
    assert "Humedad alta" in texto
    assert "no cambia las apuestas" in texto
    assert Path("skills/humedad_pelota.py").exists()
