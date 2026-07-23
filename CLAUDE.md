# Trading Agent — Instrucciones del Orquestador

## Modo activo
**PAPER TRADING** — sin capital real. Alpaca paper account.
Nunca cambiar a live trading sin instrucción explícita del usuario.

## Tu rol
Eres el orquestador. NO tomas decisiones de trading directamente.
Coordinas sub-agentes especializados y respetas sus outputs.

## Proveedor LLM
Agentes LLM (bull/bear/synthesis) usan **Groq / gpt-oss-120b**
(`openai/gpt-oss-120b`, tier gratuito). Key en `GROQ_API_KEY`, modelo en `GROQ_MODEL`.
La llamada está centralizada en `agents/llm.py`.

Racional: es el reemplazo recomendado por Groq tras la **deprecación de Llama 4 Scout
(apagado 2026-07-17)**; el mejor balance calidad/límites del free tier sobreviviente.
Límites: **8K TPM / 200K TPD / 1K RPD** → `llm_budget` bajado a 5 (cada símbolo ≈ 3
llamadas ≈ 4.5K tokens; más símbolos por corrida reventarían el TPM). Pendiente F7:
pacing proactivo entre llamadas en `agents/llm.py`. Los límites de Groq son por modelo
por API key. `call_json_llm` reintenta 1 vez ante rate-limit transitorio.

---

## Reglas de riesgo (inmutables — no negociar)
- Máximo 10% del portafolio por posición
- Stop loss obligatorio en todo trade. Sin SL = no ejecutar
- R/R mínimo 1.5:1. Por debajo = no ejecutar
- Confianza mínima 0.60 para ejecutar. Por debajo = HOLD
- Máximo 5 posiciones simultáneas (se cuentan posiciones + órdenes pendientes)
- No abrir orden si el símbolo ya tiene posición u orden pendiente (anti-duplicado)
- Validación contra precio VIVO antes de ejecutar (M1): se descarta la entrada si el
  precio real ya cruzó el SL o el TP propuestos (evita rechazos de Alpaca y entradas sin
  colchón). El dimensionado usa el precio vivo, no el `entry` del LLM.
- No operar en la ventana de apertura/cierre (M2): por defecto los primeros y últimos 15
  min de la sesión regular (`no_trade_open_min`/`no_trade_close_min` en watchlist). Se
  calcula con el calendario de Alpaca → correcto en DST y cierres tempranos. **Solo bloquea
  ENTRADAS**; las salidas se permiten en todo momento.

---

## Pipeline completo (nuevas posiciones)
Usar cuando: símbolo sin posición abierta, o primer ciclo del día.

```
python orchestrator.py full SYMBOL   # UN símbolo: analiza + ejecuta
python orchestrator.py full          # BATCH: universo amplio + ranking + cupos
```

Orden de ejecución:
1. universe.py     → construye el universo (fijos + most-actives de Alpaca; solo batch)
2. research.py     → datos crudos (sin LLM, obligatorio primero)
3. bull_agent.py   → caso alcista (LLM, temperatura 0.3)
4. bear_agent.py   → destruye la tesis (LLM, temperatura 0.3)
5. synthesis_agent.py → árbitro y decisión final (LLM, temperatura 0.2)
6. risk_agent.py   → validación de reglas (sin LLM, no modificar)
7. execution_agent.py → ejecución en Alpaca vía **bracket order** (entrada + TP + SL)

### Modo BATCH (`full` sin símbolo)
- Universo = watchlist fija (`config/watchlist.json` → `symbols`) + most-actives de
  Alpaca, deduplicado y cortado en `analysis_cap`.
- **Filtro de calidad** sobre los candidatos dinámicos (los fijos se confían):
  descarta precio < `dynamic_universe.min_price` (penny stocks) y ETFs
  apalancados/inversos por nombre (p. ej. SOXS = Semiconductor Bear 3X). Los fijos
  no se filtran.
- Lee el estado real de la cuenta: **salta** símbolos ya en cartera o con orden
  pendiente (los monitorea en su lugar).
- Rankea los candidatos BUY por confianza y ejecuta solo hasta llenar los cupos
  libres = `max_positions` − (posiciones + pendientes).
- Solo entradas BUY (no shorts).

### Pre-screen local (conservar cuota de IA)
- Antes de invocar la IA, `agents/screen.py` corre un filtro **local** (sin LLM) sobre
  los datos de research: descarta no-candidatos (p. ej. sobrecompra extrema) y ordena el
  resto por score. Solo los mejores hasta `llm_budget` gastan las 3 llamadas LLM.
- Los símbolos **curados** (`symbols`) siempre son elegibles; los **dinámicos** deben
  pasar el filtro y compiten por score por el presupuesto restante.
- Objetivo: no gastar las **1,000 solicitudes/día (RPD)** ni el TPM en símbolos que la
  lógica local ya puede descartar.

### Presupuesto de tokens (Groq free tier — gpt-oss-120b)
- Límites: **200,000 tokens/día**, **8,000 tokens/min**, **1,000 solicitudes/día**.
- Cada símbolo ≈ 3 llamadas LLM (~4.5k tokens). Prompts de bull/bear usan
  `compact_research()` (sin resúmenes de noticias).
- Un batch de 5 símbolos IA (llm_budget=5) ≈ 15 llamadas / ~25k tokens ⇒ ~8 batches
  de entrada/día por TPD; el TPM de 8K es el límite operativo real (ver pendiente F7:
  pacing entre llamadas).
- Si topas un límite (error 429), `call_json_llm` reintenta 1 vez; si persiste, espera
  al reset o baja `llm_budget`.

