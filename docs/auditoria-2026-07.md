# Auditoría de fundamentos — julio 2026 (Fase 6)

**Fecha:** 2026-07-12 · **Alcance:** decisiones base, arquitectura, comportamiento real
(2026-07-07 → 2026-07-10) · **Regla de la fase:** cero cambios de código; este documento
solo reporta. Los fixes se aprueban individualmente (Fase 7).

Evidencia usada: 159 archivos de `journal/`, cuenta paper de Alpaca consultada en vivo
(2026-07-12), 31 runs de GitHub Actions (API pública), código fuente, y documentación
oficial de Groq/Alpaca/GitHub consultada el 2026-07-12.

---

## 1. Resumen ejecutivo

El agente **funciona y gana** en paper: +2.55% de equity ($100,000 → $102,554) en 3 días
hábiles, 5/5 posiciones en verde, 0 fallos en 31 corridas de Actions, 0 errores en el
journal. La arquitectura híbrida (local primero, LLM solo cuando hace falta) cumple su
objetivo: solo 7 de 140 monitoreos gastaron llamadas LLM.

Pero hay **una bomba de tiempo con fecha**: Groq apaga Llama 4 Scout el **17 de julio de
2026** (5 días después de esta auditoría). Sin migrar el modelo, el "cerebro" del agente
muere ese día. Y hay **una ilusión estructural**: el cron de 15 minutos en realidad corre
cada 1–3.5 horas, así que toda lógica que asuma monitoreo frecuente (take-profit, protección
en extended hours) descansa sobre un supuesto falso. Ambas cosas tienen fix barato (ver §3).

## 2. Comportamiento real (2026-07-07 → 2026-07-10)

### Cuenta (consultada en vivo el 2026-07-12)
| Métrica | Valor |
|---|---|
| Equity | $102,554.53 (+2.55% desde $100,000) |
| Posiciones | AMD +8.7% · NVDA +7.1% · ARM +6.9% · AVGO +5.2% · MU +4.9% |
| P/L no realizado | +$2,554.54 |
| Protección | 5/5 posiciones con stop GTC activo (armados 2026-07-08) |
| Órdenes huérfanas / duplicadas | 0 |

### Operación
- **31 runs de Actions, 31 success** (28 cron + 2 manuales + 1 dependabot). Cero fallos de infra.
- **159 entradas de journal**: 140 monitor (126 HOLD "tesis intacta", 3 sin posición), 19 de
  entrada (7 ejecutadas, 9 sin cupo, 1 rechazo de Alpaca, 2 pruebas).
- **Uso de LLM en monitoreo: 7 de 140** (5%) — el pre-filtro local hace el 95% del trabajo.
  Idempotencia del stop GTC verificada: 121 veces "ya tiene stop activo", 0 duplicados.
- **Fills parciales manejados bien**: NVDA se llenó en 3 parciales (34+2+4), AVGO en 2,
  MU en 2 — las posiciones y brackets quedaron consistentes.
- **El gestor de salidas nunca ha cerrado una posición en producción** (todas sanas) — el
  camino de cierre (`close_position*`) solo se ha probado manualmente en Fase 4.

### Cadencia real del cron (hallazgo C2)
`*/15 * * * 1-5` debería dar ~96 corridas/día; GitHub entregó **12–13/día**:
- 2026-07-10: corridas a las 04:02, 07:17, 09:23, 11:43, 13:27, 14:59, 16:26, 17:33, 18:39, 19:37 ET.
- Huecos máximos: **195 min** (07-10), 160 min (07-09). Hueco típico en horario regular: 1.5–2 h.
- Es el comportamiento documentado de GitHub: el cron de Actions se ejecuta "best effort" con
  retrasos fuertes en horas cargadas. No es un bug nuestro y no va a mejorar en Actions.

## 3. Hallazgos

Formato: **ID · Severidad · Título** — evidencia → consecuencia → fix propuesto.

### CRÍTICOS

