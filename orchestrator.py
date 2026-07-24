"""
Orchestrator — Trading Agent v1.1
Coordina sub-agentes. Lee CLAUDE.md en cada sesión.
Modo: PAPER TRADING por defecto.

Uso:
  python orchestrator.py full NVDA       # pipeline completo de UN símbolo
  python orchestrator.py full            # modo BATCH: universo amplio + ranking + cupos
  python orchestrator.py monitor NVDA    # monitoreo de posición abierta
"""

import glob
import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

# Zona horaria del Este para timestamps tz-aware (B3). No depender del TZ del runner:
# en Actions viene fijado a America/New_York, pero una corrida local en otra TZ mezclaría
# horas en el journal y rompería la comparación de cadencia de entradas.
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None  # sin tzdata: se cae a hora local naive (menos ideal, no rompe)


def now_et() -> datetime:
    """Ahora en horario del Este, tz-aware si hay tzdata disponible."""
    return datetime.now(_ET) if _ET is not None else datetime.now()

# Windows: la consola (cp1252) no imprime los emojis/box-drawing de la salida.
# Forzamos UTF-8 en stdout/stderr para no depender de PYTHONUTF8 en el entorno.
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from agents.research import gather_research
from agents.bull_agent import run_bull_agent
from agents.bear_agent import run_bear_agent
from agents.synthesis_agent import run_synthesis
from agents.risk_agent import validate_trade
from agents.execution_agent import (
    execute_trade, close_position, close_position_extended,
    ensure_exit_bracket, ensure_protective_stop,
)
from agents.universe import build_universe
from agents.screen import local_screen
from agents.exit_agent import local_exit, review_thesis
from agents import broker


def load_config() -> dict:
    """Config completa del watchlist (símbolos fijos + parámetros de universo/cupos)."""
    with open("config/watchlist.json") as f:
        return json.load(f)


def get_portfolio() -> dict:
    """Lee el estado real del portafolio paper desde Alpaca (equity/cash/buying_power)."""
    acct = broker.account()
    if acct:
        return acct
    print("  [portfolio] No se pudo leer la cuenta. Usando valores por defecto.")
    return {"equity": 100_000, "cash": 100_000, "buying_power": 100_000}


def get_portfolio_state() -> dict:
    """Estado enriquecido: equity/cash + símbolos ya en cartera y con órdenes pendientes."""
    state = get_portfolio()
    state["held"]    = broker.held_symbols()
    state["pending"] = broker.pending_order_symbols()
    return state


def market_status() -> str:
    """Etiqueta legible del estado del mercado según el reloj de Alpaca."""
    c = broker.clock()
    if not c:
        return "desconocido"
    return "ABIERTO" if c["is_open"] else f"CERRADO (próxima apertura: {c['next_open']})"


def detect_session() -> str:
    """Sesión de mercado según Alpaca (a prueba de DST/feriados): regular|pre|post|closed.

    pre  = [04:00, apertura) ET │ post = [cierre, 20:00) ET │ regular = clock.is_open.
    """
    info = broker.market_session_info()
    if not info:
        return "closed"
    if info["is_open"]:
        return "regular"
    open_t, close_t = info.get("open_time"), info.get("close_time")
    if not open_t or not close_t:
        return "closed"                                # fin de semana o feriado
    t = info["now_et"].time()
    if dtime(4, 0) <= t < open_t:
        return "pre"
    if close_t <= t < dtime(20, 0):
        return "post"
    return "closed"


def in_no_trade_window(open_buffer_min: int = 15, close_buffer_min: int = 15) -> bool:
    """True en los primeros/últimos minutos de la sesión regular → no abrir entradas (M2).

    Usa el calendario de Alpaca (open/close reales del día) → correcto en DST y cierres
    tempranos (donde 15:45–16:00 no aplicaría). Solo afecta ENTRADAS; las salidas siguen
    permitidas en todo momento. Ante cualquier duda devuelve False (no bloquear de más).
    """
    if open_buffer_min <= 0 and close_buffer_min <= 0:
        return False
    info = broker.market_session_info()
    if not info or not info["is_open"] or not info.get("open_time") or not info.get("close_time"):
        return False
    now = info["now_et"]

    def _to_dt(t):
        return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

    mins_since_open = (now - _to_dt(info["open_time"])).total_seconds() / 60
    mins_to_close   = (_to_dt(info["close_time"]) - now).total_seconds() / 60
    return (0 <= mins_since_open < open_buffer_min) or (0 <= mins_to_close < close_buffer_min)


