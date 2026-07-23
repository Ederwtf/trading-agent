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


def _open_orders_for(api, symbol: str) -> list:
    """Órdenes abiertas del símbolo, incluyendo piernas de órdenes multi-leg (OCO/bracket)."""
    found = []
    try:
        for o in api.list_orders(status="open", nested=True):
            if o.symbol != symbol:
                continue
            found.append(o)
            for leg in (getattr(o, "legs", None) or []):
                found.append(leg)
    except Exception:
        pass
    return found


def ensure_exit_bracket(symbol: str, stop_price: float, take_profit: float, qty) -> dict:
    """Mantiene una pareja OCO GTC (TP limit + SL stop) para la posición. SIN LLM.

    Broker-side: el TP y el SL viven en Alpaca y se ejecutan al instante en horario
    regular, sin depender de la cadencia del cron (hallazgo C2 de la auditoría).
    Idempotente: si ya existe una OCO con los mismos precios no toca nada; si los
    precios deseados cambiaron (p. ej. stop subido a breakeven), cancela y re-coloca.
    Nota: en extended hours ninguna pierna dispara (limitación de Alpaca); ahí el
    monitor sigue siendo la gestión activa.
    """
    import time as _time

    api = _api()
    if api is None:
        return {"armed": False, "reason": "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY"}
    if not stop_price or float(stop_price) <= 0 or not take_profit or float(take_profit) <= 0:
        return {"armed": False, "reason": f"precios inválidos (SL={stop_price}, TP={take_profit})"}
    stop_price  = round(float(stop_price), 2)
    take_profit = round(float(take_profit), 2)
    if stop_price >= take_profit:
        return {"armed": False, "reason": f"SL {stop_price} >= TP {take_profit}"}
    qty = abs(int(float(qty)))
    if qty <= 0:
        return {"armed": False, "reason": "qty inválida"}

    try:
        # ¿Qué protección hay ya? (stop y/o limit abiertos, incluyendo piernas OCO)
        cur_stop = cur_limit = None
        existing = _open_orders_for(api, symbol)
        for o in existing:
            otype = (getattr(o, "order_type", None) or getattr(o, "type", None) or "")
            if "stop" in otype and getattr(o, "stop_price", None):
                cur_stop = float(o.stop_price)
            elif otype == "limit" and getattr(o, "limit_price", None):
                cur_limit = float(o.limit_price)

        if (cur_stop is not None and abs(cur_stop - stop_price) < 0.01
                and cur_limit is not None and abs(cur_limit - take_profit) < 0.01):
            return {"armed": False, "reason": "ya tiene OCO vigente",
                    "stop_price": stop_price, "take_profit": take_profit}

        # Reconciliar: cancelar TODO lo abierto del símbolo (libera los shares) y re-colocar.
        for o in existing:
            try:
                api.cancel_order(o.id)
            except Exception:
                pass  # piernas OCO se cancelan en cascada; el segundo cancel puede fallar

        # Esperar a que las cancelaciones procesen antes de re-colocar (evita
        # "insufficient qty available" por shares aún retenidos por la orden vieja).
        for _ in range(5):
            if not _open_orders_for(api, symbol):
                break
            _time.sleep(1)

        # Con el mercado cerrado, Alpaca deja la cancelación en pending_cancel y los
        # shares retenidos — no tiene caso enviar la OCO. La función es idempotente:
        # la próxima corrida (cancelación ya procesada) la coloca. Sin pérdida real de
        # protección: fuera de horario regular los stops tampoco disparan.
        remaining = _open_orders_for(api, symbol)
        if any(getattr(o, "status", "") == "pending_cancel" for o in remaining):
            return {"armed": False,
                    "reason": "stop viejo en pending_cancel (mercado cerrado); "
                              "la OCO se colocará en la próxima corrida"}

        last_err = None
        for attempt in range(2):
            try:
                order = api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="sell",
                    type="limit",
                    time_in_force="gtc",
                    order_class="oco",
                    take_profit={"limit_price": take_profit},
                    stop_loss={"stop_price": stop_price},
                )
                return {
                    "armed":       True,
                    "order_id":    str(order.id),
                    "order_class": "oco",
                    "stop_price":  stop_price,
                    "take_profit": take_profit,
                    "qty":         qty,
                    "replaced":    bool(existing),
                }
            except Exception as e:
                last_err = e
                if attempt == 0:
                    _time.sleep(2)   # típico: cancelación aún en vuelo
        return {"armed": False, "reason": f"OCO no colocada: {last_err}"}
    except Exception as e:
        return {"armed": False, "reason": str(e)}


def ensure_protective_stop(symbol: str, stop_price: float, qty) -> dict:
    """Coloca un stop-loss GTC si el símbolo no tiene ya una orden stop abierta. Idempotente.
    Fallback de ensure_exit_bracket cuando no hay TP conocido (protege al menos el piso)."""
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

        # (M1) Validar los niveles contra el precio VIVO, no solo los números del LLM.
        # El risk_agent solo checa coherencia interna (R/R, tamaño); si el precio real ya
        # cruzó el SL/TP propuestos, Alpaca rechaza el bracket (caso CPOP: "stop_price must
        # be <= base_price") o entraríamos sin colchón. Se descarta el candidato.
        if decision == "BUY":
            try:
                live_price = float(api.get_latest_trade(symbol, feed="iex").price)
            except Exception:
                live_price = 0.0
            if live_price > 0:
                if stop_loss > 0 and live_price <= stop_loss:
                    return {"executed": False,
                            "reason": f"precio vivo ${live_price} <= SL ${stop_loss} (setup inválido)"}
                if take_profit > 0 and live_price >= take_profit:
                    return {"executed": False,
                            "reason": f"precio vivo ${live_price} >= TP ${take_profit} (ya en objetivo)"}
                entry_price = live_price   # dimensionar con el precio real (la entrada es a mercado)

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