**C1 · Groq apaga Llama 4 Scout el 2026-07-17 (en días).**
Evidencia: console.groq.com/docs/deprecations (consultado 2026-07-12): deprecación anunciada
el 17-jun-2026, apagado el **17-jul-2026** para tiers free/developer; reemplazos recomendados
`openai/gpt-oss-120b` o `qwen/qwen3.6-27b`. El viejo fallback `llama-3.3-70b-versatile`
también muere (16-ago-2026), igual que `llama-3.1-8b-instant`.
Consecuencia: desde el 17-jul todas las llamadas LLM fallan → cero entradas nuevas y el juez
de salidas responde "juez no disponible" (degrada a HOLD + stops GTC; las posiciones quedan
protegidas solo en horario regular).
Fix propuesto (urgente, 2 líneas, sin tocar .py):
1. `trading.yml`: `GROQ_MODEL: openai/gpt-oss-120b` (mejor calidad del tier gratuito
   sobreviviente; JSON mode soportado; `_parse_json` ya tolera razonadores).
2. `watchlist.json`: `llm_budget: 12 → 5` — el reemplazo tiene **8K TPM / 200K TPD / 1K RPD**
   (vs 30K/500K/1K de Scout). Con ~4.5K tokens por símbolo, 12 símbolos reventarían el TPM.
Seguimiento (F7): pacing real en `agents/llm.py` (espera proactiva entre llamadas, backoff
con reintentos >1), probar `reasoning_effort` bajo, y validar 1 llamada con el modelo nuevo.
Nota: en la tabla free vigente, las alternativas son `qwen/qwen3.6-27b` (8K TPM/200K TPD),
`qwen/qwen3-32b` (6K TPM pero 500K TPD y 60 RPM) y `groq/compound` (70K TPM pero 250 RPD y
comportamiento agéntico no determinista). `gpt-oss-120b` es el mejor balance calidad/límites.

**C2 · El supuesto "monitoreo cada 15 min" es falso → TP y protección extended dependen de un cron que corre cada 1–3.5 h.**
Evidencia: §2 (cadencia). Además: los brackets de entrada usan `tif=day`, así que las piernas
TP/SL originales **expiraron el 2026-07-08 al cierre**; hoy el SL vive como stop GTC
(broker-side ✓) pero **el take-profit solo existe dentro de `local_exit`**, que corre cuando
el cron quiere. En extended hours ni siquiera el stop GTC dispara (limitación de Alpaca), y
la "protección activa por monitoreo frecuente" del diseño de Fase 5 no existe con huecos de 3 h.
Consecuencia: un spike que toque el TP entre corridas no se captura; un desplome en post-market
puede tardar horas en ser visto.
Fix propuesto (F7, el de mayor valor): **salidas broker-side con OCO GTC** — al armar
protección, colocar orden OCO (TP limit + SL stop, ambas GTC) en vez de solo stop. El broker
ejecuta TP y SL al instante 24/5 en regular (y el limit del TP sí puede operar en extended si
se marca), y el cron queda para lo que sí tolera retraso: revisar tesis y journalizar.
Esto además elimina la principal razón para pagar un VPS *ahora*.

### ALTOS

**A1 · En pre/post, las reglas duras de salida evalúan con precio viejo.**
Evidencia: `local_exit` compara SL/TP contra `research["price"]["current"]`
([exit_agent.py:29-43](agents/exit_agent.py)), que sale de velas **diarias IEX** — en
extended hours ese "current" es el cierre de la sesión regular, no el precio de pre/post.
La posición (`position.current_price`, [orchestrator.py:422-437](orchestrator.py)) sí es
actual, pero solo la usa la regla blanda de P/L<-3% que enruta al juez LLM.
Consecuencia: un -10% nocturno no dispara "SL cruzado"; depende de la cadena frágil
P/L<-3% → juez LLM (que además muere con C1 si no se migra).
Fix: en `local_exit`, usar `position["current_price"]` para las reglas duras cuando exista.

**A2 · Stops fijos al nivel original — una posición +9% puede volverse -12%.**
Evidencia: cuenta en vivo — AMD +8.7% con stop GTC a $450 vs entrada $513 (-12.3%); igual
patrón en las 5 posiciones (stops del 2026-07-08 nunca movidos).
Consecuencia: sin trailing ni breakeven, todo el P/L flotante (+$2,554) es devolvible.
Fix (combina con C2): al armar la OCO/stop, si P/L ≥ +X% (p. ej. 4%), subir el stop a
breakeven (o trailing simple). Config en `watchlist.json → exits`.

