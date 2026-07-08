"""
Screen Agent — Pre-filtro LOCAL antes de invocar la IA. Sin LLM.

Objetivo: conservar la cuota de Groq (1,000 solicitudes/día, TPM) usando solo los
datos que ya calcula research (precio, SMAs, RSI, volumen). Descarta candidatos que
no tienen un setup plausible de long y ordena el resto por atractivo, para que las
3 llamadas LLM por símbolo se gasten solo en los mejores.

Es SOLO triage: la decisión real sigue en bull/bear/synthesis.
"""


def local_screen(research: dict) -> dict:
    """Devuelve {passes, score, reasons}. passes=False → no vale la pena llamar a la IA."""
    p = research.get("price", {})
    v = research.get("volume", {})
    current = p.get("current")
    sma20   = p.get("sma20")
    rsi     = p.get("rsi14")
    reasons = []

    if current is None or rsi is None or not sma20:
        return {"passes": False, "score": 0.0, "reasons": ["datos insuficientes"]}

    # Hard-drop: sobrecompra extrema (mal punto de entrada long)
    if rsi > 80:
        return {"passes": False, "score": 0.0, "reasons": [f"RSI {rsi} > 80 (sobrecompra extrema)"]}

    score = 0.0

    # Tendencia intacta (precio sobre SMA50)
    if p.get("above_sma50"):
        score += 1.0
        reasons.append("tendencia alcista (>SMA50)")

    # Zona de RSI
    if 30 <= rsi <= 55:
        score += 1.5
        reasons.append(f"RSI {rsi} en zona de compra")
    elif 25 <= rsi < 30:
        score += 0.5
        reasons.append(f"RSI {rsi} sobrevendido")
    elif rsi > 70:
        score -= 1.0
        reasons.append(f"RSI {rsi} caro")

    # Pullback a soporte (cerca de SMA20, banda ±4%)
    dist = abs(current - sma20) / sma20
    if dist <= 0.04:
        score += 1.0
        reasons.append("cerca de SMA20 (pullback)")

    # Volumen inusual (posible catalizador)
    if v.get("unusual"):
        score += 1.0
        reasons.append(f"volumen inusual ({v.get('ratio')}x)")

    # Momentum mensual positivo pero no sobreextendido
    ch1m = p.get("change_1m_pct", 0.0) or 0.0
    if 0 < ch1m <= 25:
        score += 0.5
        reasons.append(f"momentum 1m +{ch1m}%")
    elif ch1m > 60:
        score -= 0.5
        reasons.append(f"sobreextendido 1m +{ch1m}%")

    return {"passes": True, "score": round(score, 2), "reasons": reasons}