## Pipeline ligero = GESTOR DE SALIDAS (posiciones abiertas)
Usar cuando: posición ya abierta. También lo llama el batch para símbolos ya en cartera.

```
python orchestrator.py monitor [SYMBOL]
```

Por cada posición abierta (lee la posición real de Alpaca + la tesis/niveles del último
`journal/` de entrada):
1. `exit_agent.local_exit` (SIN LLM): cierra si el precio cruza SL o TP; marca "zona de
   revisión" si la señal es ambigua (cerca del SL, RSI extremo, P/L negativo). Las reglas
   duras usan el **precio real de la posición** (real-time, incluye extended hours), no el
   de research (velas diarias = viejo en pre/post) — fix A1 de la auditoría. NOTA: el
   cierre por tendencia (< SMA50) viene **desactivado** (`exits.trend_exit_below_sma50`),
   porque la estrategia compra dips por debajo de la SMA50 — el SL es el piso real.
2. Solo en zona de revisión → `exit_agent.review_thesis` (LLM): relee la tesis original y
   decide CLOSE/HOLD. (Híbrido = conserva cuota: la IA no se llama en posiciones sanas.)
3. Si CLOSE y `exits.auto_close` → `execution_agent.close_position` (cancela protectoras y
   cierra a mercado). Si `auto_close` off → solo recomienda, no toca la cuenta.
4. Si HOLD → `execution_agent.ensure_exit_bracket` mantiene una pareja **OCO GTC**
   broker-side (TP limit + SL stop; una cancela a la otra) — fix C2: TP y SL se ejecutan
   al instante en horario regular sin depender de la cadencia del cron. **Breakeven**
   (fix A2): si el P/L ≥ `exits.breakeven_at_pct` (default 4%), el stop sube al precio de
   entrada y nunca baja. Idempotente: solo cancela/re-coloca si los precios deseados
   cambiaron. Con mercado cerrado la cancelación queda `pending_cancel` y la OCO se
   coloca en la próxima corrida (sin pérdida real: fuera de horario regular los stops
   tampoco disparan). Fallback sin TP conocido: `ensure_protective_stop` (stop GTC solo).
   **Estado de protección persistente (M4):** los SL/TP vigentes por símbolo se guardan en
   `journal/state.json` (`protection`) al ejecutar la entrada y en cada monitoreo. El
   breakeven es un **trinquete**: el stop deseado nunca baja del nivel ya persistido, así
   que aunque el P/L retroceda bajo el umbral el stop no revierte (esto elimina el flapping
   196.96↔180 que se observó en NVDA). Resolución de niveles: `state.json` → journal →
   OCO/stop abierta en Alpaca (último recurso). Al cerrar, el estado del símbolo se limpia.

El loop de monitoreo está **aislado por símbolo** (fix A3): un error de research/Alpaca
en un símbolo no deja al resto sin gestión en esa corrida.

Cerrar una posición libera un cupo para el próximo `full` (batch).

En `pre`/`post` (extended hours) los cierres usan **limit + extended_hours=true** (Alpaca no
permite market ni dispara stops fuera de hora); en `regular` cierra a mercado.

---

## Modo AUTO (para GitHub Actions)
```
python orchestrator.py auto
```
Router por sesión usando el reloj/calendario de Alpaca (a prueba de DST y feriados):
- `regular` → `run_batch` (monitorea abiertos + busca entradas en cupos libres).
- `pre`/`post` → solo gestor de salidas sobre posiciones abiertas (limit + extended_hours).
- `closed` → no-op.

Reglas de operación por sesión:
- **Entradas: solo en `regular`** (bracket market).
- **Salidas: en las 3 sesiones.** RPD conservado: `run_batch` salta la fase de IA si no hay
  cupos, y las entradas se buscan a lo más cada `entry_min_gap_min` (estado en `journal/state.json`).

## Despliegue (GitHub Actions)
- Workflow: `.github/workflows/trading.yml` — cron `*/15 * * * 1-5` (UTC) + `workflow_dispatch`.
- Secrets del repo: `GROQ_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (el resto va como env).
- El workflow **commitea `journal/`** al final (persiste estado entre runners efímeros).
- `.env` está en `.gitignore` — las keys NUNCA van al repo.

---

## Si algo falla
- Error en cualquier agente → detener pipeline, registrar en journal/, NO ejecutar
- Error de conexión con Alpaca → esperar 60s, reintentar una vez, luego detener
- JSON inválido de agente LLM → reintentar con temperatura 0.1, luego HOLD
- **Exit code (M5):** un fallo por-símbolo se aísla y la corrida sigue (verde), PERO un
  fallo CRÍTICO — Alpaca inaccesible, o TODAS las salidas / TODO el análisis IA fallan, o
  un crash global — hace `sys.exit(1)` para que Actions marque el run en rojo y llegue el
  email. En modo `auto` se verifica el alcance de Alpaca antes de nada (si estuviera caído,
  `detect_session` devolvería "closed" y enmascararía la caída como mercado cerrado).

---

## Roadmap y auditoría
- Fases del proyecto: ver `ROADMAP.md` (Fases 1–6 completadas; la Fase Final = dinero
  real + VPS; las intermedias mejoran el agente en paper).
- Auditoría de fundamentos (2026-07): `docs/auditoria-2026-07.md` — hallazgos con
  severidad, veredicto de decisiones base y triaje de herramientas/activos.

## Watchlist
Ver: config/watchlist.json

## Outputs
- Todo ciclo → journal/{SYMBOL}_{FECHA}_{HORA}.json
- Nunca borrar entradas del journal (auditoría)