def _reward_risk(synthesis: dict) -> float:
    """R/R de un BUY = (TP − entrada) / (entrada − SL). 0.0 si los precios no son válidos.

    Sirve de desempate del ranking cuando el modelo aplana las confianzas.
    """
    entry = synthesis.get("entry", 0.0) or 0.0
    tp    = synthesis.get("take_profit", 0.0) or 0.0
    sl    = synthesis.get("stop_loss", 0.0) or 0.0
    risk  = entry - sl
    if entry <= 0 or tp <= 0 or sl <= 0 or risk <= 0:
        return 0.0
    return round((tp - entry) / risk, 2)


def save_journal(state: dict, symbol: str) -> None:
    Path("journal").mkdir(exist_ok=True)
    ts = now_et().strftime("%Y-%m-%d_%H%M%S")
    path = f"journal/{symbol}_{ts}.json"
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    print(f"  [journal] → {path}")


def load_latest_journal(symbol: str) -> dict:
    """Última entrada de journal del símbolo que sea una DECISIÓN (tiene synthesis con SL/TP),
    no un snapshot de monitor. Devuelve {} si no hay ninguna."""
    paths = sorted(glob.glob(f"journal/{symbol}_*.json"), reverse=True)
    for path in paths:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        syn = data.get("synthesis")
        if isinstance(syn, dict) and syn.get("stop_loss"):
            return data
    return {}


# ── Estado persistente (para cadencia de entradas entre corridas) ──
def _state_path() -> Path:
    return Path("journal") / "state.json"


def load_state() -> dict:
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    Path("journal").mkdir(exist_ok=True)
    with open(_state_path(), "w") as f:
        json.dump(st, f, indent=2, default=str)


# ── Estado de protección por símbolo (M4): SL/TP vigentes persistidos en state.json ──
# Antes, la protección salía solo de load_latest_journal(); si ese archivo faltaba (rollup,
# clone nuevo, símbolo operado a mano) la posición quedaba sin stop. Además el breakeven se
# recalculaba desde el P/L de cada corrida, así que al bajar de +4% el stop RETROCEDÍA
# (flapping 196.96↔180 observado en NVDA). Persistir el nivel lo vuelve un trinquete.
def _load_protection(symbol: str) -> dict:
    return load_state().get("protection", {}).get(symbol, {})


def _save_protection(symbol: str, stop, take_profit, breakeven: bool) -> None:
    st = load_state()
    prot = st.setdefault("protection", {})
    prot[symbol] = {
        "stop":        round(float(stop), 2) if stop else 0.0,
        "take_profit": round(float(take_profit), 2) if take_profit else 0.0,
        "breakeven":   bool(breakeven),
        "updated":     now_et().isoformat(),
    }
    save_state(st)


def _clear_protection(symbol: str) -> None:
    st = load_state()
    if symbol in st.get("protection", {}):
        del st["protection"][symbol]
        save_state(st)


def _read_open_protection(symbol: str) -> dict:
    """Último recurso (M4): lee SL/TP de la OCO/stop abierta en Alpaca cuando ni el state
    ni el journal los tienen (p. ej. posición entrada a mano)."""
    out = {}
    for o in broker.open_orders(symbol):
        if "stop" in o["order_type"] and o["stop_price"]:
            out["stop"] = o["stop_price"]
        elif o["order_type"] == "limit" and o["limit_price"]:
            out["take_profit"] = o["limit_price"]
    return out


def entries_due(min_gap_min: int = 30) -> bool:
    """True si pasó ≥min_gap_min desde la última búsqueda de entradas (protege RPD)."""
    last = load_state().get("last_entry_run")
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last)
        cur  = now_et()
        # Tolerar estados viejos naive junto a timestamps nuevos tz-aware (B3).
        if (prev.tzinfo is None) != (cur.tzinfo is None):
            prev, cur = prev.replace(tzinfo=None), cur.replace(tzinfo=None)
        return (cur - prev).total_seconds() >= min_gap_min * 60
    except Exception:
        return True


