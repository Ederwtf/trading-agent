"""
LLM helper — Abstracción del proveedor para los agentes bull, bear y synthesis.

Proveedor: Groq (gpt-oss-120b por defecto) — tier gratuito, API compatible con
OpenAI, modo JSON nativo. La llamada y el parseo de JSON viven aquí, en un solo lugar.
gpt-oss-120b: 200K tokens/día, 8K tokens/min, 1K solicitudes/día. Es el reemplazo
recomendado por Groq tras la deprecación de Llama 4 Scout (apagado 2026-07-17).
Se puede cambiar de modelo con la variable de entorno GROQ_MODEL.
"""

import json
import os
import re
import time

from groq import Groq
from groq import RateLimitError

_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Elimina bloques de razonamiento <think>…</think> (por si se usa un modelo razonador).
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def compact_research(research_data: dict) -> dict:
    """Proyección ligera de research para los prompts de bull/bear.

    Elimina los resúmenes de noticias (los mayores consumidores de tokens) y deja
    solo precio, volumen y los titulares. Reduce el consumo diario de Groq sin
    perder la señal relevante.
    """
    return {
        "symbol":         research_data.get("symbol"),
        "price":          research_data.get("price", {}),
        "volume":         research_data.get("volume", {}),
        "news_headlines": [n.get("headline", "") for n in research_data.get("news", [])[:3]],
    }


def _parse_json(text: str) -> dict:
    """Limpia razonamiento/fences y parsea el JSON del contenido del modelo."""
    text = _THINK_RE.sub("", text).strip()

    # Fallback defensivo por si viniera envuelto en fences (JSON mode normalmente lo evita)
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    return json.loads(text.strip())


def call_json_llm(system: str, user: str, temperature: float, max_tokens: int = 800) -> dict:
    """Llama a Groq y devuelve un dict parseado.

    Groq lee GROQ_API_KEY del entorno automáticamente. Se usa response_format
    json_object (modo JSON nativo); requiere que la palabra "JSON" aparezca en
    el prompt — los system prompts de los agentes ya lo cumplen.

    Ante un tope transitorio de rate-limit (TPM) reintenta UNA vez con backoff
    corto; si vuelve a fallar propaga el error (el orquestador captura por símbolo).
    """
    client = Groq()

    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=_MODEL,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            )
            return _parse_json(resp.choices[0].message.content)
        except RateLimitError:
            if attempt == 0:
                time.sleep(5)   # absorbe tope por minuto; si es tope diario, el 2º intento fallará
                continue
            raise
