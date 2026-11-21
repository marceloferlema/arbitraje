# 🧠 CONTEXTO TÉCNICO Y CUANTITATIVO PARA GEMINI — BOT CRIPTO ALERTAS

> **Propósito de este documento:** Proveer a Gemini (o cualquier agente IA) el contexto completo, actualizado y verificado del sistema **Cripto Alertas** (estrategia, arquitectura, backtests Walk-Forward, lecciones validadas y flujo de auditoría), para evitar repetir experimentos descartados y tomar decisiones cuantitativas rigurosas.

---

## 1. Resumen del Sistema (Estado en Producción)
- **Nombre:** Monitor de Alertas & Ejecución Cripto (*Barrido de Liquidez sobre Volume Profile + Sniper de Capitulación*).
- **Entorno:** VPS Oracle Cloud (Ubuntu), corriendo 24/7 bajo `systemd` (`cripto-alertas.service`).
- **Fuente de datos:** Binance USD-M Futures (WebSocket `fstream.binance.com` con fallback automático a `data-stream.binance.vision` + Failsafe horario por REST para evitar congelamiento).
- **Ejecución actual:** Binance USD-M Futures **Testnet** (`--binance-testnet`) usando el **Algo Order API** (`/fapi/v1/algoOrder` para `STOP_MARKET` / `TAKE_PROFIT_MARKET`) y `/fapi/v1/order` para entradas `MARKET`. También existe conector listo para BingX (`bingx.py`).
- **Notificaciones:** Bot dedicado de Telegram (`CRIPTO_TELEGRAM_TOKEN` + `CRIPTO_CHAT_ID`, independiente del bot BYMA).
- **Analista AI 24/7:** `gemini-3.8-flash` (fallback automático `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`) usando el SDK oficial `google-genai` (Interactions API, `thinking_level="low"`, `store=False`).
- **Base de datos:** SQLite en modo `WAL` (`cripto/alerts_log/cripto_bot.db`) con 4 tablas: `signals`, `events`, `near_misses` (con relleno automático de `ret_24h` y `ret_72h`) y `ai_reports`.
- **API de Auditoría (solo lectura):** Expuesta temporalmente vía Flask Blueprint en `https://arbitraje.smartsoft.com.ar/api/cripto/{status,audit,table/<name>}` protegida con `Authorization: Bearer <CRIPTO_API_TOKEN>`.

---

## 2. Configuración Vigente — Perfil Conservador B (`cripto/config_cripto.py`)

| Categoría | Parámetro | Valor Activo | Justificación Validada |
| :--- | :--- | :---: | :--- |
| **Universo** | `universe` | `SOL, XRP, BNB, DOGE, BTC` | 5 pares líquidos; `ETHUSDT` y `PAXGUSDT` descartados por menor Sharpe/muestra. |
| **Volume Profile** | `profile_lookback_days` | `540` (~18 meses) | Sin lookahead (`h1.index < day`). |
| **Estructura 1H** | `pivot_len_1h` / `sweep_bars` | `6` / `4` | Captura swings intradía y ventana de barrido + reclaim. |
| **Filtros Entrada** | `min_score` | `65.0` | Balance óptimo entre frecuencia y calidad. |
| | `vol_percentile_min` | `0.90` (P90) | Optimizado en Fase 1 (subió PF de 1.66 → 1.82). |
| | `wick_reject_min` | `0.55` (55%) | Exige rechazo fuerte de mecha o cierre a favor. |
| | `delta_confirm` | `True` | Exige absorción agresiva en `taker_buy_base`. |
| | `no_counter_bias` | `True` | Obligatorio operar a favor del sesgo 1D (salvo Sniper). |
| | `btc_filter` | `"strict"` | Evita operar altcoins contra movimientos bruscos de BTC. |
| **Sniper Capitulación** | `capitulation_enabled` | `True` | Única excepción LONG contra tendencia: `EMA200 <= -10%`, `RSI_min4 <= 25`, `Vol >= P95`, vela verde cierre `>= 75%`, `TakerBuy >= 53%`, objetivo táctico `2:1`, `cooldown 48h`, `max_hold 3d`. |
| **Salidas** | `breakeven_r` / `partial_tp_r` | `0.0` / `0.0` | Desactivados: el Walk-Forward demostró que `TP1` parcial cortaba los mejores ganadores. |
| | `trail_start_r` / `trail_distance_r` | `2.5` / `1.5` | Trailing Stop activo desde `+2.5R`; sincroniza `STOP_MARKET` en Binance cuando mejora `>= 0.5%`. |
| | `max_hold_days` | `7` | Óptimo verificado vs 5d, 10d y 14d. |
| **Capital y Riesgo** | `leverage_max` | `5x` (Aislado) | Casi mismo retorno que 10x con mitad de riesgo de liquidación. |
| | `max_risk_pct` | `2.5%` | Riesgo por operación (interés compuesto real consultando saldo vivo de Binance). |
| | `margin_pct_min / max` | `8%` / `20%` | Permite que el sizing alcance el riesgo objetivo. |
| | `max_open_positions` | `1` por par | Venció a `2` en el 100% del Top 10 Walk-Forward. |
| | `max_portfolio_positions` | `3` global | Escudo de exposición correlacionada entre los 5 pares. |

