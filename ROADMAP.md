# Roadmap del trading-agent

> Actualizado: 2026-07-12 (Fase 6). La **Fase Final** es la migración a dinero real;
> todas las fases intermedias auditan, miden y mejoran el agente en paper.

## Completadas

- **Fase 1** — Migración de agentes LLM a Groq (gratuito) + fix UTF-8 Windows.
- **Fase 2** — Modo batch multi-símbolo: universo dinámico + filtro de calidad,
  conciencia de cartera (anti-duplicados), cupos (máx 5), ranking, bracket orders.
- **Fase 3** — Llama 4 Scout + pre-screen local (conservar cuota) + desempate por R/R.
- **Fase 4** — Gestor de salidas híbrido (reglas locales + juez LLM), auto-close,
  stop GTC persistente.
- **Fase 5** — Despliegue en GitHub Actions: modo `auto` router por sesión
  (regular/pre/post/closed), cierres extended-hours (limit + extended_hours).
- **Fase 6** — Auditoría de fundamentos → [docs/auditoria-2026-07.md](docs/auditoria-2026-07.md)
  (15 hallazgos: 2 críticos, 4 altos, 5 medios, 4 bajos; veredicto de decisiones base;
  triaje de herramientas externas y clases de activos).

## En curso / siguientes

- **Fase 7 — Fixes de la auditoría** (el usuario aprueba qué entra; orden sugerido en §6
  del reporte). **En curso** — aplicados el 2026-07-12: **C1** (modelo migrado a
  `openai/gpt-oss-120b` antes del apagado de Scout del 17-jul; `llm_budget` 12→5;
  verificado con llamada real), **B4** (workflow local restaurado), **C2+A2** (salidas
  OCO GTC broker-side + breakeven al +4% vía `ensure_exit_bracket`; las 5 posiciones
  en transición — stops viejos en pending_cancel por mercado cerrado, las OCO se
  colocan solas en la primera corrida del lunes), **A1** (reglas duras de salida con
  precio real de la posición, no el cierre diario) y **A3** (monitoreo aislado por
  símbolo). Pendientes: M5, M1/M2/M4/B3, A4 (alpaca-py), M3 (rollup) y el pacing de
  llamadas en `agents/llm.py` (seguimiento de C1 por el TPM de 8K).
- **Fase 8 — Observabilidad**: dashboard estático auto-generado en cada corrida
  (`journal/` + portfolio history de Alpaca → HTML en GitHub Pages): equity curve,
  posiciones y P/L, win rate, timeline de decisiones, errores, uso de cuota LLM.
- **Fase 9 — Estrategia estable pero dinámica**: módulo de régimen de volatilidad
  (VIX vía yfinance + vol realizada + SPY vs SMA200 → calm|nervous|panic → ajusta tamaño,
  entradas y llm_budget) + ETFs líquidos en el universo + tope de concentración por sector.
- **Fase 10 — Según evidencia del dashboard**: cripto 24/7 (misma cuenta Alpaca),
  contexto macro (calendario FOMC/FRED), COT del CFTC.
- **Fase 11 — Memoria semántica (Knowledge Adapter, etapa 1)**: job semanal *offline*
  (fuera del cron de trading) que destila el journal + métricas en notas de conocimiento
  Markdown (`knowledge/`: lecciones, regímenes, estrategias) con la convención wiki
  (índice, log, wikilinks, frontmatter) — legible en Obsidian como visor. Regla dura:
  las estadísticas se calculan con código local; el LLM solo redacta alrededor de números
  verificables, con umbral mínimo de evidencia (n≥20 trades por afirmación). Requiere las
  métricas de F8 (y las etiquetas de régimen de F9 lo enriquecen). Solo consumo humano.
- **Fase 12 — Knowledge Adapter etapa 2 (RAG, opcional)**: el agente recupera notas
  relevantes como *contexto* para bull/bear (nunca como reglas duras automáticas).
  Se decide con evidencia de la etapa 1, antes de la Fase Final.
- **Fase Final — Dinero real**: VPS, LLM de pago, feed SIP ($99/mes), notificaciones
  (Telegram/email), hardening, keepalive del repo.

## Descartes justificados (no reabrír sin nueva evidencia)

- **Forex**: Alpaca no lo soporta; requeriría segundo broker (OANDA) y segundo execution
  agent. Cripto cubre el horario extendido a costo ~cero.
- **CME CVOL / LME / USDA WASDE**: datos de pago y/o de futuros, fuera del universo actual.
- **Investing.com / TradingView / Koyfin como fuentes del agente**: sin API pública viable —
  quedan como herramientas de análisis manual del usuario.
- **VPS antes de dinero real**: con salidas OCO broker-side, la cadencia irregular de
  Actions deja de ser un riesgo de seguridad; el VPS no paga hasta la Fase Final.
