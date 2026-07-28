"""
Dashboard estático del trading-agent (Fase 8). SIN LLM.

Lee el journal (live + archivo), el historial de la cuenta y los fills de Alpaca, computa
métricas y renderiza un `docs/index.html` autocontenido (sin dependencias externas — la
CSP de GitHub Pages bloquea CDNs). El workflow lo regenera y commitea en cada corrida;
GitHub Pages sirve la URL. Efectividad sobre comodidad: números reales, cero infraestructura.

Uso:  python report/build_dashboard.py
"""

import glob
import html
import json
import os
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone

# Permite `python report/build_dashboard.py` desde la raíz del repo
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _s in (sys.stdout, sys.stderr):
    rc = getattr(_s, "reconfigure", None)
    if rc:
        rc(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from agents import broker

INITIAL_EQUITY = 100_000.0


# ─────────────────────────── Carga del journal ───────────────────────────
def load_journals() -> list:
    """Todas las entradas del journal: live (journal/*.json) + archivo (journal/archive/*.jsonl)."""
    entries = []
    for path in glob.glob("journal/*.json"):
        if os.path.basename(path) == "state.json":
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            d["_file"] = os.path.basename(path)
            entries.append(d)
        except Exception:
            pass
    for path in glob.glob("journal/archive/*.jsonl"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception:
            pass
    entries.sort(key=lambda e: e.get("timestamp", ""))
    return entries


# ─────────────────────────── Métricas ───────────────────────────
def realized_trades(fills: list) -> dict:
    """Reconstruye round-trips por símbolo (FIFO) desde los fills. P/L realizado, win rate."""
    lots = defaultdict(deque)   # symbol → deque de [qty, price] de compras abiertas
    trades = []
    for f in fills:
        sym, side, qty, price = f["symbol"], f["side"], f["qty"], f["price"]
        if side == "buy":
            lots[sym].append([qty, price])
        else:  # sell → cerrar contra las compras más viejas
            remaining = qty
            while remaining > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                matched = min(remaining, lot[0])
                trades.append({
                    "symbol": sym, "qty": matched, "entry": lot[1], "exit": price,
                    "pl": (price - lot[1]) * matched, "time": f["time"],
                })
                lot[0] -= matched
                remaining -= matched
                if lot[0] <= 1e-9:
                    lots[sym].popleft()

    wins = [t for t in trades if t["pl"] > 0]
    losses = [t for t in trades if t["pl"] <= 0]
    total_pl = sum(t["pl"] for t in trades)
    return {
        "trades": trades,
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "realized_pl": total_pl,
        "avg_win": (sum(t["pl"] for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(t["pl"] for t in losses) / len(losses)) if losses else 0.0,
    }


def journal_activity(entries: list) -> dict:
    """Actividad agregada del journal: corridas, entradas, cierres, decisiones, uso de IA."""
    runs = set()
    entries_exec = closes = llm_reviews = entry_llm_calls = 0
    monitor_actions = defaultdict(int)
    for e in entries:
        ts = e.get("timestamp", "")
        if ts:
            runs.add(ts[:16])                      # agrupa por minuto ≈ una corrida
        pipe = e.get("pipeline")
        if pipe in ("batch", "full"):
            entry_llm_calls += 3                   # bull + bear + synthesis
            if (e.get("execution") or {}).get("executed"):
                entries_exec += 1
        elif pipe == "monitor":
            monitor_actions[e.get("action") or "?"] += 1
            if e.get("source") == "llm":
                llm_reviews += 1
            if e.get("action") == "CLOSE" and (e.get("execution") or {}).get("closed"):
                closes += 1
    return {
        "runs": len(runs),
        "entries_executed": entries_exec,
        "closes": closes,
        "monitor_actions": dict(monitor_actions),
        "llm_calls_est": entry_llm_calls + llm_reviews,
    }


def recent_decisions(entries: list, n: int = 20) -> list:
    """Últimas n decisiones legibles para el timeline."""
    out = []
    for e in reversed(entries):
        ts = e.get("timestamp", "")[:16].replace("T", " ")
        sym = e.get("symbol", "?")
        pipe = e.get("pipeline")
        if pipe in ("batch", "full"):
            syn = e.get("synthesis") or {}
            ex = e.get("execution") or {}
            verb = "ejecutó" if ex.get("executed") else "descartó"
            detail = f"{syn.get('decision','?')} conf {syn.get('confidence','?')} → {verb}"
            if not ex.get("executed") and ex.get("reason"):
                detail += f" ({str(ex['reason'])[:40]})"
            out.append({"time": ts, "symbol": sym, "kind": "entrada", "detail": detail})
        elif pipe == "monitor":
            pos = e.get("position") or {}
            plpc = pos.get("unrealized_plpc")
            pl = f"{plpc*100:+.1f}%" if isinstance(plpc, (int, float)) else "—"
            out.append({"time": ts, "symbol": sym, "kind": "monitor",
                        "detail": f"{e.get('action','?')} ({e.get('source','?')}) P/L {pl} · {str(e.get('reason',''))[:50]}"})
        if len(out) >= n:
            break
    return out


def compute_metrics() -> dict:
    entries = load_journals()
    acct = broker.account()
    equity = acct.get("equity", 0.0)
    cash = acct.get("cash", 0.0)

    positions = []
    prot_state = {}
    try:
        with open("journal/state.json", encoding="utf-8") as f:
            prot_state = (json.load(f) or {}).get("protection", {})
    except Exception:
        pass
    for sym in sorted(broker.held_symbols()):
        p = broker.position(sym)
        if not p:
            continue
        pr = prot_state.get(sym, {})
        positions.append({
            "symbol": sym, "qty": p["qty"], "entry": p["avg_entry_price"],
            "price": p["current_price"], "pl": p["unrealized_pl"],
            "plpc": p["unrealized_plpc"] * 100,
            "stop": pr.get("stop"), "tp": pr.get("take_profit"),
            "breakeven": pr.get("breakeven", False),
        })

    curve = broker.portfolio_history("1M", "1D")
    rt = realized_trades(broker.fills())
    act = journal_activity(entries)

    unreal = sum(p["pl"] for p in positions)
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "equity": equity, "cash": cash,
        "total_return_pct": (equity - INITIAL_EQUITY) / INITIAL_EQUITY * 100 if equity else 0.0,
        "total_pl": equity - INITIAL_EQUITY,
        "unrealized_pl": unreal,
        "positions": positions,
        "curve": curve,
        "realized": rt,
        "activity": act,
        "recent": recent_decisions(entries),
        "journal_count": len(entries),
    }


if __name__ == "__main__":
    m = compute_metrics()
    # Modo diagnóstico: si se pasa 'metrics', imprime el resumen en vez de generar HTML
    if len(sys.argv) > 1 and sys.argv[1] == "metrics":
        import pprint
        summary = {k: v for k, v in m.items() if k not in ("curve", "recent")}
        summary["curve_points"] = len(m["curve"])
        summary["recent_count"] = len(m["recent"])
        pprint.pprint(summary, width=100)
    else:
        # Resiliencia: si no se pudo leer la cuenta (Alpaca caído), NO sobrescribir el
        # dashboard con datos vacíos — se conserva el último HTML bueno.
        if not m["equity"] and not m["positions"]:
            print("  [dashboard] cuenta ilegible; se conserva el HTML anterior (no se regenera).")
            sys.exit(0)
        from render import render_html
        os.makedirs("docs", exist_ok=True)
        with open("docs/index.html", "w", encoding="utf-8") as f:
            f.write(render_html(m))
        print(f"  [dashboard] → docs/index.html ({m['journal_count']} journals, "
              f"equity ${m['equity']:,.0f}, {m['realized']['count']} trades cerrados)")
