"""
Render del dashboard (Fase 8). Genera un HTML autocontenido (CSS inline, gráfica en SVG,
sin dependencias externas — la CSP de GitHub Pages bloquea CDNs). Recibe el dict de métricas
de build_dashboard.compute_metrics(). Tema claro/oscuro según el sistema del visitante.
"""

import html
from datetime import datetime, timezone


def _fmt_money(v: float, sign: bool = False) -> str:
    s = f"{'+' if sign and v > 0 else ''}{v:,.2f}"
    return s


def _cls(v: float) -> str:
    return "pos" if v > 0 else ("neg" if v < 0 else "flat")


def _equity_svg(curve: list) -> str:
    """Línea de equity en SVG puro, con baseline en el capital inicial y punto final marcado."""
    if len(curve) < 2:
        return '<p class="muted">Aún no hay suficiente historial para la curva.</p>'
    vals = [e for _, e in curve]
    lo, hi = min(vals), max(vals)
    base = 100_000.0
    lo, hi = min(lo, base), max(hi, base)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    W, H = 720, 220
    n = len(vals)

    def x(i):
        return i / (n - 1) * W

    def y(v):
        return H - (v - lo) / (hi - lo) * H

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals))
    area = f"0,{H} " + pts + f" {W},{H}"
    by = y(base)
    last_x, last_y = x(n - 1), y(vals[-1])
    up = vals[-1] >= base
    color = "var(--pos)" if up else "var(--neg)"
    fill = "var(--pos-fill)" if up else "var(--neg-fill)"
    return f'''<svg viewBox="0 0 {W} {H}" class="curve" preserveAspectRatio="none" role="img" aria-label="Curva de equity">
  <polygon points="{area}" fill="{fill}"/>
  <line x1="0" y1="{by:.1f}" x2="{W}" y2="{by:.1f}" class="baseline"/>
  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" vector-effect="non-scaling-stroke"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.5" fill="{color}"/>
</svg>'''


def _tile(label: str, value: str, cls: str = "", sub: str = "") -> str:
    subhtml = f'<div class="sub">{html.escape(sub)}</div>' if sub else ""
    return (f'<div class="tile"><div class="k">{html.escape(label)}</div>'
            f'<div class="v {cls}">{value}</div>{subhtml}</div>')


