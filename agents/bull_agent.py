"""
Bull Agent — Construye el caso MÁS FUERTE posible para entrar LONG.
No es balanceado. No busca riesgos. Solo razones para comprar.
Temperatura baja (0.3) para consistencia.
"""

import json

from .llm import call_json_llm, compact_research

_SYSTEM = """Eres el analista BULL en un equipo de trading algorítmico.

Tu ÚNICO trabajo es construir el caso más agresivo posible para entrar LONG
en el activo que recibes. No eres balanceado. No se te pide que consideres riesgos.
Tu trabajo es encontrar CADA señal, CADA dato, CADA noticia que apoye comprar.

Sé específico con los datos proporcionados. Usa números. Sé agresivo en tu convicción.

Responde ÚNICAMENTE con un objeto JSON válido — sin markdown, sin preámbulo:
{
  "conviction":    <float 0.0–1.0>,
  "thesis":        "<una oración: el caso central para entrar long>",
  "arguments":     ["<arg 1>", "<arg 2>", "<arg 3>"],
  "key_catalyst":  "<la razón más fuerte para comprar ahora>",
  "price_target":  <float>,
  "timeframe":     "<intraday|swing_week|position>"
}"""


def run_bull_agent(research_data: dict) -> dict:
    user = (
        f"Construye el caso BULL para {research_data['symbol']}.\n\n"
        f"Datos:\n{json.dumps(compact_research(research_data), indent=2, default=str)}"
    )
    return call_json_llm(_SYSTEM, user, temperature=0.3)
