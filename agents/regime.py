"""
Regime Agent — Detección del régimen de mercado. SIN LLM. Determinístico.

Responde "¿en qué tipo de mercado estamos?" para que la estrategia sea **estable pero
dinámica**: las mismas reglas de siempre, pero con el acelerador ajustado al contexto.

Señales (todas vía `broker.daily_bars`, sin dependencias nuevas — el proyecto ya descartó
yfinance por throttling, ver research.py):
- Volatilidad realizada de SPY (20d, anualizada) — cuánto se mueve el mercado AHORA.
- SPY vs SMA200 — tendencia estructural.
- Drawdown desde el máximo de 60d — ¿estamos en una corrección?
- VIXY vs su media de 60d — proxy gratuito de volatilidad implícita (sustituto de CME CVOL).

Umbrales calibrados con la distribución real de SPY del último año (2026-07-28):
vol20d min 5.6 · p25 9.9 · mediana 11.8 · p75 13.8 · max 20.7.

Política resultante (configurable en config/watchlist.json → regime.policy):
- calm    : opera normal.
- nervous : reduce el tamaño de posición y exige más confianza.
- panic   : NO abre entradas nuevas (las salidas siguen gestionándose siempre).

Degradación segura: si los datos fallan, devuelve `calm` con `degraded=True` — un fallo de
red no debe bloquear al agente (el resto de guardrails sigue vigente).
"""

import math

from . import broker

# Defaults (se pueden sobrescribir desde config/watchlist.json → regime)
_DEFAULT_THRESHOLDS = {
    "panic_vol":      20.0,   # vol20d anualizada % (cerca del máximo del último año)
    "panic_drawdown": -10.0,  # % desde el máximo de 60d
    "nervous_vol":    13.5,   # ~p75 de la distribución observada
    "nervous_drawdown": -5.0,
    "nervous_vixy_ratio": 1.35,
}

_DEFAULT_POLICY = {
    "calm":    {"allow_entries": True,  "size_pct": 0.05,  "min_confidence": 0.60},
    "nervous": {"allow_entries": True,  "size_pct": 0.035, "min_confidence": 0.70},
    "panic":   {"allow_entries": False, "size_pct": 0.0,   "min_confidence": 1.0},
}


def _annualized_vol(closes, window: int) -> float:
    """Volatilidad realizada anualizada (%) de los últimos `window` retornos diarios."""
    rets = closes.pct_change().dropna()
    if len(rets) < window:
        return 0.0
    return float(rets.iloc[-window:].std() * math.sqrt(252) * 100)


def _metrics() -> dict:
    """Señales crudas del mercado. Lanza si SPY no está disponible (el llamador degrada)."""
    spy = broker.daily_bars("SPY", 420)          # ~420 naturales ≈ 288 sesiones → SMA200 ok
    if spy is None or spy.empty:
        raise RuntimeError("sin barras de SPY")
    closes = spy["close"]
    current = float(closes.iloc[-1])

    sma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else None
    peak60 = float(closes.iloc[-60:].max()) if len(closes) >= 60 else current
    drawdown = (current - peak60) / peak60 * 100 if peak60 else 0.0

    # VIXY es opcional: si falla, el resto de señales sigue sirviendo.
    vixy_ratio = None
    try:
        v = broker.daily_bars("VIXY", 200)["close"]
        avg = float(v.rolling(60).mean().iloc[-1])
        if avg > 0:
            vixy_ratio = round(float(v.iloc[-1]) / avg, 2)
    except Exception:
        pass

    return {
        "spy_price":   round(current, 2),
        "spy_sma200":  round(sma200, 2) if sma200 else None,
        "above_sma200": (current > sma200) if sma200 else None,
        "vol_20d":     round(_annualized_vol(closes, 20), 1),
        "vol_60d":     round(_annualized_vol(closes, 60), 1),
        "drawdown_60d": round(drawdown, 2),
        "vixy_ratio":  vixy_ratio,
    }


def _classify(m: dict, th: dict) -> tuple:
    """(label, razones) según los umbrales. Prioridad: panic > nervous > calm."""
    reasons = []
    if m["vol_20d"] > th["panic_vol"]:
        reasons.append(f"vol 20d {m['vol_20d']}% > {th['panic_vol']}%")
    if m["drawdown_60d"] < th["panic_drawdown"]:
        reasons.append(f"drawdown {m['drawdown_60d']}% < {th['panic_drawdown']}%")
    if reasons:
        return "panic", reasons

    if m["vol_20d"] > th["nervous_vol"]:
        reasons.append(f"vol 20d {m['vol_20d']}% > {th['nervous_vol']}%")
    if m["above_sma200"] is False:
        reasons.append(f"SPY ${m['spy_price']} < SMA200 ${m['spy_sma200']}")
    if m["drawdown_60d"] < th["nervous_drawdown"]:
        reasons.append(f"drawdown {m['drawdown_60d']}%")
    if m["vixy_ratio"] is not None and m["vixy_ratio"] > th["nervous_vixy_ratio"]:
        reasons.append(f"VIXY {m['vixy_ratio']}x su media 60d")
    if reasons:
        return "nervous", reasons

    return "calm", [f"vol 20d {m['vol_20d']}%, SPY sobre SMA200, drawdown {m['drawdown_60d']}%"]


def detect_regime(cfg: dict = None) -> dict:
    """Régimen de mercado + política a aplicar. SIN LLM.

    cfg: bloque `regime` de config/watchlist.json (thresholds/policy opcionales).
    Devuelve {label, reasons, metrics, policy, degraded}.
    """
    cfg = cfg or {}
    th = {**_DEFAULT_THRESHOLDS, **(cfg.get("thresholds") or {})}
    policies = {k: {**v, **((cfg.get("policy") or {}).get(k) or {})}
                for k, v in _DEFAULT_POLICY.items()}

    try:
        m = _metrics()
    except Exception as e:
        # Nunca bloquear por un fallo de datos: se opera como en calma y se avisa.
        print(f"  [régimen] datos no disponibles ({e}); se asume 'calm'")
        return {"label": "calm", "reasons": [f"datos no disponibles: {e}"],
                "metrics": {}, "policy": policies["calm"], "degraded": True}

    label, reasons = _classify(m, th)
    return {"label": label, "reasons": reasons, "metrics": m,
            "policy": policies[label], "degraded": False}
