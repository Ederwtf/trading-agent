"""
Exit Agent — Gestión de salidas de posiciones abiertas.

Dos capas (patrón híbrido, para conservar cuota LLM):
- `local_exit`  : reglas LOCALES deterministas (SIN LLM). Cierra ante SL/TP/quiebre de
                   tendencia; marca "zona de revisión" cuando la señal es ambigua.
- `review_thesis`: juez LLM que relee la tesis original (bull/bear/synthesis del journal)
                   contra los datos actuales y decide si la tesis sigue viva.

El orquestador llama primero a `local_exit`; solo invoca `review_thesis` cuando
`zone == "review"`.
"""

import json

from .llm import call_json_llm


def local_exit(original: dict, research: dict, position: dict, cfg: dict = None) -> dict:
    """Reglas locales de salida. Devuelve {action: CLOSE|HOLD, reason, zone}."""
    cfg = cfg or {}
    rsi_high  = cfg.get("review_rsi_high", 72)
    sl_prox   = cfg.get("review_sl_proximity_pct", 0.02)
    # Off por defecto: la estrategia compra dips (por debajo de SMA50), así que cerrar por
    # "precio < SMA50" liquidaría las entradas al instante. El SL es el piso real.
    trend_exit = cfg.get("trend_exit_below_sma50", False)

    p = research.get("price", {})
    # Precio para las reglas duras: el de la POSICIÓN (real-time, incluye extended hours).
    # El "current" de research sale de velas diarias IEX y en pre/post es el cierre de la
    # sesión regular — evaluar SL/TP con ese precio viejo dejaba ciego al gestor fuera de
    # horario (hallazgo A1 de la auditoría). Research queda como fallback.
    live    = position.get("current_price")
    current = float(live) if live else p.get("current")
    sma50   = p.get("sma50")
    rsi     = p.get("rsi14")
    sl      = original.get("stop_loss", 0.0) or 0.0
    tp      = original.get("take_profit", 0.0) or 0.0
    plpc    = position.get("unrealized_plpc")  # fracción (0.05 = +5%)

    if current is None:
        return {"action": "HOLD", "reason": "sin precio actual", "zone": "none"}

    # ── Reglas duras (cierre inmediato) ──
    if sl > 0 and current <= sl:
        return {"action": "CLOSE", "reason": f"SL cruzado (${current} ≤ ${sl})", "zone": "hard"}
    if tp > 0 and current >= tp:
        return {"action": "CLOSE", "reason": f"TP alcanzado (${current} ≥ ${tp})", "zone": "hard"}
    if trend_exit and sma50 and current < sma50:
        return {"action": "CLOSE", "reason": f"tendencia rota (precio ${current} < SMA50 ${sma50})", "zone": "hard"}

    # ── Zona de revisión (ambiguo → activar juez LLM) ──
    reasons = []
    if sl > 0 and current <= sl * (1 + sl_prox):
        reasons.append(f"cerca del SL (≤{sl_prox*100:.0f}%)")
    if rsi is not None and (rsi >= rsi_high or rsi <= 28):
        reasons.append(f"RSI extremo ({rsi})")
    if plpc is not None and float(plpc) < -0.03:
        reasons.append(f"P/L {float(plpc)*100:.1f}%")

    if reasons:
        return {"action": "HOLD", "reason": "; ".join(reasons), "zone": "review"}

    return {"action": "HOLD", "reason": "tesis intacta", "zone": "none"}


_SYSTEM = """Eres el GESTOR DE SALIDAS de un equipo de trading algorítmico.

Recibes la TESIS ORIGINAL con la que se abrió una posición LONG (análisis bull, bear y la
decisión synthesis) y los DATOS ACTUALES de mercado más el P/L no realizado.

Tu trabajo: decidir si la tesis original SIGUE VIVA (HOLD) o si ya SE ROMPIÓ (CLOSE).
Cierra si los catalizadores originales se invalidaron, la estructura técnica se deterioró
o el riesgo ya no justifica mantener. Mantén si la tesis sigue en pie y solo hay ruido.

Responde ÚNICAMENTE con un objeto JSON válido — sin markdown, sin preámbulo:
{
  "action":     "<CLOSE|HOLD>",
  "confidence": <float 0.0–1.0>,
  "reason":     "<una oración: por qué la tesis sigue viva o se rompió>"
}"""


def review_thesis(original: dict, research: dict, position: dict,
                  bull: dict = None, bear: dict = None) -> dict:
    """Juez LLM: ¿la tesis original sigue viva? Devuelve {action, confidence, reason}."""
    from .llm import compact_research

    payload = {
        "tesis_original": {
            "synthesis": original,
            "bull": bull or {},
            "bear": bear or {},
        },
        "datos_actuales": compact_research(research),
        "posicion": {
            "entrada":       position.get("avg_entry_price"),
            "precio_actual": position.get("current_price"),
            "pl_pct":        position.get("unrealized_plpc"),
        },
    }
    user = (
        f"Evalúa si mantener o cerrar la posición en {research.get('symbol')}.\n\n"
        f"JSON:\n{json.dumps(payload, indent=2, default=str)}"
    )
    return call_json_llm(_SYSTEM, user, temperature=0.2)