**A3 · Un símbolo que falla aborta la gestión de salidas del resto.**
Evidencia: los loops de monitoreo no aíslan errores por símbolo:
[orchestrator.py:328-329](orchestrator.py) (batch) y [orchestrator.py:551-553](orchestrator.py)
(auto pre/post). Una excepción de research/Alpaca en el primer símbolo deja al resto sin
gestionar esa corrida (el try de `main` captura, pero ya se saltó todo).
Consecuencia: con huecos de cron de 2–3 h, una corrida perdida es mucho tiempo sin gestión.
Fix: `try/except` por símbolo en ambos loops (patrón que ya existe en el pre-screen,
[orchestrator.py:345-350](orchestrator.py)).

**A4 · SDK `alpaca-trade-api` deprecado desde 2023.**
Evidencia: PyPI/GitHub del SDK viejo (mantenido hasta fin de 2022); el oficial es `alpaca-py`
(guía de migración oficial en alpaca.markets/sdks/python/migration.html).
Consecuencia: funciona hoy, pero sin mantenimiento; y **bloquea el futuro**: los clientes de
opciones y cripto (Fases 9-10) viven en `alpaca-py`.
Fix (F7, el más invasivo — hacerlo con calma y con paper de por medio): migrar los 4 puntos
de contacto (`research`, `universe`, `execution_agent`, `orchestrator`) a `alpaca-py`.

### MEDIOS

**M1 · El risk agent valida contra números del propio LLM, no contra el precio vivo.**
Evidencia: caso CPOP 2026-07-07 — synthesis propuso entry/SL con datos de research; al enviar,
Alpaca rechazó: `stop_loss.stop_price must be <= base_price - 0.01` (el precio real ya estaba
bajo el SL). `validate_trade` ([risk_agent.py](agents/risk_agent.py)) solo revisa coherencia
interna (R/R, tamaño, confianza).
Consecuencia: sin daño (Alpaca rechaza), pero se desperdicia el candidato y el cupo del ciclo.
Fix: antes de ejecutar, validar `SL < precio_vivo < TP` con el último trade y descartar/reprecificar.

**M2 · Regla documentada que el código no aplica (ventanas de apertura/cierre).**
Evidencia: CLAUDE.md declara "inmutables" las ventanas de no-operar 09:30–09:45 y 15:45–16:00
ET, pero ningún módulo las implementa; fills reales de entrada a las **09:31–09:34 ET**
(2026-07-08, activities de Alpaca).
Consecuencia: contradicción doc↔código; entrar al minuto 1 de la apertura es entrar en el
momento más caótico del día.
Fix: decisión del usuario — implementar la ventana en `run_batch` (gate por hora del reloj
de Alpaca) **o** retirar la regla del CLAUDE.md. No dejar la mentira documentada.

**M3 · El journal crece sin límite dentro del repo.**
Evidencia: 159 archivos en 4 días de operación (~40/día ≈ 1,200/mes ≈ 14K/año).
Consecuencia: repo inflado, `load_latest_journal` cada vez más lento (glob + sort por símbolo),
clones lentos.
Fix: rollup mensual (mover a `journal/archive/AAAA-MM.zip` **conservando siempre los journals
de decisión** — ver M4) o rama `journal` aparte.

**M4 · La protección depende de que exista el journal de entrada.**
Evidencia: `run_light_pipeline` obtiene el SL de `load_latest_journal(symbol)`
([orchestrator.py:464-465,508](orchestrator.py)); si el archivo de decisión no está (rollup
mal hecho, clone nuevo, símbolo operado a mano), `ensure_protective_stop(None,...)` devuelve
"stop_price inválido" y la posición queda sin stop nuevo.
Consecuencia: acoplamiento frágil entre protección y archivos históricos de git.
Fix: persistir SL/TP vigentes en `journal/state.json` al ejecutar la entrada, o leer el stop
actual de las órdenes GTC abiertas como fallback.

