"""
Agente Analista Cuantitativo con Gemini (SDK oficial `google-genai`, Interactions API).

Incluye un Playbook / Skill cuantitativo completo adaptado específicamente a nuestra estrategia
(Barrido de Liquidez + Volume Profile 1H + Sniper de Capitulación Extrema · Perfil Conservador B)
para que `gemini-3.8-flash` entregue lecturas precisas, sin alucinaciones y con memoria de los
backtests ya validados (2024–2026).

- Nivel 1 (cada 4 horas junto al Radar): Lectura táctica de los 5 pares, distancia exacta al gatillo
  (SHORT o 🔥 CAPITULACIÓN LONG), funding, momentum 4H/24H y chequeo de salud técnica.
- Nivel 2 (Auditoría Semanal): Revisa `alertas.csv` y `eventos.csv` de los últimos 7 días y formula
  hipótesis concretas para validar en Walk-Forward backtest (nunca cambia parámetros en vivo).

Variables en `.env`:
  GEMINI_API_KEY=...
  GEMINI_MODEL=gemini-3.8-flash   (opcional, default: gemini-3.8-flash)
"""
import os
import pandas as pd

from .alerts import load_env, _p, fmt_ts

DEFAULT_MODEL = "gemini-3.8-flash"

# ---------------------------------------------------------------------------
# QUANT SKILL / PLAYBOOK — Memoria institucional del sistema para Gemini Flash
# ---------------------------------------------------------------------------
QUANT_PLAYBOOK = """
=== IDENTIDAD Y ROL ===
Sos el Analista Cuantitativo Senior del sistema algorítmico "Cripto Alertas — Barrido de Liquidez + Volume Profile".
Tu trabajo es interpretar el estado del mercado en tiempo real, auditar la disciplina del bot y detectar oportunidades o anomalías técnicas.

=== ARQUITECTURA DE LA ESTRATEGIA (PERFIL CONSERVADOR B — VALIDADO 2024-2026) ===
- Universo óptimo (5 pares): SOLUSDT, XRPUSDT, BNBUSDT, DOGEUSDT, BTCUSDT.
- Rendimiento validado (2024-2026, resolución 1m): +103.13% neto, CAGR 27.84%, Max DD -9.02%, PF 1.94, Sharpe 1.62, WR 54.2%.
- Estrategia principal (CONFIRMADA 1H): Opera SIEMPRE a favor del sesgo 1D (SHORT en DISTRIBUCIÓN tras barrido de techo/resistencia HVN; LONG en ACUMULACIÓN tras barrido de piso HVN). Exige volumen >= P90, mecha de rechazo >= 55%, delta/absorción y R:R >= 2.0.
- Sniper de Capitulación Extrema (🔥 CAPITULACIÓN LONG): Única excepción contra-tendencia permitida. Exige 5 filtros simultáneos en 1H:
  1) Precio <= -10% debajo de EMA200 (1H)
  2) RSI_min4 <= 25
  3) Volumen >= P95
  4) Vela VERDE con cierre >= 75% de su rango
  5) TakerBuy >= 53%
  Objetivo táctico 2:1, SL debajo del mínimo de 3 velas - 0.5 ATR. (Backtest 3 años: 84.6% WR, PF 8.76).
- Gestión de salida y riesgo: Apalancamiento 5x Aislado, riesgo 2.5% por trade, margen máx 20%, interés compuesto real, Trailing Stop desde +2.5R (distancia 1.5R), sin TP1 parcial, Time-stop 7 días (3 días en Sniper), Escudo Global máx 3 posiciones simultáneas.

=== LECCIONES YA VALIDADAS (NO RECOMENDAR COSAS YA DESCARTADAS) ===
- NUNCA sugerir activar modo PRE (limit intravela): tuvo 83.3% SL por selección adversa.
- NUNCA sugerir desactivar el filtro de sesgo 1D en general: duplicó el Max DD (-3.8% -> -8.1%) sin sumar PnL.
- NUNCA sugerir volver a agregar ETHUSDT ni PAXGUSDT (Oro): ya se testearon y empeoraban el Sharpe/muestra.
- NUNCA sugerir volver al TP1 parcial: el Walk-Forward demostró que cortaba los mejores trades.
- NUNCA sugerir subir el apalancamiento a 10x: 5x da casi igual retorno con la mitad de riesgo de liquidación.

=== REGLAS ANTI-ALUCINACIÓN ===
- Usá EXCLUSIVAMENTE los números provistos en el SNAPSHOT. Nunca inventes precios, niveles ni porcentajes.
- Si un dato falta o dice "-", indicalo sin suponerlo.
""".strip()

