"""Dos agentes en la PC. No sustituye a mente_mlb.py del servidor.

A las 8:00 revisa el reporte vivo. A las 23:30 hace lo mismo con el día ya jugado.
La bóveda guarda el texto. No ejecuta código nuevo y no mueve apuestas.
"""

from __future__ import annotations

from datetime import datetime

try:
    from boveda import guardar
    from noche import correr
except ImportError:
    from taller.boveda import guardar
    from taller.noche import correr


def main() -> None:
    hora = datetime.now().strftime("%H:%M")
    destino = correr()
    texto = destino.read_text(encoding="utf-8")
    bloque = f"Pasada de las {hora}\n\n{texto}"
    ruta = guardar(bloque, tipo="reporte")
    print(bloque)
    print("Bóveda:", ruta)


if __name__ == "__main__":
    main()
