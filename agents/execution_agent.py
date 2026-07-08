"""
Execution Agent — Ejecución de órdenes. SIN LLM. Completamente determinístico.
Solo actúa si risk_agent aprobó. Llama a Alpaca API directamente.
En paper mode, las órdenes son simuladas (sin dinero real).
"""

import os
from datetime import datetime


def _api():
    """Cliente REST de Alpaca a partir del entorno, o None si faltan credenciales."""
    api_key    = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    base_url   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    if not api_key or not secret_key:
        return None
    import alpaca_trade_api as tradeapi
    return tradeapi.REST(api_key, secret_key, base_url)


def close_position(symbol: str) -> dict:
    """Cierra a mercado toda la posición del símbolo. SIN LLM."""
    api = _api()
    if api is None:
        return {"closed": False, "reason": "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY"}
    try:
        # Cancela órdenes protectoras abiertas del símbolo para liberar los shares
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                try:
                    api.cancel_order(o.id)
                except Exception:
                    pass
        order = api.close_position(symbol)
        return {
            "closed":    True,
            "order_id":  str(getattr(order, "id", "")),
            "symbol":    symbol,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"closed": False, "reason": str(e), "timestamp": datetime.now().isoformat()}


def close_position_extended(symbol: str, limit_price: float) -> dict:
    """Cierra en extended hours: sell LIMIT + extended_hours=true (Alpaca no permite market
    ni dispara stops fuera de hora). Cancela órdenes abiertas del símbolo primero. SIN LLM."""
    api = _api()
    if api is None:
        return {"closed": False, "reason": "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY"}
    if not limit_price or float(limit_price) <= 0:
        return {"closed": False, "reason": f"limit_price inválido: {limit_price}"}
    try:
        pos = api.get_position(symbol)
        qty = abs(int(float(pos.qty)))
        if qty <= 0:
            return {"closed": False, "reason": "sin posición"}

        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                try:
                    api.cancel_order(o.id)
                except Exception:
                    pass

        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="limit",
            limit_price=round(float(limit_price), 2),
            time_in_force="day",
            extended_hours=True,
        )
        return {
            "closed":      True,
            "order_id":    str(order.id),
            "symbol":      symbol,
            "qty":         qty,
            "limit_price": round(float(limit_price), 2),
            "extended_hours": True,
            "timestamp":   datetime.now().isoformat(),
        }
    except Exception as e:
        return {"closed": False, "reason": str(e), "timestamp": datetime.now().isoformat()}


def ensure_protective_stop(symbol: str, stop_price: float, qty) -> dict:
    """Coloca un stop-loss GTC si el símbolo no tiene ya una orden stop abierta. Idempotente."""
    api = _api()
    if api is None:
        return {"armed": False, "reason": "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY"}
    if not stop_price or float(stop_price) <= 0:
        return {"armed": False, "reason": f"stop_price inválido: {stop_price}"}
    try:
        qty = abs(int(float(qty)))
        if qty <= 0:
            return {"armed": False, "reason": "qty inválida"}

        # ¿Ya hay una orden stop abierta para el símbolo? → no duplicar
        for o in api.list_orders(status="open"):
            if o.symbol == symbol and "stop" in (o.order_type or o.type or ""):
                return {"armed": False, "reason": "ya tiene stop activo", "order_id": str(o.id)}

        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="stop",
            stop_price=round(float(stop_price), 2),
            time_in_force="gtc",
        )
        return {
            "armed":      True,
            "order_id":   str(order.id),
            "stop_price": round(float(stop_price), 2),
            "qty":        qty,
        }
    except Exception as e:
        return {"armed": False, "reason": str(e)}


def _has_exposure(api, symbol: str) -> bool:
    """True si ya existe una posición abierta o una orden pendiente para el símbolo."""
    try:
        for pos in api.list_positions():
            if pos.symbol == symbol:
                return True
        for order in api.list_orders(status="open"):
            if order.symbol == symbol:
                return True
    except Exception:
        # Ante duda al consultar, no bloqueamos aquí; el orquestador ya filtra duplicados.
        return False
    return False


def execute_trade(synthesis: dict, risk_approval: dict, symbol: str) -> dict:
    """
    Traduce la decisión aprobada en una orden real (o simulada en paper mode).
    Nunca usa LLM. Nunca ejecuta sin aprobación del risk_agent.
    """
    # Guardrail primario: no ejecutar sin aprobación
    if not risk_approval.get("approved"):
        return {
            "executed": False,
            "reason":   risk_approval.get("reason") or str(risk_approval.get("rules_failed", "Risk rejected")),
        }

    decision = risk_approval.get("final_decision", "HOLD")
    if decision == "HOLD":
        return {"executed": False, "reason": "Decision is HOLD"}

    api_key    = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    base_url   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    is_paper   = "paper-api" in base_url

    if not api_key or not secret_key:
        return {"executed": False, "reason": "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY"}

    try:
        import alpaca_trade_api as tradeapi
        api = tradeapi.REST(api_key, secret_key, base_url)

        # Guardrail anti-duplicado: no abrir otra orden si ya hay exposición al símbolo
        if _has_exposure(api, symbol):
            return {"executed": False, "reason": f"{symbol} ya en cartera o con orden pendiente"}

        account      = api.get_account()
        equity       = float(account.equity)
        size_pct     = synthesis.get("position_size_pct", 0.05)
        entry_price  = synthesis.get("entry", 0.0)
        take_profit  = synthesis.get("take_profit", 0.0)
        stop_loss    = synthesis.get("stop_loss", 0.0)

        if entry_price <= 0:
            return {"executed": False, "reason": f"Precio de entrada inválido: {entry_price}"}

        position_value = equity * size_pct
        shares         = max(1, int(position_value / entry_price))
        side           = "buy" if decision == "BUY" else "sell"

        # Bracket order: coloca la entrada junto con TP (limit) y SL (stop) reales.
        # Requiere SL/TP válidos; el risk_agent ya garantiza SL > 0 y R/R ≥ 1.5.
        if side == "buy" and take_profit > 0 and stop_loss > 0:
            order = api.submit_order(
                symbol=symbol,
                qty=shares,
                side=side,
                type="market",
                time_in_force="day",
                order_class="bracket",
                take_profit={"limit_price": round(float(take_profit), 2)},
                stop_loss={"stop_price": round(float(stop_loss), 2)},
            )
            order_class = "bracket"
        else:
            # Fallback (p. ej. SELL): orden de mercado simple
            order = api.submit_order(
                symbol=symbol,
                qty=shares,
                side=side,
                type="market",
                time_in_force="day",
            )
            order_class = "simple"

        return {
            "executed":        True,
            "order_id":        str(order.id),
            "symbol":          symbol,
            "side":            side,
            "shares":          shares,
            "order_class":     order_class,
            "estimated_value": round(shares * entry_price, 2),
            "take_profit":     take_profit,
            "stop_loss":       stop_loss,
            "paper_mode":      is_paper,
            "timestamp":       datetime.now().isoformat(),
        }

    except Exception as e:
        return {
            "executed":  False,
            "reason":    str(e),
            "timestamp": datetime.now().isoformat(),
        }