SYSTEM_LIVE = (
    QUANT_PLAYBOOK
    + "\n\n=== FORMATO DE RESPUESTA (REPORTE 4H PARA TELEGRAM) ===\n"
    "Respondé en español rioplatense/neutro, claro y directo (máximo 10 líneas, texto plano con emojis simples, SIN tablas ni Markdown complejo).\n"
    "Estructura obligatoria:\n"
    "1) Semáforo de Régimen: [CALMA | ACERCAMIENTO SHORT | ALERTA CAPITULACIÓN | POSICIÓN ACTIVA] + 1 frase del pulso de las últimas 4H/24H.\n"
    "2) Pares más calientes (máx 2): cuál está más cerca del gatillo SHORT o del Sniper Capitulación, indicando exactamente qué filtro falta cumplir.\n"
    "3) Funding / Presión: mencionar solo si algún funding es extremo o si vendedores/compradores dominan el TakerBuy.\n"
    "4) Disciplina & Salud: confirmar si el bot protege bien el capital y si hay posiciones abiertas o anomalías."
)

SYSTEM_WEEKLY = (
    QUANT_PLAYBOOK
    + "\n\n=== FORMATO DE RESPUESTA (AUDITORÍA SEMANAL PARA TELEGRAM) ===\n"
    "Respondé en español, estructurado y concreto (máximo 18 líneas, texto plano con viñetas, SIN tablas).\n"
    "Estructura obligatoria:\n"
    "1) Balance Semanal: trades ejecutados, acierto, PnL estimado y tipos de salida (SL / TP / TRAIL / TIEMPO / BLOQUEADA).\n"
    "2) Diagnóstico por Par y Filtro: qué funcionó bien y dónde hubo fricción.\n"
    "3) 1 a 3 Hipótesis para Backtest Walk-Forward: propuestas específicas y medibles que respeten las lecciones ya validadas "
    "(aclarando siempre que ningún parámetro se cambia en producción sin validación previa)."
)


