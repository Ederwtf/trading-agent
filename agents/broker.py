"""
Broker — Única frontera con el SDK de Alpaca (alpaca-py). SIN LLM.

Todo el resto del proyecto habla con el broker a través de estas funciones, que
devuelven tipos planos (dicts / floats / sets) u objetos de orden. Centralizar el SDK
aquí (en vez de crear clientes REST sueltos en cada agente) permite:
  - migrar de SDK sin tocar la lógica de los agentes (A4: alpaca-trade-api → alpaca-py),
  - a futuro, enchufar un segundo broker (p. ej. OANDA para forex) detrás de la misma API.

Modo PAPER por defecto: se deriva de ALPACA_BASE_URL (si contiene "paper"). Los clientes
se cachean por proceso. Las LECTURAS degradan a None/{}/0.0 ante error (el llamador ya
tolera eso); las ESCRITURAS propagan la excepción (el llamador la captura y la registra).
"""

import os
from datetime import datetime, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOrdersRequest, GetCalendarRequest,
    MarketOrderRequest, LimitOrderRequest, StopOrderRequest,
    TakeProfitRequest, StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

_trading: TradingClient = None
_data: StockHistoricalDataClient = None
_FEED = DataFeed.IEX          # plan gratuito


def _s(x) -> str:
    """Enum → su valor string ('stop', 'limit', 'pending_cancel'…); str(x) de reserva."""
    return getattr(x, "value", None) or str(x)


def is_paper() -> bool:
    return "paper" in os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


def trading():
    """Cliente de trading (cacheado), o None si faltan credenciales."""
    global _trading
    if _trading is not None:
        return _trading
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        return None
    try:
        _trading = TradingClient(key, sec, paper=is_paper())
        return _trading
    except Exception as e:
        print(f"  [broker] No se pudo crear TradingClient: {e}")
        return None


def data():
    """Cliente de datos de mercado (cacheado), o None si faltan credenciales."""
    global _data
    if _data is not None:
        return _data
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        return None
    try:
        _data = StockHistoricalDataClient(key, sec)
        return _data
    except Exception as e:
        print(f"  [broker] No se pudo crear StockHistoricalDataClient: {e}")
        return None


# ─────────────────────────── Lecturas ───────────────────────────
def is_reachable() -> bool:
    """True si se puede leer la cuenta (deja al agente 'ver'). Base del chequeo crítico M5."""
    tc = trading()
    if tc is None:
        return False
    try:
        tc.get_account()
        return True
    except Exception as e:
        print(f"  [broker] get_account falló: {e}")
        return False


def account() -> dict:
    """Equity/cash/buying_power o {} si no se pudo leer."""
    tc = trading()
    if tc is None:
        return {}
    try:
        a = tc.get_account()
        return {"equity": float(a.equity), "cash": float(a.cash),
                "buying_power": float(a.buying_power)}
    except Exception as e:
        print(f"  [broker] Error al leer cuenta: {e}")
        return {}


def account_equity() -> float:
    return account().get("equity", 0.0)


def held_symbols() -> set:
    tc = trading()
    if tc is None:
        return set()
    try:
        return {p.symbol for p in tc.get_all_positions()}
    except Exception as e:
        print(f"  [broker] No se pudieron leer posiciones: {e}")
        return set()


def pending_order_symbols() -> set:
    tc = trading()
    if tc is None:
        return set()
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        return {o.symbol for o in tc.get_orders(filter=req)}
    except Exception as e:
        print(f"  [broker] No se pudieron leer órdenes abiertas: {e}")
        return set()


def position(symbol: str) -> dict:
    """Posición abierta real como dict, o {} si no hay."""
    tc = trading()
    if tc is None:
        return {}
    try:
        p = tc.get_open_position(symbol)
        return {
            "qty":             float(p.qty),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price":   float(p.current_price),
            "unrealized_pl":   float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc),
        }
    except Exception:
        return {}   # sin posición


def position_qty(symbol: str) -> int:
    p = position(symbol)
    return abs(int(float(p["qty"]))) if p else 0


def latest_price(symbol: str) -> float:
    """Último precio negociado (feed IEX), o 0.0 si no disponible."""
    dc = data()
    if dc is None:
        return 0.0
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=_FEED)
        return float(dc.get_stock_latest_trade(req)[symbol].price)
    except Exception:
        return 0.0


def asset_info(symbol: str) -> dict:
    """{tradable, name} del activo, o {} si no se pudo leer."""
    tc = trading()
    if tc is None:
        return {}
    try:
        a = tc.get_asset(symbol)
        return {"tradable": bool(getattr(a, "tradable", True)),
                "name": getattr(a, "name", "") or ""}
    except Exception:
        return {}


