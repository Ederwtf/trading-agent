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
  símbolo). Aplicados el 2026-07-23: **M5** (exit code ≠ 0 en fallos críticos →
  Actions rojo + email), **M4** (protección persistida en `state.json` + breakeven
  como trinquete que ya no revierte — arregla el flapping 196.96↔180 visto en NVDA),
  **M1** (validación contra precio vivo + dimensionado con precio real), **M2**
  (ventanas de no-operar apertura/cierre vía calendario de Alpaca, solo entradas) y
  **B3** (timestamps tz-aware ET). Aplicado el 2026-07-23: **A4** — migración del SDK
  deprecado `alpaca-trade-api` al oficial **alpaca-py**, con todo el acceso al broker
  centralizado en el nuevo `agents/broker.py` (única frontera con el SDK; habilita
  opciones/cripto y un futuro segundo broker sin tocar la lógica de los agentes).
  Aplicado el 2026-07-27: **fix de fiabilidad LLM** — gpt-oss-120b truncaba el JSON
  (~1 de 3 con prompts reales) y tumbaba runs del workflow vía M5; fix con
  `reasoning_effort="low"` + `max_tokens=1200` + retry ante BadRequestError/JSON inválido,
  más pacing proactivo entre llamadas (seguimiento de C1). **F7 COMPLETA.**
  Pendiente movido a F8: M3 (rollup del journal).

  > Nota de operación (2026-07-27): NVDA cerró en su stop de breakeven a $197.02
  > (entrada $196.9635) — salida en breakeven casi exacta; el trinquete M4/A2 funcionó.
  > Los fallos del workflow del 24 y 27 de julio eran el JSON de gpt-oss (arriba), no A4.

  > Observación de producción (07-13 → 07-17): los 4 semis tocaron sus stops de
  > breakeven durante el selloff pero **llenaron 3–9% por debajo del trigger** por
  > gaps a la baja (un stop es orden de mercado al dispararse). NVDA sobrevivió (+6%).
  > Cuenta: 100K → pico 102.5K → ~99.1K. Aprendizaje: un stop en breakeven no garantiza
  > salida en breakeven; en activos con gaps, considerar (futuro) stops-limit o reducir
  > exposición por nombre. Candidato para el Knowledge Adapter (F11).
- **Fase 8 — Observabilidad (COMPLETA 2026-07-27)**: dashboard estático auto-generado en
  cada corrida (`report/build_dashboard.py` + `report/render.py` → `docs/index.html`) desde
  `journal/` + cuenta/portfolio-history/fills de Alpaca: equity curve (SVG), P/L realizado
  por round-trip FIFO (validado: −$1,312 = cambio de equity), win rate, posiciones con
  protección, timeline de decisiones, uso estimado de IA. Autocontenido (CSP-safe), servido
  por GitHub Pages. Incluye **M3** (rollup del journal a `journal/archive/AAAA-MM.jsonl`) y el
  **prune de protección huérfana** en `state.json`. Wiring: pasos `if: always()` en el
  workflow (dashboard/commit corren aun si el orquestador falla). **Acción manual pendiente
  del usuario:** habilitar GitHub Pages (Settings → Pages → branch `master` /docs).
- **Fase 9 — Estrategia estable pero dinámica (COMPLETA 2026-07-28)**:
  `agents/regime.py` (SIN LLM) clasifica el mercado en `calm|nervous|panic` con vol realizada
  de SPY, SPY vs SMA200, drawdown 60d y VIXY vs su media — todo vía `broker.daily_bars`, **sin
  yfinance** (el proyecto ya lo había descartado por throttling). Umbrales calibrados con la
  distribución real del último año. Política: `nervous` recorta tamaño a 3.5% y sube la
  confianza mínima a 0.70; `panic` suspende entradas (las salidas siguen siempre).
  Además: **ETFs de sectores diversos** (SPY, QQQ, XLE, XLF, GLD) en el universo, **tope de 3
  posiciones por sector** (mapa explícito en config; Alpaca no expone sector), `llm_budget`
  convertido en **tope duro que incluye a los curados** (evita 39 llamadas con 13 símbolos), y
  **régimen + exposición sectorial en el dashboard**.
- **Fase 9.5 — Calidad de las decisiones (COMPLETA 2026-08-03)**. Revisión de la primera
  semana completa con F9: infraestructura impecable (50/50 runs success) y la diversificación
  funcionó (de 4/4 semis a 5 posiciones en 4 sectores), pero el rendimiento destapó dos
  fallos estructurales:
  1. **El juez LLM liquidaba en los dips.** El 29-jul cerró 11 posiciones con la razón
     "precio bajo SMA20/50, RSI sobrevendido" — exactamente la condición de entrada de la
     estrategia (la regla local equivalente ya estaba desactivada desde F4, pero al juez
     nadie se lo había dicho). Fix: el prompt ahora explica la estrategia, exige evidencia
     de tesis rota real y sesga a HOLD ante la duda. Verificado con replay de los 5 casos
     reales: **5/5 ahora mantienen**, y sigue cerrando ante catalizador cancelado o fraude.
  2. **El breakeven al 4% convertía ganadores en scratch** (+$1.16 de ganancia promedio vs
     −$69 de pérdida; win rate 13%). Fix: escalera breakeven (6% + colchón) → trailing (10%,
     gap 6%), con guardrail de stop nunca por encima del precio vivo.

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