class GeminiAnalyst:
    def __init__(self, api_key: str = None, model: str = None):
        env = load_env()
        self.api_key = api_key or env.get("GEMINI_API_KEY") or ""
        self.model = model or env.get("GEMINI_MODEL") or DEFAULT_MODEL
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is not None:
            return self._client
        from google import genai
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _ask(self, system_instruction: str, prompt: str, max_tokens: int = 650, thinking: str = "low") -> str:
        if not self.enabled:
            return ""
        print(f"[gemini] Consultando {self.model} (thinking={thinking})...", flush=True)
        client = self._get_client()
        interaction = client.interactions.create(
            model=self.model,
            system_instruction=system_instruction,
            input=prompt,
            generation_config={"thinking_level": thinking, "max_output_tokens": max_tokens},
            store=False,
            timeout=45.0,
        )
        return (interaction.output_text or "").strip()

    # --------------------------------------------------------- Nivel 1: 4H Live
    def build_snapshot_text(self, runners) -> str:
        now_utc = pd.Timestamp.now(tz="UTC")
        now_str = fmt_ts(now_utc)
        open_total = sum(1 for rn in runners for x in rn.trades if not x.is_closed and x.status == "ABIERTA")
        max_port = getattr(runners[0].cfg, "max_portfolio_positions", 3) if runners else 3
        lines = [
            f"SNAPSHOT MERCADO CRIPTO ({now_str}) | Cupo Cartera: {open_total}/{max_port} posiciones abiertas",
        ]
        # Saldo si hay ejecutor conectado
        ex = getattr(runners[0], "executor", None) if runners else None
        if ex is not None and not getattr(ex, "dry_run", True):
            try:
                bal = ex.get_balance()
                lines.append(f"Saldo Exchange: Total={bal['balance']:,.2f} {bal['asset']} | Disponible={bal['availableMargin']:,.2f} {bal['asset']}")
            except Exception:
                pass

        for r in runners:
            if r.ctx is None or r.h1 is None or len(r.h1) == 0:
                continue
            try:
                eng = r.engine()
                t = len(eng.times) - 1
                d = eng.describe(t, r.ctx)
                px = float(r.partial_1h["close"].iloc[-1]) if (r.partial_1h is not None and len(r.partial_1h)) else d["price"]

                # Variación 4H y 24H
                c4 = float(r.h1["close"].iloc[-4]) if len(r.h1) >= 4 else px
                c24 = float(r.h1["close"].iloc[-24]) if len(r.h1) >= 24 else px
                chg4 = (px / c4 - 1.0) * 100.0 if c4 > 0 else 0.0
                chg24 = (px / c24 - 1.0) * 100.0 if c24 > 0 else 0.0

                # Funding actual
                fund = r.ds.funding_now(r.cfg.symbol)
                fund_s = f"{fund*100:+.4f}%" if fund is not None else "-"

                vp = f"P{d['vol_pct']*100:.0f}" if d.get("vol_pct") is not None else "-"
                ed = f"{d['ema_dist_pct']:+.1f}%" if d.get("ema_dist_pct") is not None else "-"
                rs = f"{d['rsi_min4']:.1f}" if d.get("rsi_min4") is not None else "-"
                tb = f"{d['tb_ratio']*100:.0f}%" if d.get("tb_ratio") is not None else "-"
                col = "VERDE" if d.get("is_green") else "ROJA"

                above = [z for z in r.ctx.zones if z.low > px]
                techo = f"{_p(above[0].low)}-{_p(above[0].high)} (+{(above[0].mid/px-1)*100:.1f}%, muralla {above[0].wall_score*100:.0f}%)" if above else "-"

                act = [tr for tr in r.trades if not tr.is_closed]
                act_s = ", ".join(f"{tr.direction} {tr.status} E={_p(tr.entry)} SL={_p(tr.sl)} TP={_p(tr.tp)}" for tr in act) or "ninguna"

                lines.append(
                    f"- {r.cfg.symbol}: px={_p(px)} (4h:{chg4:+.1f}%, 24h:{chg24:+.1f}%) | Funding={fund_s} | "
                    f"1D={r.ctx.structure.summary()} | Techo={techo} | Vol={vp} | "
                    f"Sniper(EMA200={ed}, RSI={rs}, TB={tb}, {col} cierre={d.get('wick_bull',0)*100:.0f}%) | "
                    f"Filtro={d.get('skip_reason') or 'sin vol alto'} | Pos={act_s}"
                )
            except Exception as e:
                lines.append(f"- {r.cfg.symbol}: error snapshot ({e})")

        # Últimas 3 alertas recientes si existen
        alerts_dir = runners[0].cfg.alerts_dir if runners else None
        if alerts_dir:
            ap = os.path.join(alerts_dir, "alertas.csv")
            if os.path.exists(ap):
                try:
                    dfa = pd.read_csv(ap).tail(3)
                    if len(dfa):
                        cols = [c for c in ("time", "symbol", "direction", "entry", "rr", "score") if c in dfa.columns]
                        lines.append("\nÚltimas alertas registradas:\n" + dfa[cols].to_string(index=False))
                except Exception:
                    pass

        return "\n".join(lines)

    def analyze_radar_snapshot(self, runners) -> str:
        if not self.enabled or not runners:
            return ""
        try:
            snap = self.build_snapshot_text(runners)
            prompt = (
                "Analizá el siguiente SNAPSHOT real de los 5 pares del bot y devolvé tu lectura ejecutiva "
                "para Telegram siguiendo estrictamente tu Playbook y formato:\n\n" + snap
            )
            ans = self._ask(SYSTEM_LIVE, prompt, max_tokens=650)
            if not ans:
                return ""
            return f"🧠 ANÁLISIS GEMINI ({self.model})\n{ans}"
        except Exception as e:
            print(f"[gemini] Aviso en análisis 4H: {e}")
            return ""

    # --------------------------------------------------------- Nivel 2: Semanal
    def run_weekly_audit(self, alerts_dir: str, cfg) -> str:
        if not self.enabled:
            return ""
        try:
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
            alertas_path = os.path.join(alerts_dir, "alertas.csv")
            eventos_path = os.path.join(alerts_dir, "eventos.csv")

            parts = [
                f"AUDITORÍA SEMANAL (últimos 7 días hasta {fmt_ts(pd.Timestamp.now(tz='UTC'))})",
                f"Config activa: pares={list(cfg.universe)}, wick>={cfg.wick_reject_min}, vol>=P{cfg.vol_percentile_min*100:.0f}, "
                f"trail={cfg.trail_start_r}R/{cfg.trail_distance_r}R, lev={cfg.leverage_max}x, risk={cfg.max_risk_pct*100:.1f}%",
            ]

            if os.path.exists(alertas_path):
                df_a = pd.read_csv(alertas_path)
                if "time" in df_a.columns and len(df_a):
                    df_a["time_dt"] = pd.to_datetime(df_a["time"], format="ISO8601", utc=True, errors="coerce")
                    df_a = df_a[df_a["time_dt"] >= cutoff]
                parts.append(f"\nAlertas emitidas últimos 7d ({len(df_a)}):")
                if len(df_a):
                    cols = [c for c in ("time", "id", "symbol", "direction", "entry", "sl", "tp", "rr", "score", "bias_alignment") if c in df_a.columns]
                    parts.append(df_a[cols].tail(30).to_string(index=False))
            else:
                parts.append("\nAlertas emitidas últimos 7d: 0 (sin archivo alertas.csv aún)")

            if os.path.exists(eventos_path):
                df_e = pd.read_csv(eventos_path)
                if "time" in df_e.columns and len(df_e):
                    df_e["time_dt"] = pd.to_datetime(df_e["time"], format="ISO8601", utc=True, errors="coerce")
                    df_e = df_e[df_e["time_dt"] >= cutoff]
                parts.append(f"\nEventos de trades últimos 7d ({len(df_e)}):")
                if len(df_e):
                    parts.append(df_e.drop(columns=["time_dt"], errors="ignore").tail(40).to_string(index=False))
            else:
                parts.append("\nEventos de trades últimos 7d: 0 (sin archivo eventos.csv aún)")

            ans = self._ask(SYSTEM_WEEKLY, "\n".join(parts), max_tokens=1100)
            if not ans:
                return ""
            return f"📊 AUDITORÍA SEMANAL GEMINI ({self.model})\n{ans}"
        except Exception as e:
            print(f"[gemini] Aviso en auditoría semanal: {e}")
            return ""