def mark_entries_ran() -> None:
    st = load_state()
    st["last_entry_run"] = now_et().isoformat()
    save_state(st)


# ── Estado de la corrida: errores críticos para que Actions marque el run en rojo (M5) ──
# Un fallo "suave" (el agente imprime [ERROR] pero el proceso sale 0) hace que un agente
# medio-roto se vea "success" para siempre. Estos helpers acumulan fallos CRÍTICOS —
# Alpaca/Groq caídos o un crash global — y salen con código 1 para disparar el email de
# GitHub. Los fallos por-símbolo tolerables NO son críticos (se aíslan y la corrida sigue).
_CRITICAL_ERRORS: list = []


def _note_critical(msg: str) -> None:
    print(f"  [CRÍTICO] {msg}")
    _CRITICAL_ERRORS.append(msg)


def _alpaca_reachable() -> bool:
    """True si se puede leer la cuenta. Un fallo aquí deja al agente a ciegas → crítico."""
    return broker.is_reachable()


def _finish() -> None:
    """Cierra la corrida: si hubo errores críticos, sale con código 1 (Actions → rojo)."""
    if _CRITICAL_ERRORS:
        print(f"\n✗ Corrida con {len(_CRITICAL_ERRORS)} error(es) crítico(s):")
        for m in _CRITICAL_ERRORS:
            print(f"    - {m}")
        sys.exit(1)
    print("\n✓ Corrida sin errores críticos.")


# ─────────────────────────────────────────────────────────
# Análisis (sin ejecución): research → bull → bear → synthesis → risk
# ─────────────────────────────────────────────────────────
def analyze_symbol(symbol: str, portfolio: dict, research: dict = None) -> dict:
    print(f"\n{'═'*54}")
    print(f"  ANÁLISIS │ {symbol} │ {now_et().strftime('%H:%M:%S')}")
    print(f"{'═'*54}")

    # 1. Research (sin LLM) — se reutiliza si ya viene precomputado del pre-screen
    print("\n[1/4] Research Agent — recopilando datos...")
    if research is None:
        research = gather_research(symbol)
    p = research["price"]
    print(f"  Precio: ${p['current']} │ RSI: {p['rsi14']} │ SMA20: ${p['sma20']} │ SMA50: ${p['sma50']}")
    print(f"  Cambio 5d: {p['change_5d_pct']}% │ Vol ratio: {research['volume']['ratio']}x")

    # 2a. Bull Agent (LLM — construye caso alcista)
    print("\n[2a/4] Bull Agent — argumentando a favor...")
    bull = run_bull_agent(research)
    print(f"  Convicción: {bull['conviction']:.2f} │ {bull['thesis']}")

    # 2b. Bear Agent (LLM — destruye la tesis)
    print("\n[2b/4] Bear Agent — atacando la tesis...")
    bear = run_bear_agent(research)
    print(f"  Convicción: {bear['conviction']:.2f} │ {bear['thesis']}")

    # 3. Synthesis Agent (LLM — árbitro)
    print("\n[3/4] Synthesis Agent — pesando argumentos...")
    synthesis = run_synthesis(research, bull, bear)
    print(f"  Decisión: {synthesis['decision']} │ Confianza: {synthesis['confidence']:.2f}")
    print(f"  Entrada: ${synthesis.get('entry','?')} │ TP: ${synthesis.get('take_profit','?')} │ SL: ${synthesis.get('stop_loss','?')}")

    # 4. Risk Agent (sin LLM — reglas fijas)
    print("\n[4/4] Risk Agent — validando reglas...")
    risk = validate_trade(synthesis, portfolio)
    if risk["approved"]:
        for r in risk["rules_passed"]:
            print(f"  ✓ {r}")
    else:
        for r in risk["rules_failed"]:
            print(f"  ✗ {r}")

    return {
        "symbol":    symbol,
        "timestamp": now_et().isoformat(),
        "portfolio": {k: v for k, v in portfolio.items() if k not in ("held", "pending")},
        "research":  research,
        "bull":      bull,
        "bear":      bear,
        "synthesis": synthesis,
        "risk":      risk,
    }


