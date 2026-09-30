"""El paso de publicar backup/memoria tiene que ser bash válido.

El heredoc anidado se rompía al quitar la sangría YAML: el cierre PY
quedaba indentado y bash moría con EOF. La comparación de fechas vive
en validar_dump_memoria.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / ".github" / "scripts" / "validar_dump_memoria.py"
WORKFLOW = ROOT / ".github" / "workflows" / "backup-memoria.yml"


def extraer_run(text: str, step_name: str) -> str:
    """Devuelve el shell de un `run: |` tal como lo ve bash tras el YAML."""
    lines = text.splitlines()
    start = None
    run_indent = 0
    for i, line in enumerate(lines):
        if line.strip() != f"- name: {step_name}":
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].strip() == "run: |":
                start = j + 1
                run_indent = len(lines[j]) - len(lines[j].lstrip(" "))
                break
        break
    if start is None:
        raise AssertionError(f"no está el paso {step_name}")
    block: list[str] = []
    for line in lines[start:]:
        if line.strip() == "":
            block.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= run_indent:
            break
        block.append(line)
    indents = [len(line) - len(line.lstrip(" ")) for line in block if line.strip()]
    cut = min(indents)
    body = "\n".join(line[cut:] if line.strip() else "" for line in block)
    return body + "\n"


def _dump(dias: list) -> dict:
    return {"dia_actual": 2, "capital": 10, "dias": dias}


def _dia(fecha: str, *, preds: bool = False, apuestas: bool = False) -> dict:
    return {
        "fecha": fecha,
        "predicciones": [{"game_id": "g"}] if preds else [],
        "apuestas": [{"game_id": "a"}] if apuestas else [],
    }


def _correr(tmp_path: Path, prev: dict, live: dict) -> subprocess.CompletedProcess[str]:
    prev_path = tmp_path / "prev.json"
    live_path = tmp_path / "live.json"
    prev_path.write_text(json.dumps(prev), encoding="utf-8")
    live_path.write_text(json.dumps(live), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(prev_path), str(live_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_paso_publicar_es_bash_valido(tmp_path: Path):
    script = extraer_run(WORKFLOW.read_text(encoding="utf-8"), "Publicar en la rama backup/memoria")
    assert script.count("<<'PY'") == 1
    assert "\nPY\n" in script
    assert "    PY" not in script
    assert (
        "python3 /tmp/validar_dump_memoria.py memoria_auditoria.json /tmp/memoria_live.json"
        in script
    )
    assert "cp .github/scripts/validar_dump_memoria.py /tmp/validar_dump_memoria.py" in script
    assert 'git commit -m "backup(db): dia ${DIA} capital ${CAP}"' in script
    assert "git push origin backup/memoria" in script
    assert "git push origin main" not in script
    path = tmp_path / "publicar.sh"
    path.write_text(script, encoding="utf-8")
    subprocess.run(["bash", "-n", str(path)], check=True)


def test_heredoc_de_meta_sigue_escribiendo(tmp_path: Path):
    script = extraer_run(WORKFLOW.read_text(encoding="utf-8"), "Publicar en la rama backup/memoria")
    inicio = script.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n")
    fin = script.index("\nPY\n", inicio)
    cuerpo = script[inicio:fin]
    live = tmp_path / "memoria_live.json"
    meta = tmp_path / "backup_meta.json"
    envf = tmp_path / "dia_cap.env"
    live.write_text(
        json.dumps({"dia_actual": 47, "capital": 97.66, "dias": [{}, {}]}),
        encoding="utf-8",
    )
    cuerpo = (
        cuerpo.replace('"/tmp/memoria_live.json"', repr(str(live)))
        .replace('"/tmp/backup_meta.json"', repr(str(meta)))
        .replace('"/tmp/dia_cap.env"', repr(str(envf)))
    )
    subprocess.run([sys.executable, "-c", cuerpo], check=True)
    escrito = json.loads(meta.read_text(encoding="utf-8"))
    assert escrito == {
        "dia_actual": 47,
        "capital": 97.66,
        "dias": 2,
        "fuente": "/api/exportar-memoria",
    }
    assert envf.read_text(encoding="utf-8") == "DIA=47\nCAP=97.66\n"


def test_rechaza_fechas_perdidas(tmp_path: Path):
    prev = _dump([_dia("2026-08-01", preds=True), _dia("2026-08-03", apuestas=True)])
    live = _dump([_dia("2026-08-03", preds=True), _dia("2026-08-04", apuestas=True)])
    proc = _correr(tmp_path, prev, live)
    assert proc.returncode == 1
    assert proc.stdout.strip() == "::error::El dump perdería fechas 2026-08-01"


def test_acepta_el_mismo_historial_y_fechas_nuevas(tmp_path: Path):
    prev = _dump([
        _dia("2026-08-02", preds=True),
        _dia("2026-08-01", apuestas=True),
        _dia("2026-08-09"),
        "no-es-dia",
    ])
    live = _dump([
        _dia("2026-08-01", preds=True),
        _dia("2026-08-02", apuestas=True),
        _dia("2026-08-05", preds=True),
        _dia(""),
        _dia("2026-08-10"),
    ])
    proc = _correr(tmp_path, prev, live)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "Dump OK: 2 fechas previas, 3 en vivo"
