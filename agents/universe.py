"""
Universe Agent — Construye el conjunto de símbolos a analizar. Sin LLM.

Combina la watchlist fija (curada en config/watchlist.json) con los "most-actives"
del mercado (Alpaca screener API). Deduplica preservando orden y corta en un tope.
Si el screener falla, hace fallback a solo la lista fija.

Los candidatos DINÁMICOS pasan por un filtro de calidad (los fijos se confían):
- precio mínimo (descarta penny stocks)
- excluye ETFs apalancados/inversos por nombre (p. ej. SOXS = Semiconductor Bear 3X),
  que rompen la tesis del análisis (el LLM no sabe que son inversos).
"""

import os

import requests

# Palabras clave que delatan ETFs apalancados/inversos (nombre en mayúsculas).
_LEVERAGED_KEYWORDS = ("BEAR", "BULL", "ULTRA", "INVERSE", "LEVERAG", "2X", "3X", "-1X")


def _alpaca_api():
    try:
        import alpaca_trade_api as tradeapi
        return tradeapi.REST(
            os.getenv("ALPACA_API_KEY"),
            os.getenv("ALPACA_SECRET_KEY"),
            os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        )
    except Exception:
        return None


def _alpaca_most_actives(top: int) -> list:
    """Símbolos más activos del mercado vía Alpaca Screener API."""
    headers = {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }
    try:
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives",
            headers=headers,
            params={"by": "volume", "top": top},
            timeout=8,
        )
        r.raise_for_status()
        return [row["symbol"] for row in r.json().get("most_actives", [])]
    except Exception as e:
        print(f"  [universe] Screener no disponible ({e}). Uso solo la lista fija.")
        return []


def _quality_filter(api, symbols: list, min_price: float) -> list:
    """Filtra candidatos dinámicos: precio mínimo, tradable, sin ETFs apalancados/inversos."""
    if api is None:
        return symbols  # sin API no podemos filtrar; devolvemos tal cual

    keep = []
    for sym in symbols:
        # Filtro por precio (descarta penny stocks)
        try:
            price = float(api.get_latest_trade(sym, feed="iex").price)
            if price < min_price:
                continue
        except Exception:
            pass  # sin precio, dejamos que el filtro de nombre y research decidan

        # Filtro por nombre / tradabilidad (descarta apalancados/inversos)
        try:
            asset = api.get_asset(sym)
            if not getattr(asset, "tradable", True):
                continue
            name = (getattr(asset, "name", "") or "").upper()
            if any(k in name for k in _LEVERAGED_KEYWORDS):
                continue
        except Exception:
            pass

        keep.append(sym)
    return keep


def build_universe(static_symbols: list, top: int = 15, cap: int = 20,
                   dynamic: bool = True, min_price: float = 10.0) -> list:
    """
    Devuelve la lista ordenada y deduplicada de símbolos a analizar.
    Los fijos van primero (prioridad, sin filtro); luego el relleno dinámico ya
    filtrado por calidad, hasta `cap`.
    """
    fixed = [s.upper() for s in static_symbols]

    dynamic_syms = []
    if dynamic:
        raw = [s.upper() for s in _alpaca_most_actives(top)]
        # No re-filtrar los que ya están en la lista fija
        raw = [s for s in raw if s not in set(fixed)]
        dynamic_syms = _quality_filter(_alpaca_api(), raw, min_price)

    # Dedup preservando orden (fijos primero)
    seen = set()
    universe = []
    for sym in fixed + dynamic_syms:
        if sym and sym not in seen:
            seen.add(sym)
            universe.append(sym)
        if len(universe) >= cap:
            break

    return universe
