# 📘 DOCUMENTO FUNCIONAL — BOT DE ALERTAS Y TRADING CRIPTO

**Estrategia:** Barrido de Liquidez sobre Zonas de Volume Profile + Sniper de Capitulación Extrema  
**Perfil Activo:** Conservador B *(techo de caída máxima histórica < 10%)*  
**Pares Monitoreados (5):** `SOLUSDT`, `XRPUSDT`, `BNBUSDT`, `DOGEUSDT`, `BTCUSDT`  
**Exchange:** Binance USD-M Futures *(actualmente en Modo Testnet Demo)*  

---

## 1. ¿Cuál es la filosofía de la estrategia?
El bot busca operar **a favor del dinero institucional**, nunca adivinando techos ni pisos en medio del ruido intradía.

Opera bajo dos motores complementarios:
1. **Motor Principal (A favor de tendencia):** Espera a que el precio barra la liquidez (Stop Loss de minoristas) sobre una zona histórica de alto volumen (*Volume Profile*), absorba las órdenes con volumen fuerte y cierre la vela de 1 hora confirmando el rechazo.
2. **Sniper de Capitulación Extrema (Rebotes de pánico):** Una excepción ultra-selectiva que solo dispara compras (`LONG`) cuando el mercado sufre un desplome extremo de pánico y aparecen compras institucionales masivas en el piso.

---

## 2. ¿Qué analiza el bot y con qué frecuencia?

| Frecuencia | Qué analiza | Objetivo |
| :--- | :--- | :--- |
| **Cada 24 horas** *(00:00 UTC)* | Últimos 18 meses en velas `1D` y `1H` | Construye el **Volume Profile** (zonas HVN, `POC`, `VAH`, `VAL` y fuerza de "muralla") y define si el sesgo macro 1D es **ACUMULACIÓN** (comprador), **DISTRIBUCIÓN** (vendedor) o **NEUTRAL**. |
| **Cada 1 hora** *(al cierre `:00`)* | Vela `1H` recién cerrada | Evalúa si se cumplieron los filtros del **Motor Principal** o del **Sniper de Capitulación**. Si no hay señal pero hubo volumen alto (`>= P90`) o sobreextensión (`EMA200 <= -8%`), graba el caso en `near_misses` para auditoría futura. |
| **Cada 1 minuto** | Vela `1m` en tiempo real (WebSocket) | Supervisa operaciones abiertas: mueve el **Trailing Stop** y verifica toques de Stop Loss, Take Profit o Time-Stop sin esperar al cierre horario. |
| **Cada 4 horas** | Foto consolidada de los 5 pares | Envía a Telegram el **Radar Previsor** (techos/pisos clave) + el **Análisis Ejecutivo de Gemini AI**. |

---

## 3. ¿Qué señales detecta y dispara el bot?

### A) Señal Principal — `🟢 LONG` o `🔴 SHORT` (`SEÑAL CONFIRMADA`)
Para que el bot dispare una entrada principal, deben cumplirse **todas** estas condiciones al cierre de la vela de 1 hora:
1. **Alineación con el sesgo diario (`1D`):**
   * En **DISTRIBUCIÓN** (mercado bajista) → solo busca **🔴 SHORT** en techos.
   * En **ACUMULACIÓN** (mercado alcista) → solo busca **🟢 LONG** en pisos.
2. **Barrido de liquidez + Reclaim:** El precio perfora un máximo/mínimo relevante previo (`pivot_len = 6`) dentro de una franja de Volume Profile (HVN) y vuelve a cerrar del lado correcto dentro de una ventana de 4 velas.
3. **Volumen institucional (`>= Percentil 90`):** El volumen de la vela debe estar dentro del 10% más alto de los últimos 30 días.
4. **Rechazo claro por mecha (`>= 55%`):** La vela cerrada debe mostrar rechazo evidente (cierre en el 55% superior para LONG o 55% inferior para SHORT).
5. **Absorción de flujo (`Delta / Taker Buy`):** Confirma presión agresiva a favor de la dirección de la señal.
6. **Filtro de Bitcoin (`strict`):** Si es una altcoin, verifica que BTC no esté rompiendo violentamente en contra.
7. **Relación Riesgo/Beneficio (`R:R >= 2.0`) y Score (`>= 65/100`):** El objetivo (siguiente zona de liquidez) debe ofrecer al menos 2 veces lo que se arriesga al Stop Loss.