---

## 3. Resultados de Backtesting Oficial (Validación Walk-Forward `2024-01-01` → `2026-11-20`)

*Backtest oficial con resolución intravela de 1 minuto, comisiones maker/taker, slippage y funding histórico real (capital inicial: \$1,000):*

| Métrica | Baseline Inicial (6p, 3x, 1%) | + Sniper Capitulación | 🔥 **Conservador B (Actual)** |
| :--- | :---: | :---: | :---: |
| **Capital final** | \$1,145.69 | \$1,183.26 | **\$2,031.35** |
| **Retorno neto** | +14.57% | +18.33% | **+103.13%** *(CAGR 27.84%)* |
| **Max Drawdown** | -3.80% | -3.47% | **-9.02%** *(techo conservador ≤ -10%)* |
| **Profit Factor** | 1.54 | 1.65 | **1.94** |
| **Sharpe Ratio** | 1.12 | 1.30 | **1.62** |
| **Win Rate** | 52.0% | 53.6% | **54.2%** (`LONG 52.4%` · `SHORT 55.6%`) |
| **Avg R / trade** | +0.276 R | +0.303 R | **+0.426 R** |

**Validación Walk-Forward (sin sobreajuste):**
- **In-Sample (`2024-01` → `2025-07`):** `+55.3%`
- **Out-of-Sample ciego (`2025-07` → `2026-11`):** `+35.4%`

**Desglose por par (todos en positivo):**
- `XRPUSDT`: `+$448.88` (28 trades, WR `60.7%`, PF `2.39`)
- `BNBUSDT`: `+$208.81` (18 trades, WR `44.4%`, PF `1.90`)
- `SOLUSDT`: `+$167.62` (16 trades, WR `56.2%`, PF `1.99`)
- `DOGEUSDT`: `+$137.44` (14 trades, WR `57.1%`, PF `1.93`)
- `BTCUSDT`: `+$68.38` (20 trades, WR `50.0%`, PF `1.30`)

---

## 4. Lecciones Validadas (Reglas de No-Regresión para IA)
1. **NO activar modo `PRE` (órdenes limit anticipadas):** Sufrió selección adversa severa (**83.3% SL**). Solo operar `CONFIRMADA` al cierre de vela 1H.
2. **NO desactivar `no_counter_bias` de forma general:** Permitió 53 trades extra contra tendencia ganando exactamente el mismo PnL (`+$145.47` vs `+$145.69`) pero **duplicando el Max Drawdown** (`-8.12%` vs `-3.80%`).
3. **SÍ mantener el Sniper de Capitulación:** Aislado sobre 3 años generó 13 señales en pánicos reales con **84.6% WR** (solo 1 SL), **PF 8.76** y **+8.88 R** (incluyendo el piso exacto de SOL en `$67.35` el `15/11/2026`).
4. **NO re-agregar `ETHUSDT` ni `PAXGUSDT` (Oro):** `ETH` bajaba el Sharpe global; `PAXGUSDT` solo dio 5–7 trades en 16 meses (muestra insuficiente, Sharpe `0.38`).
5. **NO volver al `TP1` parcial ni subir a `10x`:** `TP1` recortaba los ganadores de cola larga; `10x` apenas sumaba +4% sobre `5x` duplicando el riesgo de liquidación.
6. **NUNCA modificar parámetros en producción sin validación Walk-Forward previa** sobre 3 años respetando el techo `Max DD <= -10%`.