# ─────────────────────────────────────────────────────────
# Pipeline completo de UN símbolo (analiza + ejecuta)
# ─────────────────────────────────────────────────────────
def run_full_pipeline(symbol: str) -> dict:
    portfolio = get_portfolio()
    state = analyze_symbol(symbol, portfolio)

    print("\n[Ejecución] Execution Agent...")
    execution = execute_trade(state["synthesis"], state["risk"], symbol)
    if execution["executed"]:
        _save_protection(symbol, state["synthesis"].get("stop_loss"),
                         state["synthesis"].get("take_profit"), breakeven=False)
        print(f"  ✓ Orden {execution.get('order_class','?')} │ ID: {execution['order_id']} │ Shares: {execution['shares']}")
    else:
        print(f"  — Sin orden: {execution.get('reason', '–')}")

    state["pipeline"] = "full"
    state["execution"] = execution
    save_journal(state, symbol)
    return state


# ─────────────────────────────────────────────────────────
# Modo BATCH: universo amplio, ranking por confianza, cupos
# ─────────────────────────────────────────────────────────
def run_batch(session: str = "regular") -> None:
    cfg        = load_config()
    max_pos    = cfg.get("max_positions", 5)
    llm_budget = cfg.get("llm_budget", 12)
    entry_gap  = cfg.get("entry_min_gap_min", 30)
    dyn        = cfg.get("dynamic_universe", {})
    static_set = {s.upper() for s in cfg.get("symbols", [])}
    universe = build_universe(
        cfg.get("symbols", []),
        top=dyn.get("top", 14),
        cap=cfg.get("analysis_cap", 15),
        dynamic=dyn.get("enabled", True),
        min_price=dyn.get("min_price", 10.0),
    )

    state = get_portfolio_state()
    excluded = state["held"] | state["pending"]
    slots = max(0, max_pos - len(excluded))

    print(f"\n{'━'*54}")
    print("  MODO BATCH")
    print(f"  Mercado: {market_status()}")
    print(f"  Universo ({len(universe)}): {', '.join(universe)}")
    print(f"  En cartera/pendiente: {', '.join(sorted(excluded)) or '—'}")
    print(f"  Cupos libres: {slots} de {max_pos} │ Presupuesto IA: {llm_budget} símbolos")
    print(f"{'━'*54}")

    # ── Gestión de salidas: monitorear TODO lo abierto/pendiente (incluso fuera del universo) ──
    # Aislado por símbolo: un error (research/Alpaca) no deja al resto sin gestión (A3).
    mon_fail = 0
    for sym in sorted(excluded):
        try:
            run_light_pipeline(sym, session=session)
        except Exception as e:
            mon_fail += 1
            print(f"  [ERROR] monitor {sym}: {e} — se continúa con el resto")
    if excluded and mon_fail == len(excluded):
        _note_critical(f"gestión de salidas falló en las {len(excluded)} posiciones "
                       "(posible caída de Alpaca/research)")

    # ── ¿Buscar entradas? solo con cupo libre y cadencia cumplida (protege RPD) ──
    if slots <= 0:
        print("\n  Sin cupos libres → no se buscan entradas nuevas.")
        return
    if not entries_due(entry_gap):
        print(f"\n  Entradas en pausa (<{entry_gap} min desde la última búsqueda).")
        return
    # (M2) No abrir en los primeros/últimos minutos de la sesión (los más caóticos).
    if in_no_trade_window(cfg.get("no_trade_open_min", 15), cfg.get("no_trade_close_min", 15)):
        print("\n  En ventana de no-operar (apertura/cierre) → salidas sí, entradas no.")
        return

    # ── Fase 1: pre-screen LOCAL (research + screen, SIN IA) ──
    print("\n[Pre-screen local] research + filtro técnico (sin llamar IA)...")
    screened = []
    for sym in universe:
        if sym in excluded:
            continue   # ya monitoreado arriba
        try:
            research = gather_research(sym)
            sc = local_screen(research)
        except Exception as e:
            print(f"  [ERROR] {sym}: {e}")
            continue
        is_static = sym in static_set
        tag = "curado " if is_static else "dinámico"
        p = research["price"]
        # Los curados siempre son elegibles; los dinámicos deben pasar el hard-drop
        if is_static or sc["passes"]:
            screened.append({"symbol": sym, "research": research, "screen": sc, "static": is_static})
            print(f"  [{tag}] {sym:6} RSI {p['rsi14']:>5} │ score {sc['score']:>4} → elegible")
        else:
            print(f"  [{tag}] {sym:6} RSI {p['rsi14']:>5} │ descartado sin IA: {', '.join(sc['reasons'])}")
        time.sleep(1)

    # ── Fase 2: selección hasta llm_budget (curados primero, dinámicos por score) ──
    curated = [x for x in screened if x["static"]]
    dynamic = sorted([x for x in screened if not x["static"]],
                     key=lambda x: x["screen"]["score"], reverse=True)
    remaining = max(0, llm_budget - len(curated))
    selected = curated + dynamic[:remaining]
    over_budget = dynamic[remaining:]
    for x in over_budget:
        print(f"  (sin presupuesto IA) {x['symbol']:6} score {x['screen']['score']}")

    # ── Fase 3: análisis con IA solo para los seleccionados ──
    candidates = []
    analysis_fail = 0
    for x in selected:
        try:
            analysis = analyze_symbol(x["symbol"], state, research=x["research"])
            if analysis["risk"]["approved"] and analysis["synthesis"]["decision"] == "BUY":
                candidates.append(analysis)
        except Exception as e:
            analysis_fail += 1
            print(f"\n[ERROR] {x['symbol']}: {e}")
        time.sleep(2)  # pausa entre símbolos
    if selected and analysis_fail == len(selected):
        _note_critical(f"análisis con IA falló en los {len(selected)} símbolos "
                       "(posible caída de Groq/LLM)")

    # Ranking por confianza; DESEMPATE por R/R (a igual confianza, mejor riesgo/beneficio
    # primero). Scout tiende a aplanar la confianza, así que el R/R hace el ranking útil.
    candidates.sort(
        key=lambda a: (a["synthesis"].get("confidence", 0.0), _reward_risk(a["synthesis"])),
        reverse=True,
    )

    print(f"\n{'━'*54}")
    print(f"  RANKING │ {len(candidates)} candidatos BUY │ {slots} cupos")
    print(f"{'━'*54}")
    for i, a in enumerate(candidates):
        mark = "→ ejecutar" if i < slots else "  (sin cupo)"
        print(f"  {mark} │ {a['symbol']:6} conf {a['synthesis']['confidence']:.2f} │ R/R {_reward_risk(a['synthesis']):.2f}")

    executed = 0
    for i, a in enumerate(candidates):
        sym = a["symbol"]
        if executed >= slots:
            a["pipeline"] = "batch"
            a["execution"] = {"executed": False, "reason": "sin cupo (slots llenos)"}
        else:
            print(f"\n[Ejecución] {sym}...")
            execution = execute_trade(a["synthesis"], a["risk"], sym)
            if execution["executed"]:
                executed += 1
                _save_protection(sym, a["synthesis"].get("stop_loss"),
                                 a["synthesis"].get("take_profit"), breakeven=False)
                print(f"  ✓ Orden {execution.get('order_class','?')} │ ID: {execution['order_id']} │ Shares: {execution['shares']}")
            else:
                print(f"  — Sin orden: {execution.get('reason', '–')}")
            a["pipeline"] = "batch"
            a["execution"] = execution
        save_journal(a, sym)

    mark_entries_ran()   # registra la cadencia de búsqueda de entradas
    print(f"\n✓ Batch completo │ {executed} orden(es) colocada(s) de {len(candidates)} candidatos.")