**M5 · Fallos "suaves" invisibles.**
Evidencia: los errores del agente se imprimen y el proceso sale con código 0
([orchestrator.py:556-557](orchestrator.py)) → el run aparece verde; GitHub solo emailaría
en fallo del workflow.
Consecuencia: un agente medio-roto (p. ej. LLM caído post-17-jul) se ve "success" para siempre.
Fix rápido (F7): terminar con `sys.exit(1)` ante errores críticos (LLM irrecuperable, Alpaca
inaccesible) para que Actions marque el run rojo y llegue el email. Fix completo (F8): el
dashboard expone errores y "última corrida sana".

### BAJOS

**B1 · Auto-disable del cron a los 60 días sin commits.**
Evidencia: docs de GitHub (disabling-and-enabling-a-workflow): en repos con 60 días sin
actividad de commits, los workflows de cron se desactivan solos, con un único email fácil
de perder. Los commits del journal SÍ resetean el contador — mientras el bot commitee.
Consecuencia: si el trading se pausa >60 días (vacaciones, cuenta vaciada), el agente muere
en silencio. Mitigación: recordatorio, o keepalive mensual (más relevante en Fase Final).

**B2 · Feed IEX = fracción pequeña del volumen consolidado.**
Evidencia: plan gratuito de Alpaca = feed IEX en tiempo real (docs); IEX representa un
porcentaje bajo de un dígito del volumen total de mercado. El flag `unusual volume`
([research.py:103-108](agents/research.py), screen +1 punto) se calcula sobre ese subconjunto.
Consecuencia: señal de volumen ruidosa (el *ratio* sobre el mismo feed mitiga el sesgo, pero
no lo elimina). Aceptable en paper; en Fase Final considerar Algo Trader Plus (SIP, $99/mes).

**B3 · Timestamps naive (sin zona horaria).**
`datetime.now()` en journal/state depende de `TZ` del runner (hoy fijada a America/New_York).
Correcto hoy; frágil si alguien corre local con otra TZ (mezcla horas en el journal, y
`entries_due` compararía mal). Fix trivial en F7 si se toca ese código.

**B4 · Estado local de git peligroso (máquina del usuario).**
Evidencia: `git status` local tiene **staged el borrado de `.github/workflows/trading.yml`**,
que es el ÚNICO workflow y es el que funciona (idéntico al remoto, blob `8e27cdd`). El
`trading2.yml` ya no existe en el remoto — la limpieza que quedaba pendiente ya ocurrió.
Consecuencia: un `git commit` local despistado + push mataría el agente.
Fix (1 comando, lo corre el usuario o yo en F7):
`git restore --staged --worktree .github/workflows/trading.yml`

## 4. Decisiones base — veredicto

| Decisión | Veredicto | Notas |
|---|---|---|
| **Alpaca** como broker | ✅ Correcta, mantener | Única opción gratuita con paper + API decente + acciones/ETFs/cripto/opciones en la misma cuenta. IBKR: más mercados (futuros/forex) pero fricción y sin free tier de datos equiparable. Cambiar de broker hoy no paga. |
| **Paper trading** primero | ✅ Correcta | Ya reveló 15 hallazgos sin costar un dólar. |
| **Groq** como proveedor LLM | ✅ Correcta, pero el modelo debe migrar YA | Ver C1. Groq sigue siendo el free tier más generoso; el riesgo demostrado es la rotación de modelos (~1 mes de aviso). Plan B estructural (F7): `agents/llm.py` ya centraliza todo — hacer el proveedor configurable (base_url/env) para poder saltar a Gemini/OpenRouter free si Groq empeora. |
| **GitHub Actions** como infra | ✅ Correcta para paper | 31/31 success. Su límite real es la cadencia (C2); con salidas OCO broker-side, la cadencia deja de importar para la seguridad y solo modula la frecuencia de entradas. El VPS se pospone a la Fase Final sin pérdida. |
| **Arquitectura** (agentes + orquestador) | ✅ Sana | Separación LLM/no-LLM correcta; el patrón híbrido de salidas demostró conservar cuota (7/140). Deudas: A3 (aislamiento), M4 (acoplamiento journal↔protección), sin tests de la lógica local. |
| **Estrategia** (momentum + dips) | ⚠️ Temprano para juzgar | +2.55% en 3 días con viento a favor del sector. Muestra mínima; el dashboard (F8) y el régimen (F9) son los que permitirán evaluarla de verdad. Riesgo visible hoy: 5/5 posiciones en el MISMO sector (semis/AI) — cero diversificación; considerar tope por sector en F9. |

