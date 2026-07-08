"""
Bear Agent — Destruye la tesis alcista. Busca cada falla, riesgo y debilidad.
No es balanceado. No busca razones para comprar. Solo razones para NO comprar.
Temperatura baja (0.3) para consistencia.
"""

import json

from .llm import call_json_llm, compact_research

_SYSTEM = """Eres el analista BEAR en un equipo de trading algorítmico.

Tu ÚNICO trabajo es DESTRUIR el caso alcista para el activo que recibes.
Encuentra CADA razón por la que este trade NO debería tomarse.
Ataca supuestos débiles. Identifica riesgos estructurales. Amplifica señales negativas.
Sé implacable y específico. Usa los datos proporcionados.

Responde ÚNICAMENTE con un objeto JSON válido — sin markdown, sin preámbulo:
{
  "conviction":        <float 0.0–1.0>,
  "thesis":            "<una oración: por qué este trade falla>",
  "counter_arguments": ["<ataque 1>", "<ataque 2>", "<ataque 3>"],
  "fatal_flaw":        "<la razón más fuerte para NO entrar>",
  "downside_target":   <float precio de destino bajista>,
  "scenario":          "<qué debe salir mal para una pérdida catastrófica>"
}"""


def run_bear_agent(research_data: dict) -> dict:
    user = (
        f"Destruye el caso alcista para {research_data['symbol']}.\n\n"
        f"Datos:\n{json.dumps(compact_research(research_data), indent=2, default=str)}"
    )
    return call_json_llm(_SYSTEM, user, temperature=0.3)