# ─────────────────────────────────────────────────────────
# Pipeline ligero: monitoreo de posición abierta
# ─────────────────────────────────────────────────────────
def _read_position(symbol: str) -> dict:
    """Posición abierta real en Alpaca, o {} si no hay."""
    return broker.position(symbol)


def run_light_pipeline(symbol: str, session: str = "regular") -> dict:
    """Gestor de salidas: evalúa una posición abierta y cierra si la tesis se rompe.

    Híbrido: reglas locales duras → (si ambiguo) juez de tesis LLM. Si mantiene, re-arma
    un stop GTC persistente. Cerrar libera un cupo para el próximo batch.

    session: en 'pre'/'post' los cierres usan limit + extended_hours (Alpaca no permite
    market ni deja disparar stops fuera de hora); en 'regular' cierra a mercado.
    """
    print(f"\n[MONITOR] {symbol} │ {now_et().strftime('%H:%M:%S')} │ sesión {session}")

    cfg      = load_config().get("exits", {})
    auto     = cfg.get("auto_close", True)
    research = gather_research(symbol)
    p = research["price"]
    position = _read_position(symbol)

    if not position:
        print(f"  Precio: ${p['current']} │ RSI: {p['rsi14']} │ sin posición abierta (solo pendiente)")
        state = {"symbol": symbol, "timestamp": now_et().isoformat(),
                 "pipeline": "monitor", "action": "NONE", "reason": "sin posición"}
        save_journal(state, symbol)
        return state

    journal  = load_latest_journal(symbol)
    original = journal.get("synthesis", {})
    plpc     = position["unrealized_plpc"] * 100
    print(f"  Entrada ${position['avg_entry_price']} │ Actual ${position['current_price']} │ "
          f"P/L {plpc:+.2f}% │ RSI {p['rsi14']}")

    # 1. Reglas locales
    sig    = local_exit(original, research, position, cfg)
    source = "local"

    # 2. Zona de revisión → juez de tesis LLM
    if sig["zone"] == "review":
        print(f"  [revisión] {sig['reason']} → consultando juez de tesis (LLM)...")
        try:
            verdict = review_thesis(original, research, position,
                                    bull=journal.get("bull"), bear=journal.get("bear"))
            sig = {"action": verdict.get("action", "HOLD"),
                   "reason": verdict.get("reason", ""), "zone": "llm"}
            source = "llm"
        except Exception as e:
            print(f"  [revisión] juez LLM falló ({e}); se mantiene por defecto")
            sig = {"action": "HOLD", "reason": f"juez no disponible: {e}", "zone": "llm"}

    action = sig["action"]
    print(f"  Decisión: {action} ({source}) │ {sig['reason']}")

    # 3. Ejecutar / recomendar cierre
    execution = None
    if action == "CLOSE":
        if auto:
            if session in ("pre", "post"):
                # Extended hours: solo limit + extended_hours. Limit marketable (0.1% bajo el precio).
                limit = round(position["current_price"] * 0.999, 2)
                print(f"  [Cierre] {symbol} extended-hours limit @ ${limit}...")
                execution = close_position_extended(symbol, limit)
            else:
                print(f"  [Cierre] cerrando {symbol} a mercado...")
                execution = close_position(symbol)
            print(f"  {'✓ Cerrada' if execution.get('closed') else '— No cerrada: ' + execution.get('reason','')}")
            if execution.get("closed"):
                _clear_protection(symbol)   # (M4) la posición se fue; olvida su estado
        else:
            print(f"  RECOMENDACIÓN: cerrar {symbol} (auto_close=off, sin tocar la cuenta)")
            execution = {"closed": False, "reason": "recomendación (auto_close off)"}
    else:
        # 4. Mantener → protección broker-side: pareja OCO GTC (TP limit + SL stop).
        # Resolución de niveles (M4): state.json manda (incluye el stop ya trinqueteado),
        # journal como fallback (posiciones viejas), OCO viva como último recurso.
        prot_state = _load_protection(symbol)
        base_sl    = prot_state.get("stop") or original.get("stop_loss") or 0.0
        tp_target  = prot_state.get("take_profit") or original.get("take_profit") or 0.0
        if not base_sl or not tp_target:
            live = _read_open_protection(symbol)
            base_sl   = base_sl   or live.get("stop", 0.0)
            tp_target = tp_target or live.get("take_profit", 0.0)

        # Breakeven como TRINQUETE (A2 corregido): el flag se pega una vez alcanzado, y el
        # stop deseado nunca baja del nivel ya persistido — aunque el P/L retroceda bajo +4%.
        be_at     = cfg.get("breakeven_at_pct", 0.04)
        breakeven = bool(prot_state.get("breakeven", False))
        if be_at and position.get("unrealized_plpc") is not None \
                and float(position["unrealized_plpc"]) >= float(be_at):
            breakeven = True
        sl_desired = base_sl
        if breakeven:
            sl_desired = max(sl_desired, round(float(position["avg_entry_price"]), 2))
        sl_desired = max(sl_desired, prot_state.get("stop", 0.0))   # trinquete duro

        if sl_desired > 0 and tp_target > 0:
            protection = ensure_exit_bracket(symbol, sl_desired, tp_target, position["qty"])
        else:
            # Sin TP conocido: al menos el stop persistente
            protection = ensure_protective_stop(symbol, sl_desired, position["qty"])
        protection["breakeven"] = breakeven

        # Persistir el nivel deseado (aunque la OCO ya estuviera vigente): el state es la
        # fuente de verdad del trinquete para la próxima corrida.
        if sl_desired > 0:
            _save_protection(symbol, sl_desired, tp_target or sl_desired, breakeven)

        if protection.get("armed"):
            kind = "OCO GTC" if protection.get("order_class") == "oco" else "stop GTC"
            extra = " (stop en breakeven)" if breakeven else ""
            print(f"  [protección] {kind} armada: SL ${protection.get('stop_price')}"
                  + (f" / TP ${protection.get('take_profit')}" if protection.get('take_profit') else "")
                  + extra)
        else:
            print(f"  [protección] {protection.get('reason', '—')}"
                  + (" (breakeven persistido)" if breakeven else ""))
        execution = {"protection": protection}

    state = {
        "symbol":    symbol,
        "timestamp": now_et().isoformat(),
        "pipeline":  "monitor",
        "position":  position,
        "action":    action,
        "reason":    sig["reason"],
        "source":    source,
        "execution": execution,
    }
    save_journal(state, symbol)
    return state


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────
def main():
    print("\n⚡ Trading Agent Orchestrator v1.1")
    print("   Modo: PAPER TRADING (sin capital real)\n")
    _CRITICAL_ERRORS.clear()

    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    symbol = sys.argv[2].upper() if len(sys.argv) > 2 else None

    if mode == "auto":
        # Router por sesión (para GitHub Actions): decide qué hacer según el reloj de Alpaca.
        try:
            # Alpaca primero: si está caído, detect_session devolvería "closed" y enmascararía
            # la caída como "mercado cerrado" (run verde eterno). Verificar alcance rompe eso.
            if not _alpaca_reachable():
                _note_critical("Alpaca inaccesible: no se pudo leer la cuenta")
            else:
                session = detect_session()
                print(f"  Sesión detectada: {session}")
                if session == "regular":
                    run_batch(session="regular")        # monitorea abiertos + busca entradas
                elif session in ("pre", "post"):
                    st = get_portfolio_state()
                    held = sorted(st["held"] | st["pending"])
                    if not held:
                        print("  Sin posiciones abiertas que gestionar en extended hours.")
                    mon_fail = 0
                    for sym in held:
                        try:
                            run_light_pipeline(sym, session=session)   # solo gestión de salidas
                        except Exception as e:
                            mon_fail += 1
                            print(f"  [ERROR] monitor {sym}: {e} — se continúa con el resto")
                        time.sleep(2)
                    if held and mon_fail == len(held):
                        _note_critical(f"gestión de salidas falló en las {len(held)} posiciones "
                                       "(posible caída de Alpaca/research)")
                else:
                    print("  Mercado cerrado → no-op.")
        except Exception as e:
            _note_critical(f"auto abortó con excepción no controlada: {e}")
        _finish()
        return

    if mode == "full" and symbol is None:
        # Sin símbolo → modo batch (universo amplio + ranking + cupos)
        try:
            run_batch()
        except Exception as e:
            _note_critical(f"batch abortó: {e}")
        _finish()
        return

    symbols = [symbol] if symbol else load_config().get("symbols", [])
    for sym in symbols:
        try:
            if mode == "full":
                run_full_pipeline(sym)
            elif mode == "monitor":
                run_light_pipeline(sym)
            else:
                print(f"Modo desconocido: {mode}. Usa 'full' o 'monitor'.")
        except Exception as e:
            _note_critical(f"{sym}: {e}")
            print("  Pipeline detenido. Revisa journal/ para detalles.")
        time.sleep(2)
    _finish()


if __name__ == "__main__":
    main()