---

## 5. Mapa de Archivos del Módulo `cripto/`

```text
cripto/
├── config_cripto.py                  # Parámetros globales (Perfil Conservador B)
├── run_live.py                       # CLI en vivo (multi-par, Radar 4H, Gemini, Binance/BingX, --export-audit)
├── run_backtest.py                   # Backtester oficial (individual y cartera multi-par, resolución 1m)
├── cripto-alertas.service            # Unidad systemd para VPS Oracle
├── requirements.txt                  # Dependencias Python (incl. google-genai>=2.30.1)
├── CONTEXTO_GEMINI.md                # Este documento (contexto técnico/cuantitativo para IA)
├── DOCUMENTO_FUNCIONAL.md            # Manual funcional de la estrategia y alertas para humanos
└── cripto_alertas/
    ├── data_source.py                # Binance REST/WS + fallback sin 451 + escritura CSV atómica thread-safe
    ├── indicators.py                 # ATR, Pivots, Volume Stats, Squeeze Momentum
    ├── volume_profile.py             # Volume Profile logarítmico, HVN, POC/VAH/VAL, Wall Score
    ├── structure.py                  # Estructura 1D, Acumulación/Distribución, Contexto sin lookahead
    ├── strategy.py                   # Motor de señales (CONFIRMADA + Sniper Capitulación + describe())
    ├── trades.py                     # Simulador de trades (SL, TP, BE, TP1, Trailing Stop, Time-Stop)
    ├── backtest.py                   # Motor de replay histórico con caché de contexto diario
    ├── live.py                       # LiveRunner (WS 1m/1h, Failsafe REST, interés compuesto, escudo cartera)
    ├── alerts.py                     # Formateo Telegram + logging dual (SQLite + CSV)
    ├── radar.py                      # Radar Previsor de techos/pisos cada 4 horas
    ├── ai_analyst.py                 # Analista Gemini (Playbook cuantitativo 4H + Auditoría semanal + fallback)
    ├── storage.py                    # SQLite WAL (signals, events, near_misses con ret_24h/72h, ai_reports)
    ├── api_blueprint.py              # API Flask solo lectura (/api/cripto/*) protegida por Bearer Token
    ├── binance_exec.py               # Ejecutor automático Binance USD-M Futures (Algo Order API)
    └── bingx.py                      # Ejecutor automático BingX Perpetual Futures (Demo VST / Live)
```

---

## 6. Flujo de Auditoría y Evolución Futura
1. **Recolección continua (VPS):** `cripto_bot.db` graba señales, eventos, near-misses (con retorno real a 24h y 72h) y reportes de Flash.
2. **Consulta en vivo (costo \$0):** Desde Antigravity se consulta `GET https://arbitraje.smartsoft.com.ar/api/cripto/audit?days=30&format=json` con `CRIPTO_API_TOKEN` del `.env` local.
3. **Ciclo de mejora:** Analizar telemetría real (mínimo 15–20 trades reales o 30 días de near-misses) → formular hipótesis → validar en Walk-Forward (`2024–2026`, `Max DD <= -10%`) → aprobar → `git push` + `git pull` en VPS.
4. **Plan futuro de separación:** Toda la lógica y el blueprint de API ya viven aislados dentro de `cripto/`. Cuando se decida separar `cripto` de `arbitraje`, solo se mueve la carpeta `cripto/` a un repositorio propio, se levanta un micro-servidor Gunicorn independiente para `api_blueprint.py` y se remueve el `try/except` de 5 líneas en `app/__init__.py`.