def render_html(m: dict) -> str:
    gen = m.get("generated", "")
    try:
        gen_h = datetime.fromisoformat(gen).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        gen_h = gen
    rt = m["realized"]
    act = m["activity"]

    # Régimen de mercado (F9)
    reg = m.get("regime") or {}
    reg_label = reg.get("label", "—")
    reg_metrics = reg.get("metrics") or {}
    reg_pol = reg.get("policy") or {}
    reg_cls = {"calm": "pos", "nervous": "warnc", "panic": "neg"}.get(reg_label, "")
    if reg_metrics:
        reg_sub = (f'vol 20d {reg_metrics.get("vol_20d","?")}% · '
                   f'dd {reg_metrics.get("drawdown_60d","?")}%')
        if not reg_pol.get("allow_entries", True):
            reg_sub += " · entradas OFF"
        else:
            reg_sub += f' · tam {reg_pol.get("size_pct",0)*100:.1f}%'
    else:
        reg_sub = "aún sin lectura (se registra en la próxima corrida)"

    # Tiles
    tiles = [
        _tile("Régimen", reg_label.upper(), reg_cls, reg_sub),
        _tile("Equity", f'${_fmt_money(m["equity"])}', "",
              f'inicial $100,000 · cash ${_fmt_money(m["cash"])}'),
        _tile("Retorno total", f'{m["total_return_pct"]:+.2f}%', _cls(m["total_return_pct"]),
              f'${_fmt_money(m["total_pl"], sign=True)}'),
        _tile("P/L realizado", f'${_fmt_money(rt["realized_pl"], sign=True)}', _cls(rt["realized_pl"]),
              f'{rt["count"]} trades cerrados'),
        _tile("P/L no realizado", f'${_fmt_money(m["unrealized_pl"], sign=True)}', _cls(m["unrealized_pl"]),
              f'{len(m["positions"])} posición(es) abierta(s)'),
        _tile("Win rate", f'{rt["win_rate"]:.0f}%', "",
              f'{rt["wins"]}W / {rt["losses"]}L'),
        _tile("Prom. gana / pierde", f'${rt["avg_win"]:,.0f} / ${rt["avg_loss"]:,.0f}',
              "", "por trade cerrado"),
        _tile("Corridas", f'{act["runs"]:,}', "", f'{m["journal_count"]:,} registros de journal'),
        _tile("Actividad IA", f'~{act["llm_calls_est"]:,}', "",
              f'llamadas LLM · {act["entries_executed"]} entradas, {act["closes"]} cierres'),
    ]

    # Posiciones abiertas (con sector — F9)
    if m["positions"]:
        rows = ""
        for p in m["positions"]:
            prot = ""
            if p["stop"] is not None:
                be = " · <span class=\"be\">breakeven</span>" if p["breakeven"] else ""
                prot = f'SL ${p["stop"]:,.2f} / TP ${p["tp"]:,.2f}{be}' if p["tp"] else f'SL ${p["stop"]:,.2f}'
            else:
                prot = '<span class="warn">sin protección</span>'
            rows += (f'<tr><td class="sym">{html.escape(p["symbol"])}</td>'
                     f'<td class="dim">{html.escape(str(p.get("sector","—")))}</td>'
                     f'<td class="num">{p["qty"]:g}</td>'
                     f'<td class="num">${p["entry"]:,.2f}</td>'
                     f'<td class="num">${p["price"]:,.2f}</td>'
                     f'<td class="num {_cls(p["pl"])}">${_fmt_money(p["pl"], sign=True)}</td>'
                     f'<td class="num {_cls(p["plpc"])}">{p["plpc"]:+.2f}%</td>'
                     f'<td class="prot">{prot}</td></tr>')
        positions_html = (f'<table><thead><tr><th>Símbolo</th><th>Sector</th><th class="num">Qty</th>'
                          f'<th class="num">Entrada</th><th class="num">Actual</th>'
                          f'<th class="num">P/L</th><th class="num">%</th><th>Protección</th></tr></thead>'
                          f'<tbody>{rows}</tbody></table>')
        # Exposición por sector: barra + aviso de concentración
        exp = m.get("exposure") or {}
        total = sum(exp.values()) or 1
        bars = ""
        for sec, n in sorted(exp.items(), key=lambda kv: -kv[1]):
            pct = n / total * 100
            heavy = " heavy" if n >= 3 else ""
            bars += (f'<div class="exp-row"><span class="exp-label">{html.escape(str(sec))}</span>'
                     f'<span class="exp-bar"><span class="exp-fill{heavy}" style="width:{pct:.0f}%"></span></span>'
                     f'<span class="exp-n">{n}</span></div>')
        concentrated = max(exp.values()) if exp else 0
        warn = ('<p class="muted warn-txt">Concentración alta: un solo sector domina la cartera '
                '— es lo que amplificó el selloff de julio.</p>') if concentrated >= 3 else ""
        positions_html += f'<div class="exposure">{bars}</div>{warn}'
    else:
        positions_html = '<p class="muted">Sin posiciones abiertas — la cuenta está en efectivo.</p>'

    # Trades cerrados (más recientes primero, hasta 15)
    trs = ""
    for t in sorted(rt["trades"], key=lambda x: x["time"], reverse=True)[:15]:
        trs += (f'<tr><td class="dim">{html.escape(t["time"][:10])}</td>'
                f'<td class="sym">{html.escape(t["symbol"])}</td>'
                f'<td class="num">{t["qty"]:g}</td>'
                f'<td class="num">${t["entry"]:,.2f}</td>'
                f'<td class="num">${t["exit"]:,.2f}</td>'
                f'<td class="num {_cls(t["pl"])}">${_fmt_money(t["pl"], sign=True)}</td></tr>')
    trades_html = (f'<table><thead><tr><th>Fecha</th><th>Símbolo</th><th class="num">Qty</th>'
                   f'<th class="num">Entrada</th><th class="num">Salida</th><th class="num">P/L</th>'
                   f'</tr></thead><tbody>{trs}</tbody></table>') if trs else '<p class="muted">Aún no hay trades cerrados.</p>'

    # Timeline de decisiones
    tl = ""
    for d in m["recent"]:
        badge = "entrada" if d["kind"] == "entrada" else "monitor"
        tl += (f'<li><span class="tl-time">{html.escape(d["time"])}</span>'
               f'<span class="tl-badge {badge}">{html.escape(d["symbol"])}</span>'
               f'<span class="tl-detail">{html.escape(d["detail"])}</span></li>')
    timeline_html = f'<ul class="timeline">{tl}</ul>' if tl else '<p class="muted">Sin decisiones registradas.</p>'

    ret_cls = _cls(m["total_return_pct"])

    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Agent · Dashboard</title>
