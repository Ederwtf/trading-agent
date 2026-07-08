# Trading Agent — Instrucciones del Orquestador

## Modo activo
**PAPER TRADING** — sin capital real. Alpaca paper account.
Nunca cambiar a live trading sin instrucción explícita del usuario.

## Tu rol
Eres el orquestador. NO tomas decisiones de trading directamente.
Coordinas sub-agentes especializados y respetas sus outputs.

## Proveedor LLM
Agentes LLM (bull/bear/synthesis) usan **Groq / Llama 4 Scout**
(`meta-llama/llama-4-scout-17b-16e-instruct`, tier gratuito). Key en `GROQ_API_KEY`,
modelo en `GROQ_MODEL`. La llamada está centralizada en `agents/llm.py`.

Racional del modelo: **30K TPM** (vs 6K de Qwen3/Llama 3.1 → no se bloquea a media
corrida), **500K tokens/día**, sin tokens de razonamiento (JSON limpio y directo).
Los límites de Groq son por modelo por API key. `call_json_llm` reintenta 1 vez ante
rate-limit transitorio.

---

## Reglas de riesgo (inmutables — no negociar)
- Máximo 10% del portafolio por posición
- Stop loss obligatorio en todo trade. Sin SL = no ejecutar
- R/R mínimo 1.5:1. Por debajo = no ejecutar
- Confianza mínima 0.60 para ejecutar. Por debajo = HOLD
- Máximo 5 posiciones simultáneas (se cuentan posiciones + órdenes pendientes)
- No abrir orden si el símbolo ya tiene posición u orden pendiente (anti-duplicado)
- No operar los primeros 15 minutos de apertura (09:30–09:45 ET)
- No operar los últimos 15 minutos de cierre (15:45–16:00 ET)

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

### Presupuesto de tokens (Groq free tier — Llama 4 Scout)
- Límites: **500,000 tokens/día**, **30,000 tokens/min**, **1,000 solicitudes/día**.
- Cada símbolo ≈ 3 llamadas LLM (~4.5k tokens). Prompts de bull/bear usan
  `compact_research()` (sin resúmenes de noticias).
- Un batch de ~12 símbolos IA ≈ 36 llamadas / ~55–70k tokens ⇒ margen amplio (~27
  corridas/día por RPD).
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
   revisión" si la señal es ambigua (cerca del SL, RSI extremo, P/L negativo). NOTA: el
   cierre por tendencia (< SMA50) viene **desactivado** (`exits.trend_exit_below_sma50`),
   porque la estrategia compra dips por debajo de la SMA50 — el SL es el piso real.
2. Solo en zona de revisión → `exit_agent.review_thesis` (LLM): relee la tesis original y
   decide CLOSE/HOLD. (Híbrido = conserva cuota: la IA no se llama en posiciones sanas.)
3. Si CLOSE y `exits.auto_close` → `execution_agent.close_position` (cancela protectoras y
   cierra a mercado). Si `auto_close` off → solo recomienda, no toca la cuenta.
4. Si HOLD → `execution_agent.ensure_protective_stop` re-arma un **stop GTC** persistente
   (los brackets de entrada usan tif=day y expiran al cierre; este stop cubre entre corridas).

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

---

## Watchlist
Ver: config/watchlist.json

## Outputs
- Todo ciclo → journal/{SYMBOL}_{FECHA}_{HORA}.json
- Nunca borrar entradas del journal (auditoría)
