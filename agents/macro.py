"""
Macro Agent — Contexto macroeconómico. SIN LLM. Determinístico.

Dos señales, ambas de fuentes públicas y gratuitas (sin credenciales nuevas):

1. **Calendario FOMC** (fechas oficiales de la Fed, en `config/watchlist.json → macro.fomc`).
   El día del anuncio la volatilidad se dispara y el mercado suele girar en minutos: se
   suspenden las ENTRADAS nuevas (las salidas se gestionan siempre, como en todo el sistema).
   Precedente medido: el 2026-07-29 —segundo día de la reunión de julio— el agente cerró 11
   posiciones en pleno dip. Saber que era día de FOMC habría cambiado la lectura.

2. **COT del CFTC** (Commitments of Traders, API pública Socrata). Posicionamiento neto de
   los non-commercials en el E-MINI S&P 500. Señal SEMANAL y lenta → se registra como
   CONTEXTO en el journal, nunca bloquea ni dispara órdenes por sí sola. Es el sustituto
   gratuito de Barchart COT (que solo revende este mismo dato público).

Degradación segura: si una fuente falla, se devuelve el resto y `allow_entries=True` — una
caída de red no debe bloquear al agente ni dejarlo ciego.
"""

from datetime import date, datetime, timedelta

import requests

_COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
_COT_MARKET = "E-MINI S&P 500"


def _parse_days(entry) -> list:
    """Una entrada del calendario ('2026-07-28..2026-07-29' o '2026-07-29') → [date,...]."""
    out = []
    try:
        if ".." in entry:
            a, b = entry.split("..")
            d0 = date.fromisoformat(a.strip())
            d1 = date.fromisoformat(b.strip())
            while d0 <= d1:
                out.append(d0)
                d0 += timedelta(days=1)
        else:
            out.append(date.fromisoformat(entry.strip()))
    except Exception:
        pass
    return out


def fomc_context(cfg: dict, today: date = None) -> dict:
    """Contexto del calendario FOMC: ¿hoy es día de anuncio? ¿cuántos días faltan?

    El "día del anuncio" es el ÚLTIMO día de cada reunión (las de 2026 son todas de 2 días).
    """
    today = today or date.today()
    meetings = cfg.get("fomc", [])
    days_all, announce_days = [], []
    for m in meetings:
        ds = _parse_days(m)
        if ds:
            days_all.extend(ds)
            announce_days.append(ds[-1])       # último día = anuncio

    if not announce_days:
        return {"in_meeting": False, "announcement_today": False, "days_to_next": None,
                "next_meeting": None, "calendar_ok": False}

    future = sorted(d for d in announce_days if d >= today)
    stale = not future                          # calendario agotado → hay que actualizarlo
    return {
        "in_meeting":          today in days_all,
        "announcement_today":  today in announce_days,
        "days_to_next":        (future[0] - today).days if future else None,
        "next_meeting":        future[0].isoformat() if future else None,
        "calendar_ok":         not stale,
    }


def cot_positioning(market: str = _COT_MARKET, timeout: int = 8) -> dict:
    """Posicionamiento neto de non-commercials (COT del CFTC). {} si no disponible.

    Semanal (datos del martes, publicados el viernes). Solo contexto: net/OI negativo =
    los especuladores están netos cortos en el índice.
    """
    try:
        r = requests.get(_COT_URL, params={"$limit": 400, "$order": "report_date_as_yyyy_mm_dd DESC"},
                         timeout=timeout)
        r.raise_for_status()
        rows = r.json()
        want = market.upper()
        # Preferir el contrato EXACTO; si no aparece, aceptar una coincidencia parcial pero
        # nunca los mini/micro (que arrastran el mismo nombre y confundirían la señal).
        candidates = [x for x in rows if (x.get("contract_market_name") or "").upper().strip() == want]
        if not candidates:
            candidates = [x for x in rows
                          if want in (x.get("contract_market_name") or "").upper()
                          and "MICRO" not in (x.get("contract_market_name") or "").upper()]
        for row in candidates:
            long_ = float(row["noncomm_positions_long_all"])
            short = float(row["noncomm_positions_short_all"])
            oi    = float(row["open_interest_all"]) or 1.0
            net   = long_ - short
            return {
                "market":     row.get("contract_market_name"),
                "report_date": (row.get("report_date_as_yyyy_mm_dd") or "")[:10],
                "net":        int(net),
                "net_pct_oi": round(net / oi * 100, 1),
                "bias":       "corto" if net < 0 else "largo",
            }
    except Exception as e:
        print(f"  [macro] COT no disponible ({e})")
    return {}


def macro_context(cfg: dict = None, today: date = None) -> dict:
    """Contexto macro completo + si se permiten entradas. SIN LLM.

    cfg: bloque `macro` de config/watchlist.json.
    Devuelve {allow_entries, reasons, fomc, cot}.
    """
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return {"allow_entries": True, "reasons": ["macro desactivado"], "fomc": {}, "cot": {}}

    fomc = fomc_context(cfg, today)
    reasons = []
    allow = True

    if cfg.get("block_on_fomc", True) and fomc.get("announcement_today"):
        allow = False
        reasons.append(f"día de anuncio del FOMC ({fomc.get('next_meeting') or 'hoy'})")
    elif fomc.get("days_to_next") is not None:
        reasons.append(f"próximo FOMC en {fomc['days_to_next']} día(s)")
    if not fomc.get("calendar_ok", True):
        reasons.append("CALENDARIO FOMC DESACTUALIZADO — actualizar config/watchlist.json")

    cot = cot_positioning() if cfg.get("cot_enabled", True) else {}
    if cot:
        reasons.append(f"COT non-comm neto {cot['bias']} ({cot['net_pct_oi']:+.1f}% del OI, "
                       f"{cot['report_date']})")

    return {"allow_entries": allow, "reasons": reasons or ["sin eventos macro"],
            "fomc": fomc, "cot": cot}
