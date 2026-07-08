"""
Research Agent — Datos de mercado. Sin LLM.
Calcula indicadores técnicos y obtiene noticias recientes.
Output: dict estructurado que se pasa a bull y bear agents.

Fuente de precios: Alpaca Market Data (bars diarios, feed IEX). Se eligió sobre
yfinance porque Yahoo throttlea (timeouts) al analizar muchos símbolos en batch;
Alpaca usa las credenciales que ya tenemos y es estable.
"""

import os
from datetime import datetime, timedelta

import requests


def _rsi(closes, period: int = 14) -> float:
    delta = closes.diff()
    gain  = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs    = gain / loss
    return round(float((100 - 100 / (1 + rs)).iloc[-1]), 2)


def _alpaca_api():
    import alpaca_trade_api as tradeapi
    return tradeapi.REST(
        os.getenv("ALPACA_API_KEY"),
        os.getenv("ALPACA_SECRET_KEY"),
        os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
    )


def _daily_bars(symbol: str):
    """Velas diarias (~180 días naturales) desde Alpaca, feed IEX (plan gratuito)."""
    api   = _alpaca_api()
    start = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    return api.get_bars(symbol, "1Day", start=start, feed="iex").df


def _alpaca_news(symbol: str) -> list:
    """Noticias recientes del símbolo via Alpaca Data API."""
    headers = {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }
    try:
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/news",
            headers=headers,
            params={"symbols": symbol, "limit": 5},
            timeout=8,
        )
        r.raise_for_status()
        return [
            {"headline": n["headline"], "summary": n.get("summary", "")[:200]}
            for n in r.json().get("news", [])
        ]
    except Exception as e:
        return [{"headline": f"News unavailable ({e})", "summary": ""}]


def gather_research(symbol: str) -> dict:
    """
    Recopila datos de precio, volumen, indicadores técnicos y noticias.
    No usa LLM. Solo datos y cálculos determinísticos.
    """
    hist = _daily_bars(symbol)

    if hist is None or hist.empty or len(hist) < 30:
        n = 0 if hist is None else len(hist)
        raise ValueError(f"Datos insuficientes para {symbol} ({n} velas). ¿Símbolo correcto?")

    closes  = hist["close"]
    current = round(float(closes.iloc[-1]), 2)
    sma20   = round(float(closes.rolling(20).mean().iloc[-1]), 2)
    sma50   = round(float(closes.rolling(50).mean().iloc[-1]), 2)
    rsi14   = _rsi(closes)

    avg_vol = float(hist["volume"].rolling(20).mean().iloc[-1])
    cur_vol = float(hist["volume"].iloc[-1])

    # Cambios de precio en múltiples horizontes
    def pct_change(back: int) -> float:
        if len(closes) < back + 1:
            return 0.0
        return round((current - float(closes.iloc[-(back + 1)])) / float(closes.iloc[-(back + 1)]) * 100, 2)

    return {
        "symbol":    symbol,
        "timestamp": datetime.now().isoformat(),
        "price": {
            "current":       current,
            "sma20":         sma20,
            "sma50":         sma50,
            "rsi14":         rsi14,
            "above_sma20":   current > sma20,
            "above_sma50":   current > sma50,
            "change_1d_pct": pct_change(1),
            "change_5d_pct": pct_change(5),
            "change_1m_pct": pct_change(21),
        },
        "volume": {
            "current":  int(cur_vol),
            "avg_20d":  int(avg_vol),
            "ratio":    round(cur_vol / avg_vol, 2) if avg_vol > 0 else 1.0,
            "unusual":  (cur_vol / avg_vol) > 1.5 if avg_vol > 0 else False,
        },
        "news": _alpaca_news(symbol),
    }