def daily_bars(symbol: str, lookback_days: int = 180):
    """Velas diarias (feed IEX) como DataFrame indexado por timestamp, o None.

    Se descarta el nivel 'symbol' del MultiIndex para que el DataFrame se comporte igual
    que el del SDK viejo (los agentes usan hist['close']/hist['volume'] posicionalmente).
    """
    dc = data()
    if dc is None:
        return None
    try:
        start = datetime.now() - timedelta(days=lookback_days)
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=start, feed=_FEED)
        df = dc.get_stock_bars(req).df
        if df is None or df.empty:
            return df
        if "symbol" in (df.index.names or []):
            df = df.reset_index(level="symbol", drop=True)
        return df
    except Exception as e:
        raise RuntimeError(f"bars no disponibles para {symbol}: {e}")


def clock() -> dict:
    """{is_open, timestamp(ET tz-aware), next_open} o {} si no disponible."""
    tc = trading()
    if tc is None:
        return {}
    try:
        c = tc.get_clock()
        return {"is_open": bool(c.is_open), "timestamp": c.timestamp, "next_open": c.next_open}
    except Exception:
        return {}


def market_session_info() -> dict:
    """Todo lo que necesitan detect_session / in_no_trade_window en un solo set de llamadas:
    {is_open, now_et, open_time, close_time} o {} si no disponible.
    open_time/close_time son datetime.time (del calendario del día en ET)."""
    tc = trading()
    if tc is None:
        return {}
    try:
        c = tc.get_clock()
        now_et = c.timestamp
        info = {"is_open": bool(c.is_open), "now_et": now_et,
                "open_time": None, "close_time": None}
        cal = tc.get_calendar(GetCalendarRequest(start=now_et.date(), end=now_et.date()))
        if cal:
            o, cl = cal[0].open, cal[0].close
            info["open_time"]  = o.time()  if hasattr(o, "time")  else o
            info["close_time"] = cl.time() if hasattr(cl, "time") else cl
        return info
    except Exception as e:
        print(f"  [broker] market_session_info falló: {e}")
        return {}


def open_orders(symbol: str) -> list:
    """Órdenes abiertas del símbolo, aplanando piernas de OCO/bracket. Cada elemento:
    {id, order_type, stop_price, limit_price, status} (tipos/estado ya como string)."""
    tc = trading()
    if tc is None:
        return []
    out = []
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
        for o in tc.get_orders(filter=req):
            if o.symbol != symbol:
                continue
            for leg in [o] + list(o.legs or []):
                out.append({
                    "id":          str(leg.id),
                    "order_type":  _s(leg.order_type),
                    "stop_price":  float(leg.stop_price) if leg.stop_price else None,
                    "limit_price": float(leg.limit_price) if leg.limit_price else None,
                    "status":      _s(leg.status),
                })
    except Exception as e:
        print(f"  [broker] open_orders({symbol}) falló: {e}")
    return out


# ─────────────────────────── Escrituras (propagan excepción) ───────────────────────────
def cancel_orders_for(symbol: str) -> None:
    """Cancela todas las órdenes abiertas del símbolo (cancelar el padre OCO/bracket
    cancela las piernas en cascada). Best-effort por orden."""
    tc = trading()
    if tc is None:
        return
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    for o in tc.get_orders(filter=req):
        if o.symbol == symbol:
            try:
                tc.cancel_order_by_id(o.id)
            except Exception:
                pass


def close_position_market(symbol: str):
    """Cierra a mercado toda la posición del símbolo."""
    return trading().close_position(symbol)


def submit_bracket_buy(symbol: str, qty: int, take_profit: float, stop_loss: float):
    """Compra a mercado con piernas reales TP (limit) + SL (stop), tif=day."""
    req = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(float(take_profit), 2)),
        stop_loss=StopLossRequest(stop_price=round(float(stop_loss), 2)),
    )
    return trading().submit_order(order_data=req)


def submit_market(symbol: str, qty: int, side: str):
    """Orden de mercado simple (side='buy'|'sell'), tif=day."""
    req = MarketOrderRequest(
        symbol=symbol, qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    return trading().submit_order(order_data=req)


def submit_limit_extended(symbol: str, qty: int, side: str, limit_price: float):
    """Sell/buy LIMIT con extended_hours=True (para cerrar en pre/post), tif=day."""
    req = LimitOrderRequest(
        symbol=symbol, qty=qty,
        side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
        type="limit", time_in_force=TimeInForce.DAY,
        limit_price=round(float(limit_price), 2), extended_hours=True,
    )
    return trading().submit_order(order_data=req)


def submit_stop_gtc(symbol: str, qty: int, stop_price: float):
    """Sell STOP GTC (protección simple sin TP)."""
    req = StopOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC, stop_price=round(float(stop_price), 2),
    )
    return trading().submit_order(order_data=req)


def submit_oco_gtc(symbol: str, qty: int, take_profit: float, stop_loss: float):
    """Pareja OCO GTC: TP (limit) + SL (stop), una cancela a la otra.

    Idiom de alpaca-py: el limit_price de nivel superior ES la pierna de take-profit; la de
    stop-loss va en stop_loss. (No se duplica con take_profit para no mandar el TP dos veces.)
    """
    req = LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OCO, limit_price=round(float(take_profit), 2),
        stop_loss=StopLossRequest(stop_price=round(float(stop_loss), 2)),
    )
    return trading().submit_order(order_data=req)