### B) Señal Especial — `🔥 CAPITULACIÓN EXTREMA 🟢 LONG` (*Sniper Rebote*)
Única excepción que opera contra una tendencia bajista, diseñada exclusivamente para cazar pisos de pánico extremo (como el piso `$67.35` de `SOLUSDT` del `15/11/2026`). Exige **5 filtros simultáneos**:
1. Precio sobreextendido **`<= -10%`** por debajo de la `EMA 200` (1H).
2. **`RSI (1H) <= 25`** en alguna de las últimas 4 velas (sobreventa extrema).
3. **Clímax de volumen** en **`Percentil >= 95`**.
4. **Vela VERDE** con cierre en el **`>= 75%` superior** de su rango.
5. **Compras agresivas (`Taker Buy >= 53%`)** demostrando absorción real de manos grandes.
* *Stop Loss:* Debajo del mínimo de las últimas 3 velas menos `0.5 ATR`.
* *Take Profit táctico:* **`2:1`** (vuelta a la media) con Time-Stop máximo de **3 días**.

---

## 4. Gestión de Riesgo y Ejecución Automática (Perfil Conservador B)
* **Margen y Apalancamiento:** **Margen Aislado (`ISOLATED`) a `5x`** (margen entre `8%` y `20%` del capital según ancho del SL).
* **Riesgo por operación:** Máximo **`2.5%`** del capital real disponible.
* **Interés compuesto automático:** Antes de abrir cada trade, consulta el saldo real de la cuenta en Binance para ajustar el tamaño de posición automáticamente.
* **Escudo Global de Cartera:** Máximo **1 posición por moneda** y máximo **3 posiciones abiertas simultáneas** entre los 5 pares.
* **Protección de ganancias (Trailing Stop):** Al alcanzar **`+2.5R`** a favor, activa un Trailing Stop a **`1.5R`** de distancia y actualiza automáticamente la orden `STOP_MARKET` en Binance (cuando mejora al menos `0.5%`). No usa TP1 parcial para dejar correr las tendencias largas.
* **Time-Stop:** Si una operación principal no toca SL ni TP en **7 días**, se cierra automáticamente a mercado para liberar capital.

---

## 5. Catálogo de Alertas y Mensajes en Telegram

| Tipo de Mensaje | Cuándo llega | Qué contiene |
| :--- | :--- | :--- |
| **`🚀 Monitor de Alertas Cripto ACTIVO`** | Al iniciar/reiniciar el servicio | Confirma los 5 pares activos, el estado de conexión con Binance, el saldo disponible y el sesgo 1D de cada par. |
| **`🔭 RADAR PREVISOR CRIPTO`** | Al arrancar y **cada 4 horas** | Mapa de navegación: precio actual en vivo, techo vendedor más cercano (`+X%` y fuerza de muralla), piso comprador (`-X%`) y foco operativo (`SHORT` / `LONG`). |
| **`🧠 ANÁLISIS GEMINI`** | Justo después de cada Radar (**cada 4 horas**) | Interpretación ejecutiva generada por Gemini: Semáforo de Régimen (`🔥 ALERTA CAPITULACIÓN`, `🟡 ACERCAMIENTO SHORT`, `🟢 POSICIÓN ACTIVA`, `⚪ CALMA`), top 2 pares más cercanos al gatillo, Funding Rate extremo y salud/cupo del bot. |
| **`🟢/🔴 SEÑAL CONFIRMADA`** o **`🔥 CAPITULACIÓN EXTREMA`** | Al cierre de una vela `1H` que pasa todos los filtros | Dirección, Score, Entrada, SL, TP, R:R, tamaño de posición en USD/qty, zona HVN, Funding, enlace directo a TradingView y confirmación de ejecución en Binance (`🤖 [BINANCE TESTNET]`). |
| **Eventos de Posición** | Al instante (monitoreo `1m`) | `📈 Trailing Stop actualizado`, `🎯 TAKE PROFIT final`, `🛑 STOP LOSS tocado`, `⏱️ Time-stop` o `⛔ BLOQUEADA` (si se alcanzó el cupo global de 3 posiciones). |
| **`📊 AUDITORÍA SEMANAL GEMINI`** | Domingos `21:00 ART` | Balance de los últimos 7 días e hipótesis de mejora para validar en backtest (nunca modifica parámetros sin aprobación). |

---

## 6. Desempeño Histórico Validado (`2024-01-01` → `2026-11-20`)
Simulación oficial de ~2.9 años con resolución intravela de 1 minuto, comisiones, slippage y funding real (\$1,000 iniciales):
* **Capital final:** **\$2,031.35** (**`+103.13%`** neto · **`27.84%` anual compuesto**)
* **Caída máxima (Max Drawdown):** **`-9.02%`** *(dentro del techo conservador del -10%)*
* **Tasa de acierto (Win Rate):** **`54.2%`** (`96` trades cerrados)
* **Profit Factor:** **`1.94`** · **Sharpe Ratio:** **`1.62`** · **Avg R:** **`+0.426 R`**
* **Los 5 pares terminaron en positivo:** `XRP (+$449)`, `BNB (+$209)`, `SOL (+$168)`, `DOGE (+$137)`, `BTC (+$68)`.
