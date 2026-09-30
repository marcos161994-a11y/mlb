#!/usr/bin/env python3
"""Rechaza un dump vivo que perdería fechas del backup anterior.

Una fecha cuenta cuando el día trae predicciones o apuestas. El JSON en
vivo tiene que conservar todas las fechas que ya están en backup/memoria.
"""

from __future__ import annotations

import json
import pathlib
import sys


def fechas(path: str) -> set[str]:
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    out: set[str] = set()
    for dia in data.get("dias") or []:
        if not isinstance(dia, dict):
            continue
        fecha = str(dia.get("fecha") or "")
        if fecha and ((dia.get("predicciones") or []) or (dia.get("apuestas") or [])):
            out.add(fecha)
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("uso: validar_dump_memoria.py PREV_JSON LIVE_JSON", file=sys.stderr)
        return 2
    prev = fechas(argv[1])
    live = fechas(argv[2])
    lost = prev - live
    if lost:
        print("::error::El dump perdería fechas", ", ".join(sorted(lost)))
        return 1
    print(f"Dump OK: {len(prev)} fechas previas, {len(live)} en vivo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