<style>
  :root {{
    --bg:#0e1116; --surface:#161b22; --surface2:#1c232d; --line:#2a3340;
    --ink:#e6edf3; --muted:#8b98a6; --accent:#58a6ff;
    --pos:#3fb950; --neg:#f85149; --flat:#8b98a6; --warnc:#d29922;
    --pos-fill:rgba(63,185,80,.14); --neg-fill:rgba(248,81,73,.14);
    --mono:"SFMono-Regular",ui-monospace,"Cascadia Code",Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg:#f6f8fa; --surface:#ffffff; --surface2:#f0f3f6; --line:#d8dee4;
      --ink:#1f2328; --muted:#59636e; --accent:#0969da;
      --pos:#1a7f37; --neg:#cf222e; --flat:#59636e; --warnc:#9a6700;
      --pos-fill:rgba(26,127,55,.10); --neg-fill:rgba(207,34,46,.10);
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
    line-height:1.5; padding:28px 18px 60px; }}
  .wrap {{ max-width:900px; margin:0 auto; display:flex; flex-direction:column; gap:24px; }}
  header .eyebrow {{ font-family:var(--mono); font-size:12px; letter-spacing:.12em;
    text-transform:uppercase; color:var(--accent); margin:0; }}
  header h1 {{ font-size:22px; margin:4px 0 2px; }}
  header .gen {{ color:var(--muted); font-size:13px; margin:0; }}
  .hero {{ display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-top:8px; }}
  .hero .big {{ font-family:var(--mono); font-size:34px; font-weight:600; font-variant-numeric:tabular-nums; }}
  .hero .ret {{ font-family:var(--mono); font-size:18px; font-weight:600; }}
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(158px,1fr)); gap:10px; }}
  .tile {{ background:var(--surface); border:1px solid var(--line); border-radius:9px; padding:12px 14px; }}
  .tile .k {{ font-family:var(--mono); font-size:10.5px; letter-spacing:.09em; text-transform:uppercase; color:var(--muted); }}
  .tile .v {{ font-family:var(--mono); font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; margin-top:3px; }}
  .tile .sub {{ font-size:11.5px; color:var(--muted); margin-top:2px; }}
  .pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }} .flat {{ color:var(--flat); }}
  section {{ background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:18px 20px; }}
  section h2 {{ font-size:13px; font-family:var(--mono); letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); margin:0 0 14px; }}
  .curve {{ width:100%; height:200px; display:block; }}
  .baseline {{ stroke:var(--muted); stroke-width:1; stroke-dasharray:4 4; opacity:.5; }}
  table {{ width:100%; border-collapse:collapse; font-size:13.5px; }}
  .tablewrap {{ overflow-x:auto; }}
  th {{ text-align:left; font-family:var(--mono); font-size:10.5px; letter-spacing:.06em; text-transform:uppercase;
    color:var(--muted); font-weight:600; padding:7px 10px; border-bottom:1px solid var(--line); }}
  td {{ padding:7px 10px; border-bottom:1px solid var(--line); }}
  tr:last-child td {{ border-bottom:none; }}
  .num {{ text-align:right; font-family:var(--mono); font-variant-numeric:tabular-nums; }}
  .sym {{ font-family:var(--mono); font-weight:600; }}
  .dim {{ color:var(--muted); font-family:var(--mono); font-size:12px; }}
  .prot {{ font-family:var(--mono); font-size:11.5px; color:var(--muted); }}
  .be {{ color:var(--accent); }}
  .warn {{ color:var(--neg); font-weight:600; }}
  .warnc {{ color:var(--warnc); }}
  .warn-txt {{ margin-top:10px; font-size:12.5px; }}
  .muted {{ color:var(--muted); font-size:14px; margin:0; }}
  .exposure {{ margin-top:14px; display:flex; flex-direction:column; gap:5px; }}
  .exp-row {{ display:flex; align-items:center; gap:9px; font-size:12.5px; }}
  .exp-label {{ font-family:var(--mono); font-size:11.5px; color:var(--muted); min-width:82px; }}
  .exp-bar {{ flex:1; height:8px; background:var(--surface2); border-radius:4px; overflow:hidden; }}
  .exp-fill {{ display:block; height:100%; background:var(--accent); border-radius:4px; }}
  .exp-fill.heavy {{ background:var(--neg); }}
  .exp-n {{ font-family:var(--mono); font-size:11.5px; color:var(--muted); min-width:14px; text-align:right; }}
  .timeline {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:6px; }}
  .timeline li {{ display:flex; gap:10px; align-items:baseline; font-size:13px; padding:5px 0;
    border-bottom:1px solid var(--line); }}
  .timeline li:last-child {{ border-bottom:none; }}
  .tl-time {{ font-family:var(--mono); font-size:11.5px; color:var(--muted); white-space:nowrap; }}
  .tl-badge {{ font-family:var(--mono); font-size:11px; font-weight:600; padding:1px 6px; border-radius:4px;
    background:var(--surface2); border:1px solid var(--line); white-space:nowrap; }}
  .tl-detail {{ color:var(--ink); }}
  footer {{ text-align:center; color:var(--muted); font-size:12px; }}
  a {{ color:var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <p class="eyebrow">Trading Agent · Paper</p>
    <h1>Dashboard de rendimiento</h1>
    <p class="gen">Actualizado {html.escape(gen_h)} · se regenera en cada corrida del agente</p>
    <div class="hero">
      <span class="big">${_fmt_money(m["equity"])}</span>
      <span class="ret {ret_cls}">{m["total_return_pct"]:+.2f}% (${_fmt_money(m["total_pl"], sign=True)})</span>
    </div>
  </header>

  <div class="tiles">{''.join(tiles)}</div>

  <section>
    <h2>Curva de equity · 1 mes</h2>
    {_equity_svg(m["curve"])}
  </section>

  <section>
    <h2>Posiciones abiertas</h2>
    <div class="tablewrap">{positions_html}</div>
  </section>

  <section>
    <h2>Trades cerrados (recientes)</h2>
    <div class="tablewrap">{trades_html}</div>
  </section>

  <section>
    <h2>Decisiones recientes</h2>
    {timeline_html}
  </section>

  <footer>Generado por report/build_dashboard.py · datos de Alpaca (paper) + journal · sin capital real</footer>
</div>
</body>
</html>'''
