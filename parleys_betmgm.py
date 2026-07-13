"""
Sistema de recomendaciones de parleys (apuestas combinadas) basado en el modelo MLB.
Calcula parleys óptimos combinando picks con valor individual.
"""

from __future__ import annotations

import itertools
from typing import Any, List, Tuple
import math


def calcular_cuota_parley(decimal_odds: List[float]) -> float:
    """Calcula la cuota decimal de un parley multiplicando las cuotas individuales."""
    cuota = 1.0
    for odd in decimal_odds:
        cuota *= odd
    return round(cuota, 3)


def calcular_probabilidad_parley(probabilidades: List[float]) -> float:
    """Calcula la probabilidad conjunta del parley (asumiendo independencia)."""
    prob = 1.0
    for p in probabilidades:
        prob *= (p / 100.0)
    return round(prob * 100, 2)


def calcular_edge_parley(prob_modelo: float, cuota_parley: float) -> float:
    """Calcula el edge del parley: prob_modelo - prob_implicita."""
    if cuota_parley <= 1:
        return -999.0
    prob_implicita = 100.0 / cuota_parley
    return round(prob_modelo - prob_implicita, 2)


def decimal_a_american(decimal: float) -> int:
    """Convierte cuota decimal a americana."""
    if decimal >= 2.0:
        return int(round((decimal - 1) * 100))
    return int(round(-100 / (decimal - 1)))


def generar_parleys(
    juegos: List[dict],
    max_picks_por_parley: int = 3,
    min_edge_individual: float = 3.0,
    min_prob_modelo: float = 52.0
) -> List[dict]:
    """
    Genera parleys óptimos combinando picks con valor individual.
    
    Args:
        juegos: Lista de juegos con datos del modelo
        max_picks_por_parley: Máximo número de picks en un parley (2-3 recomendado)
        min_edge_individual: Edge mínimo individual para considerar un pick
        min_prob_modelo: Probabilidad mínima del modelo para considerar un pick
    
    Returns:
        Lista de parleys ordenados por edge descendente
    """
    # Filtrar picks con valor individual
    picks_validos = []
    for juego in juegos:
        if not juego.get("apostable"):
            continue
        
        edge = juego.get("edge", 0)
        prob = juego.get("probPick", 0)
        odds = juego.get("odds")
        
        if edge >= min_edge_individual and prob >= min_prob_modelo and odds:
            picks_validos.append({
                "juego": juego,
                "pick": juego["pick"],
                "equipo": juego.get("pick", "").replace(" ML", ""),
                "odds": odds,
                "prob": prob,
                "edge": edge,
                "game_id": juego["id"],
                "visitante": juego["visitante"],
                "home": juego["home"]
            })
    
    if len(picks_validos) < 2:
        return []
    
    parleys = []
    
    # Generar combinaciones de 2 y 3 picks
    for num_picks in range(2, min(max_picks_por_parley + 1, len(picks_validos) + 1)):
        for combinacion in itertools.combinations(picks_validos, num_picks):
            odds = [p["odds"] for p in combinacion]
            probs = [p["prob"] for p in combinacion]
            
            cuota_parley = calcular_cuota_parley(odds)
            prob_parley = calcular_probabilidad_parley(probs)
            edge_parley = calcular_edge_parley(prob_parley, cuota_parley)
            
            # Solo incluir parleys con valor positivo
            if edge_parley > 0:
                parley = {
                    "picks": list(combinacion),
                    "num_picks": num_picks,
                    "cuota_parley": cuota_parley,
                    "cuota_parley_american": decimal_a_american(cuota_parley),
                    "prob_modelo": prob_parley,
                    "prob_implicita": round(100.0 / cuota_parley, 2),
                    "edge": edge_parley,
                    "stake_recomendado": 5.0,  # Stake base, puede ajustarse según Kelly
                    "ev_esperado": round((prob_parley / 100.0) * (cuota_parley - 1) - (1 - prob_parley / 100.0), 3)
                }
                parleys.append(parley)
    
    # Ordenar por edge descendente
    parleys.sort(key=lambda x: x["edge"], reverse=True)
    
    return parleys


def seleccionar_mejor_parley(parleys: List[dict], max_risk: float = 0.05) -> dict | None:
    """
    Selecciona el mejor parley basado en edge y riesgo.
    
    Args:
        parleys: Lista de parleys generados
        max_risk: Máximo riesgo aceptable como porcentaje del bankroll
    
    Returns:
        El mejor parley o None si no hay opciones válidas
    """
    if not parleys:
        return None
    
    # Filtrar parleys con EV positivo y edge razonable
    validos = [p for p in parleys if p["ev_esperado"] > 0 and p["edge"] > 2.0]
    
    if not validos:
        return None
    
    # Seleccionar el con mayor edge, pero priorizando parleys de 2 picks sobre 3
    # (menor varianza, más consistencia)
    validos.sort(key=lambda x: (x["num_picks"], -x["edge"]))
    
    return validos[0]


def formatear_recomendacion_parley(parley: dict) -> str:
    """Genera un mensaje legible con la recomendación del parley."""
    if not parley:
        return "No hay parleys recomendados en este momento."
    
    lines = [
        "=" * 70,
        "🎯 RECOMENDACIÓN DE PARLEY BETMGM",
        "=" * 70,
        "",
        f"PARLEY DE {parley['num_picks']} PICKS:",
        f"Cuota: {parley['cuota_parley']:.2f} ({parley['cuota_parley_american']:+d})",
        f"Probabilidad Modelo: {parley['prob_modelo']:.1f}%",
        f"Probabilidad Mercado: {parley['prob_implicita']:.1f}%",
        f"EDGE: +{parley['edge']:.1f}%",
        f"EV Esperado: ${parley['ev_esperado']:.2f} por $1 apostado",
        "",
        "PICKS INCLUIDOS:",
    ]
    
    for i, pick in enumerate(parley["picks"], 1):
        lines.append(
            f"  {i}. {pick['pick']} @ {pick['odds']:.2f} "
            f"(Edge +{pick['edge']:.1f}%, Prob {pick['prob']:.0f}%)"
        )
    
    lines.extend([
        "",
        f"💰 STAKE RECOMENDADO: ${parley['stake_recomendado']:.0f}",
        "",
        "=" * 70,
    ])
    
    return "\n".join(lines)
