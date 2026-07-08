"""
Risk Agent — Validación de reglas. SIN LLM. Completamente determinístico.
Este agente NUNCA usa IA. Las reglas son fijas y no se negocian.
Si risk_agent rechaza, execution_agent no actúa. Punto final.
"""


def validate_trade(synthesis: dict, portfolio: dict) -> dict:
    """
    Valida que el trade propuesto cumpla todas las reglas de riesgo.
    Retorna approved=True solo si TODAS las reglas pasan.
    """
    passed = []
    failed = []

    equity      = portfolio.get("equity", 100_000)
    decision    = synthesis.get("decision", "HOLD")
    confidence  = synthesis.get("confidence", 0.0)
    entry       = synthesis.get("entry", 0.0)
    tp          = synthesis.get("take_profit", 0.0)
    sl          = synthesis.get("stop_loss", 0.0)
    size_pct    = synthesis.get("position_size_pct", 0.0)

    # HOLD no requiere validación
    if decision == "HOLD":
        return {
            "approved":       False,
            "reason":         "Decision is HOLD",
            "rules_passed":   [],
            "rules_failed":   [],
            "final_decision": "HOLD",
            "position_value": 0.0,
        }

    # Regla 1: Umbral de confianza mínima
    if confidence >= 0.60:
        passed.append(f"Confianza {confidence:.2f} ≥ 0.60")
    else:
        failed.append(f"Confianza {confidence:.2f} < 0.60 — insuficiente")

    # Regla 2: Stop loss obligatorio
    if sl > 0:
        passed.append("Stop loss definido")
    else:
        failed.append("Stop loss ausente — obligatorio")

    # Regla 3: R/R mínimo 1.5:1
    if entry > 0 and sl > 0 and tp > 0:
        if decision == "BUY":
            reward = tp - entry
            risk   = entry - sl
        else:
            reward = entry - tp
            risk   = sl - entry

        if risk > 0:
            rr = reward / risk
            if rr >= 1.5:
                passed.append(f"R/R {rr:.2f} ≥ 1.5")
            else:
                failed.append(f"R/R {rr:.2f} < 1.5 mínimo")
        else:
            failed.append("SL inválido: riesgo ≤ 0")
    else:
        failed.append("Faltan precios de entrada, TP o SL")

    # Regla 4: Tamaño máximo de posición 10%
    position_value = equity * size_pct
    max_position   = equity * 0.10
    if position_value <= max_position:
        passed.append(f"Posición ${position_value:,.0f} ≤ máximo ${max_position:,.0f}")
    else:
        failed.append(f"Posición ${position_value:,.0f} excede 10% (${max_position:,.0f})")

    # Regla 5: Capital mínimo operativo
    if equity * size_pct >= 100:
        passed.append(f"Capital suficiente (${equity * size_pct:,.0f})")
    else:
        failed.append(f"Posición demasiado pequeña (<$100)")

    approved = len(failed) == 0

    return {
        "approved":       approved,
        "rules_passed":   passed,
        "rules_failed":   failed,
        "final_decision": decision if approved else "HOLD",
        "position_value": round(position_value, 2),
    }
