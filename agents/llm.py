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
from groq import RateLimitError, BadRequestError

_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

# Elimina bloques de razonamiento <think>…</think> (por si se usa un modelo razonador).
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# gpt-oss y qwen3 emiten un canal de razonamiento; con response_format=json_object ese
# razonamiento consume el presupuesto de tokens y puede TRUNCAR el JSON → Groq responde
# BadRequestError "Failed to generate JSON" (causa real de los fallos del workflow tras C1).
# reasoning_effort="low" recorta ese razonamiento y estabiliza el JSON. Se aplica solo si el
# modelo lo soporta (un modelo sin razonamiento rechazaría el parámetro).
_SUPPORTS_REASONING = any(k in _MODEL for k in ("gpt-oss", "qwen3"))

# Pacing proactivo entre llamadas: suaviza ráfagas para no reventar el TPM (8K en el free
# tier de gpt-oss). Configurable; súbelo si aparecen 429 en corridas con muchos símbolos.
_MIN_INTERVAL_S = float(os.getenv("GROQ_MIN_INTERVAL_S", "1.5"))
_last_call_ts = 0.0


def _pace() -> None:
    """Espera lo necesario para respetar el intervalo mínimo entre llamadas (anti-ráfaga)."""
    global _last_call_ts
    wait = _MIN_INTERVAL_S - (time.time() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


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


def call_json_llm(system: str, user: str, temperature: float, max_tokens: int = 1200) -> dict:
    """Llama a Groq y devuelve un dict parseado.

    Groq lee GROQ_API_KEY del entorno automáticamente. Se usa response_format json_object
    (modo JSON nativo); requiere que la palabra "JSON" aparezca en el prompt — los system
    prompts de los agentes ya lo cumplen. max_tokens con holgura para que el JSON no se
    trunque tras el razonamiento del modelo.

    Reintenta hasta 3 veces:
    - RateLimitError (TPM): backoff creciente (5s, 10s).
    - BadRequestError "Failed to generate JSON" / JSON inválido: fallo estocástico de
      gpt-oss; reintentar suele resolverlo. Si agota los intentos, propaga (el orquestador
      captura por símbolo y M5 marca la corrida si TODOS fallan).
    """
    client = Groq()
    kwargs = dict(
        model=_MODEL,
        max_tokens=max_tokens,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    )
    if _SUPPORTS_REASONING:
        kwargs["reasoning_effort"] = "low"

    last_err = None
    for attempt in range(3):
        _pace()
        try:
            resp = client.chat.completions.create(**kwargs)
            return _parse_json(resp.choices[0].message.content)
        except RateLimitError as e:
            last_err = e
            time.sleep(5 * (attempt + 1))          # 5s, 10s: absorbe topes por minuto
        except (BadRequestError, json.JSONDecodeError, ValueError) as e:
            last_err = e
            time.sleep(1)                           # JSON truncado/sucio: reintento rápido
    raise last_err
