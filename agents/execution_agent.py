"""
Execution Agent — Ejecución de órdenes. SIN LLM. Completamente determinístico.
Solo actúa si risk_agent aprobó. Habla con Alpaca a través de agents/broker.py.
En paper mode, las órdenes son simuladas (sin dinero real).
"""

import time as _time
from datetime import datetime

from . import broker


def _no_creds() -> dict:
    return {"reason": "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY"}


def close_position(symbol: str) -> dict:
    """Cierra a mercado toda la posición del símbolo. SIN LLM."""
    if broker.trading() is None:
        return {"closed": False, **_no_creds()}
    try:
        broker.cancel_orders_for(symbol)          # libera los shares de las protectoras
        order = broker.close_position_market(symbol)
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
    if broker.trading() is None:
        return {"closed": False, **_no_creds()}
    if not limit_price or float(limit_price) <= 0:
        return {"closed": False, "reason": f"limit_price inválido: {limit_price}"}
    try:
        qty = broker.position_qty(symbol)
        if qty <= 0:
            return {"closed": False, "reason": "sin posición"}

        broker.cancel_orders_for(symbol)
        order = broker.submit_limit_extended(symbol, qty, "sell", round(float(limit_price), 2))
        return {
            "closed":         True,
            "order_id":       str(order.id),
            "symbol":         symbol,
            "qty":            qty,
            "limit_price":    round(float(limit_price), 2),
            "extended_hours": True,
            "timestamp":      datetime.now().isoformat(),
        }
    except Exception as e:
        return {"closed": False, "reason": str(e), "timestamp": datetime.now().isoformat()}


def ensure_exit_bracket(symbol: str, stop_price: float, take_profit: float, qty) -> dict:
    """Mantiene una pareja OCO GTC (TP limit + SL stop) para la posición. SIN LLM.

    Broker-side: el TP y el SL viven en Alpaca y se ejecutan al instante en horario
    regular, sin depender de la cadencia del cron (hallazgo C2 de la auditoría).
    Idempotente: si ya existe una OCO con los mismos precios no toca nada; si los
    precios deseados cambiaron (p. ej. stop subido a breakeven), cancela y re-coloca.
    Nota: en extended hours ninguna pierna dispara (limitación de Alpaca); ahí el
    monitor sigue siendo la gestión activa.
    """
    if broker.trading() is None:
        return {"armed": False, **_no_creds()}
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
        existing = broker.open_orders(symbol)
        for o in existing:
            if "stop" in o["order_type"] and o["stop_price"]:
                cur_stop = o["stop_price"]
            elif o["order_type"] == "limit" and o["limit_price"]:
                cur_limit = o["limit_price"]

        if (cur_stop is not None and abs(cur_stop - stop_price) < 0.01
                and cur_limit is not None and abs(cur_limit - take_profit) < 0.01):
            return {"armed": False, "reason": "ya tiene OCO vigente",
                    "stop_price": stop_price, "take_profit": take_profit}

        # Reconciliar: cancelar TODO lo abierto del símbolo (libera los shares) y re-colocar.
        broker.cancel_orders_for(symbol)

        # Esperar a que las cancelaciones procesen antes de re-colocar (evita
        # "insufficient qty available" por shares aún retenidos por la orden vieja).
        for _ in range(5):
            if not broker.open_orders(symbol):
                break
            _time.sleep(1)

        # Con el mercado cerrado, Alpaca deja la cancelación en pending_cancel y los
        # shares retenidos — no tiene caso enviar la OCO. La función es idempotente:
        # la próxima corrida (cancelación ya procesada) la coloca. Sin pérdida real de
        # protección: fuera de horario regular los stops tampoco disparan.
        remaining = broker.open_orders(symbol)
        if any(o["status"] == "pending_cancel" for o in remaining):
            return {"armed": False,
                    "reason": "stop viejo en pending_cancel (mercado cerrado); "
                              "la OCO se colocará en la próxima corrida"}

        last_err = None
        for attempt in range(2):
            try:
                order = broker.submit_oco_gtc(symbol, qty, take_profit, stop_price)
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
    if broker.trading() is None:
        return {"armed": False, **_no_creds()}
    if not stop_price or float(stop_price) <= 0:
        return {"armed": False, "reason": f"stop_price inválido: {stop_price}"}
    try:
        qty = abs(int(float(qty)))
        if qty <= 0:
            return {"armed": False, "reason": "qty inválida"}

        # ¿Ya hay una orden stop abierta para el símbolo? → no duplicar
        for o in broker.open_orders(symbol):
            if "stop" in o["order_type"]:
                return {"armed": False, "reason": "ya tiene stop activo", "order_id": o["id"]}

        order = broker.submit_stop_gtc(symbol, qty, round(float(stop_price), 2))
        return {
            "armed":      True,
            "order_id":   str(order.id),
            "stop_price": round(float(stop_price), 2),
            "qty":        qty,
        }
    except Exception as e:
        return {"armed": False, "reason": str(e)}


def _has_exposure(symbol: str) -> bool:
    """True si ya existe una posición abierta o una orden pendiente para el símbolo."""
    return symbol in broker.held_symbols() or symbol in broker.pending_order_symbols()


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

    if broker.trading() is None:
        return {"executed": False, **_no_creds()}

    try:
        # Guardrail anti-duplicado: no abrir otra orden si ya hay exposición al símbolo
        if _has_exposure(symbol):
            return {"executed": False, "reason": f"{symbol} ya en cartera o con orden pendiente"}

        equity       = broker.account_equity()
        size_pct     = synthesis.get("position_size_pct", 0.05)
        entry_price  = synthesis.get("entry", 0.0)
        take_profit  = synthesis.get("take_profit", 0.0)
        stop_loss    = synthesis.get("stop_loss", 0.0)

        if equity <= 0:
            return {"executed": False, "reason": "No se pudo leer el equity de la cuenta"}
        if entry_price <= 0:
            return {"executed": False, "reason": f"Precio de entrada inválido: {entry_price}"}

        # (M1) Validar los niveles contra el precio VIVO, no solo los números del LLM.
        # El risk_agent solo checa coherencia interna (R/R, tamaño); si el precio real ya
        # cruzó el SL/TP propuestos, Alpaca rechaza el bracket (caso CPOP: "stop_price must
        # be <= base_price") o entraríamos sin colchón. Se descarta el candidato.
        if decision == "BUY":
            live_price = broker.latest_price(symbol)
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
            order = broker.submit_bracket_buy(symbol, shares, take_profit, stop_loss)
            order_class = "bracket"
        else:
            # Fallback (p. ej. SELL): orden de mercado simple
            order = broker.submit_market(symbol, shares, side)
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
            "paper_mode":      broker.is_paper(),
            "timestamp":       datetime.now().isoformat(),
        }

    except Exception as e:
        return {
            "executed":  False,
            "reason":    str(e),
            "timestamp": datetime.now().isoformat(),
        }
