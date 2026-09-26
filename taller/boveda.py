"""Bóveda mental: lo aprendido se queda en disco, en esta carpeta.

ChromaDB se usa solo si está instalado. Si no, el archivo memoria.jsonl
guarda igual cada reporte.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

BOVEDA = Path(__file__).resolve().parent / "boveda_mental"
MEMORIA = BOVEDA / "memoria.jsonl"


def guardar(texto: str, tipo: str = "reporte") -> Path:
    BOVEDA.mkdir(parents=True, exist_ok=True)
    nota = {
        "cuando": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tipo": tipo,
        "texto": texto,
    }
    with MEMORIA.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(nota, ensure_ascii=False) + "\n")
    _chroma(nota)
    return MEMORIA


def ultimas(n: int = 5) -> list[dict]:
    if not MEMORIA.exists():
        return []
    lineas = [ln for ln in MEMORIA.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out = []
    for ln in lineas[-n:]:
        try:
            item = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _chroma(nota: dict) -> None:
    try:
        import chromadb
    except Exception:
        return
    try:
        client = chromadb.PersistentClient(path=str(BOVEDA / "chroma"))
        col = client.get_or_create_collection("mente")
        col.add(
            ids=[nota["cuando"] + "-" + nota["tipo"]],
            documents=[nota["texto"][:2000]],
            metadatas=[{"tipo": nota["tipo"]}],
        )
    except Exception:
        return
