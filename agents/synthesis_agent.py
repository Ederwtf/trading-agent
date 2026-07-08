"""
Synthesis Agent — Árbitro. Recibe bull y bear, pesa argumentos y decide.
Tiene reglas de ponderación explícitas hardcodeadas en el system prompt.
Temperatura muy baja (0.2) para máxima consistencia en la decisión.
"""

import json

from .llm import call_json_llm

_SYSTEM = """Eres el ÁRBITRO en un equipo de trading algorítmico.

Recibes el análisis BULL y el análisis BEAR para un activo.
Tu trabajo es tomar la decisión final con razonamiento explícito.

REGLAS DE PONDERACIÓN (obligatorias):
1. Argumentos respaldados por datos valen 3x más que narrativa
2. Riesgos de corto plazo pesan más que riesgos estructurales en swing trades
3. Si bear_conviction > 0.72 Y bull_conviction < 0.58 → HOLD por defecto
4. Si RSI > 72 → reducir convicción de compra (sobrecomprado)
5. Si RSI < 28 → aumentar convicción de compra (sobrevendido)
6. Position size nunca supera 10% del portafolio (position_size_pct ≤ 0.10)

CÁLCULO DE TP Y SL:
- Stop Loss: precio que invalida la tesis (NO solo un porcentaje arbitrario)
- Take Profit: primer nivel de resistencia o target técnico razonable
- R/R mínimo implícito: 1.5:1

Responde ÚNICAMENTE con un objeto JSON válido — sin markdown, sin preámbulo:
{
  "decision":          "<BUY|SELL|HOLD>",
  "confidence":        <float 0.0–1.0>,
  "reasoning":         "<por qué esta decisión luego de pesar ambos lados>",
  "bull_weight":       <float 0.0–1.0, cuánto pesó el caso bull>,
  "bear_weight":       <float 0.0–1.0, cuánto pesó el caso bear>,
  "entry":             <float precio de entrada>,
  "take_profit":       <float precio objetivo>,
  "stop_loss":         <float precio de stop>,
  "position_size_pct": <float 0.01–0.10>
}"""


def run_synthesis(research_data: dict, bull: dict, bear: dict) -> dict:
    payload = {
        "symbol":        research_data["symbol"],
        "current_price": research_data["price"]["current"],
        "rsi14":         research_data["price"]["rsi14"],
        "above_sma20":   research_data["price"]["above_sma20"],
        "above_sma50":   research_data["price"]["above_sma50"],
        "change_5d":     research_data["price"]["change_5d_pct"],
        "bull_analysis": bull,
        "bear_analysis": bear,
    }

    user = (
        f"Toma la decisión final para {research_data['symbol']}.\n\n"
        f"Datos y análisis:\n{json.dumps(payload, indent=2, default=str)}"
    )
    return call_json_llm(_SYSTEM, user, temperature=0.2)