## 5. Herramientas externas y clases de activos (triaje solicitado)

**Corrección conceptual:** CME FedWatch/CVOL, Barchart COT, LME y USDA WASDE no son hedge
funds — son reportes, índices y bolsas que usan los traders (sobre todo de futuros).

| Herramienta | Veredicto | Razón |
|---|---|---|
| CME CVOL | ❌ | De pago, enfocada a futuros. Sustituto gratuito: **VIX + volatilidad realizada** (F9). |
| CME FedWatch | ⚠️ proxy | Sin API gratuita. Proxy: calendario FOMC + FRED (gratis). Decidir tras F8. |
| Barchart COT | ⚠️ vía CFTC | El dato original es público y gratis (CFTC, semanal). Señal de contexto. Decidir tras F8. |
| LME / USDA WASDE | ❌ | Metales/agrícolas de futuros; irrelevantes para el universo actual. |
| Investing.com / TradingView / Koyfin | 👤 humano | Sin API pública viable — herramientas tuyas de análisis manual, no del agente. |
| Yahoo Finance (`yfinance`) | ✅ agente | Gratis; fuente del VIX para F9. |
| Perplexity Finance | ⏳ Fase Final | API de pago (Sonar). |

| Clase de activo | Veredicto | Razón |
|---|---|---|
| ETFs | ✅ ya (F9) | Alpaca los opera idéntico a acciones. Solo watchlist/universo. También responden a la falta de diversificación (§4). |
| Cripto | ✅ siguiente (F10) | Misma cuenta Alpaca, 24/7, paper incluido. Cubre el deseo de horario extendido sin broker nuevo. |
| Forex | ⏸ posponer | **Alpaca no tiene forex** (en roadmap, sin fecha). Requeriría 2º broker (OANDA practice, API v20 gratis) + 2º execution agent. Cripto da el 24/x a costo ~cero. |
| Opciones | ⏳ futuro | Alpaca sí las soporta (incl. paper), pero la complejidad (griegas, expiración) no paga hasta demostrar edge en el subyacente. Requiere migración a `alpaca-py` (A4). |
| Futuros | ⏳ lejano | No en Alpaca; broker aparte. Ahí sí aplicarían COT/CVOL/WASDE. |

## 6. Qué sigue (propuesta para Fase 7 — el usuario elige)

Orden sugerido por urgencia/valor:
1. **C1** — migrar `GROQ_MODEL` a `openai/gpt-oss-120b` + `llm_budget: 5` (**antes del 17-jul**).
2. **B4** — restaurar el `trading.yml` staged local (1 comando).
3. **C2+A2** — salidas OCO GTC broker-side con breakeven (mata 2 críticos/altos de un tiro).
4. **A1, A3** — precio vivo en reglas duras + aislamiento por símbolo (pocas líneas c/u).
5. **M5** — exit code ≠ 0 en errores críticos (email automático de GitHub).
6. **M1, M2, M4, B3** — endurecimientos menores.
7. **A4** — migración a `alpaca-py` (la más grande; puede ir en su propia fase).
8. **M3** — rollup del journal (puede esperar meses).

Después: **F8 dashboard** (GitHub Pages auto-generado) → **F9 régimen de volatilidad + ETFs
(+ tope por sector)** → **F10 (según evidencia del dashboard): cripto 24/7, macro/COT** →
**Fase Final: dinero real + VPS + LLM de pago + feed SIP**.

---
*Fuentes web (consultadas 2026-07-12): console.groq.com/docs/deprecations,
console.groq.com/docs/rate-limits, alpaca.markets/sdks/python/migration.html,
docs.alpaca.markets/us/docs/about-market-data-api, docs.alpaca.markets/us/docs/trading-api,
docs.github.com/actions/managing-workflow-runs/disabling-and-enabling-a-workflow.*
