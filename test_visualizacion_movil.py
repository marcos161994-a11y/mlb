from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_panel_principal_tiene_orden_movil_y_controles_compactos():
    html = (ROOT / "QuantumMLB.html").read_text(encoding="utf-8")
    assert 'class="quick-nav"' in html
    assert 'href="#picks-hero"' in html
    assert 'id="resumen-hoy"' in html
    assert 'id="historiales"' in html
    assert 'id="juegos-hoy"' in html
    assert '<details class="operations">' in html
    assert '<details class="system-status">' in html
    assert '<details class="analisis-juego">' in html
    assert "@media (max-width: 699px)" in html
