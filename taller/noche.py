"""Taller de noche para la PC.

1. La mente de Render sigue apostando solo con habilidades ya probadas.
2. CrewAI, si está instalado y hay clave, redacta la siguiente propuesta.
3. LangChain, en el mismo caso, investiga la limitación antes de redactar.

Este archivo no entra al servidor de Render y no cambia las apuestas solo.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

API = os.environ.get("TALLER_API", "https://mlb-1-en7i.onrender.com")
SALIDA = Path(__file__).resolve().parent / "salida"
MODELO = os.environ.get("TALLER_MODEL", "llama-3.3-70b-versatile")


def leer_reporte() -> dict:
    url = API.rstrip("/") + "/api/mente-skills"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("habilidad"):
            return data
    except Exception as exc:
        return {
            "ok": False,
            "habilidad": "Humedad alta",
            "motivo": "No conecté con Render (" + str(exc)[:120] + ").",
            "prueba": "Se queda la última habilidad que ya pasó la prueba.",
            "limitaciones": [],
        }
    return {
        "ok": False,
        "habilidad": "Humedad alta",
        "motivo": "Render respondió sin reporte.",
        "prueba": "",
        "limitaciones": [],
    }


def _texto_limitaciones(reporte: dict) -> str:
    lineas = []
    for item in reporte.get("limitaciones") or []:
        if isinstance(item, dict) and item.get("texto"):
            lineas.append(str(item["texto"]))
    if lineas:
        return " ".join(lineas[:4])
    return str(reporte.get("motivo") or "Sin limitación nueva esta noche.")


def investigar_local(reporte: dict) -> str:
    return (
        "Investigación local: "
        + _texto_limitaciones(reporte)
        + " La humedad alta ya está medida en el historial. "
        "Una habilidad nueva solo sirve si otro grupo de partidos también viene por debajo del 45%."
    )


def investigar_langchain(reporte: dict) -> str | None:
    clave = os.environ.get("GROQ_API_KEY", "").strip()
    if not clave:
        return None
    try:
        from langchain_core.messages import HumanMessage
        from langchain_groq import ChatGroq
    except Exception:
        return None
    try:
        chat = ChatGroq(model=MODELO, temperature=0.2, api_key=clave)
        preg = (
            "En dos frases, en español sencillo, dime qué dato de béisbol "
            "conviene mirar después de esta limitación. Sin código. "
            + _texto_limitaciones(reporte)
        )
        out = chat.invoke([HumanMessage(content=preg)])
        texto = getattr(out, "content", "") or str(out)
        return "LangChain: " + str(texto).strip()[:700]
    except Exception as exc:
        return "LangChain no pudo consultar (" + str(exc)[:140] + ")."


def proponer_crew(reporte: dict, investigacion: str) -> str | None:
    clave = os.environ.get("GROQ_API_KEY", "").strip()
    if not clave:
        return None
    try:
        from crewai import Agent, Crew, LLM, Process, Task
    except Exception:
        return None
    try:
        llm = LLM(model="groq/" + MODELO, temperature=0.2, api_key=clave)
        investigador = Agent(
            role="Investigador de béisbol",
            goal="Explicar en español qué limitación vale la pena convertir en regla",
            backstory="Lees fallos de picks y no inventas porcentajes.",
            llm=llm,
            verbose=False,
        )
        programador = Agent(
            role="Programador de habilidades",
            goal="Proponer una sola regla corta, sin tocar las apuestas todavía",
            backstory="Solo propones. La regla entra si el historial del grupo viene perdiendo.",
            llm=llm,
            verbose=False,
        )
        t1 = Task(
            description=(
                "Limitación: " + _texto_limitaciones(reporte)
                + "\nInvestigación previa: " + investigacion
                + "\nResponde en español, en dos frases."
            ),
            expected_output="Dos frases en español.",
            agent=investigador,
        )
        t2 = Task(
            description=(
                "Con eso, propone UNA regla para la mente. "
                "Di en qué grupo de partidos se probaría y que no se activa sola."
            ),
            expected_output="Una propuesta corta en español.",
            agent=programador,
        )
        crew = Crew(agents=[investigador, programador], tasks=[t1, t2], process=Process.sequential, verbose=False)
        result = crew.kickoff()
        return "CrewAI: " + str(result).strip()[:900]
    except Exception as exc:
        return "CrewAI no pudo redactar (" + str(exc)[:140] + ")."


def proponer_local(reporte: dict) -> str:
    return (
        "Propuesta local: mantener activa «"
        + str(reporte.get("habilidad") or "Humedad alta")
        + "». No hay una regla nueva que haya pasado la prueba del historial esta noche."
    )


def armar_resumen(reporte: dict, investigacion: str, propuesta: str) -> str:
    return "\n".join(
        [
            "Taller de noche",
            "",
            "1. La mente que apuesta",
            "Habilidad activa: " + str(reporte.get("habilidad") or "Humedad alta"),
            "Motivo: " + str(reporte.get("motivo") or ""),
            "Prueba: " + str(reporte.get("prueba") or ""),
            "",
            "2. LangChain investiga",
            investigacion,
            "",
            "3. CrewAI propone",
            propuesta,
            "",
            "Esto no cambia las apuestas. Entra en el servidor solo si el historial del grupo viene perdiendo y se copia a skills/.",
            "",
        ]
    )


def correr() -> Path:
    reporte = leer_reporte()
    investigacion = investigar_langchain(reporte) or investigar_local(reporte)
    propuesta = proponer_crew(reporte, investigacion) or proponer_local(reporte)
    texto = armar_resumen(reporte, investigacion, propuesta)
    SALIDA.mkdir(parents=True, exist_ok=True)
    destino = SALIDA / "resumen.txt"
    destino.write_text(texto, encoding="utf-8")
    return destino


if __name__ == "__main__":
    path = correr()
    print(path.read_text(encoding="utf-8"))
    print("Guardado en", path)
