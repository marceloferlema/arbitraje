"""
Agente Analista Cuantitativo con Gemini (SDK oficial `google-genai`, Interactions API).

- Nivel 1 (cada 4 horas junto al Radar): Interpreta el estado actual de los 5 pares,
  qué monedas están más cerca de disparar SHORT o 🔥 CAPITULACIÓN LONG, y valida la disciplina del bot.
- Nivel 2 (Auditoría Semanal): Analiza `alertas.csv` y `eventos.csv` de los últimos 7 días
  y propone hipótesis concretas de mejora para validar en Walk-Forward backtest (nunca modifica
  parámetros en vivo sin aprobación humana).

Variables en `.env`:
  GEMINI_API_KEY=...
  GEMINI_MODEL=gemini-3.8-flash   (opcional, default: gemini-3.8-flash)
"""
import os
import pandas as pd

from .alerts import load_env, _p, fmt_ts

DEFAULT_MODEL = "gemini-3.8-flash"

SYSTEM_LIVE = (
    "Sos un trader cuantitativo senior que audita un bot algorítmico de cripto futuros "
    "(estrategia: Barrido de Liquidez + Volume Profile 1H a favor del sesgo 1D + Sniper de Capitulación Extrema LONG). "
    "El perfil activo es Conservador B (5 pares: SOL, XRP, BNB, DOGE, BTC; apalancamiento 5x; riesgo 2.5%; "
    "Trailing Stop 2.5R/1.5R; sin TP1 parcial; escudo global máx 3 posiciones). "
    "Responde SIEMPRE en español rioplatense/neutro, conciso, directo y accionable (máximo 10 líneas). "
    "Usá texto plano limpio con viñetas simples (sin tablas Markdown ni HTML). "
    "Destacá: 1) qué 1 o 2 pares están más cerca de disparar señal (SHORT en techo o CAPITULACIÓN LONG), "
    "2) si el bot está protegiendo bien el capital, y 3) estado de operaciones activas si hay."
)

SYSTEM_WEEKLY = (
    "Sos un investigador cuantitativo senior realizando una auditoría semanal de un bot de cripto futuros "
    "(Barrido de Liquidez + Volume Profile + Sniper de Capitulación). "
    "Recibís el historial de alertas y eventos cerrados de los últimos 7 días y la configuración vigente. "
    "Respondé en español, estructurado y concreto (máximo 18 líneas, texto plano con viñetas): "
    "1) Resumen de desempeño semanal (trades, acierto, PnL, salidas SL/TP/TRAIL/TIEMPO). "
    "2) Qué funcionó bien y qué pares/filtros mostraron fricción. "
    "3) 1 a 3 hipótesis específicas de mejora para probar en backtest Walk-Forward (recordando que "
    "ningún parámetro se cambia en vivo sin validación estadística previa)."
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

    def _ask(self, system_instruction: str, prompt: str) -> str:
        if not self.enabled:
            return ""
        client = self._get_client()
        interaction = client.interactions.create(
            model=self.model,
            system_instruction=system_instruction,
            input=prompt,
            store=False,
        )
        return (interaction.output_text or "").strip()

    # --------------------------------------------------------- Nivel 1: 4H Live
    def build_snapshot_text(self, runners) -> str:
        now_str = fmt_ts(pd.Timestamp.now(tz="UTC"))
        lines = [f"SNAPSHOT MERCADO CRIPTO ({now_str}):"]
        for r in runners:
            if r.ctx is None or r.h1 is None or len(r.h1) == 0:
                continue
            try:
                eng = r.engine()
                t = len(eng.times) - 1
                d = eng.describe(t, r.ctx)
                px = float(r.partial_1h["close"].iloc[-1]) if (r.partial_1h is not None and len(r.partial_1h)) else d["price"]
                vp = f"P{d['vol_pct']*100:.0f}" if d.get("vol_pct") is not None else "-"
                ed = f"{d['ema_dist_pct']:+.1f}%" if d.get("ema_dist_pct") is not None else "-"
                rs = f"{d['rsi_min4']:.1f}" if d.get("rsi_min4") is not None else "-"
                tb = f"{d['tb_ratio']*100:.0f}%" if d.get("tb_ratio") is not None else "-"
                col = "VERDE" if d.get("is_green") else "ROJA"
                above = [z for z in r.ctx.zones if z.low > px]
                techo = f"{_p(above[0].low)}-{_p(above[0].high)} (+{(above[0].mid/px-1)*100:.1f}%)" if above else "-"
                act = [tr for tr in r.trades if not tr.is_closed]
                act_s = ", ".join(f"{tr.direction} {tr.status} E={_p(tr.entry)} SL={_p(tr.sl)} TP={_p(tr.tp)}" for tr in act) or "ninguna"
                lines.append(
                    f"- {r.cfg.symbol}: px={_p(px)} | 1D={r.ctx.structure.summary()} | Techo={techo} | "
                    f"Vol={vp} | Sniper(EMA200={ed} req<=-10%, RSI={rs} req<=25, TB={tb} req>=53%, {col} cierre={d.get('wick_bull',0)*100:.0f}%) | "
                    f"Skip={d.get('skip_reason') or 'ok'} | Posiciones={act_s}"
                )
            except Exception as e:
                lines.append(f"- {r.cfg.symbol}: error snapshot ({e})")
        return "\n".join(lines)

    def analyze_radar_snapshot(self, runners) -> str:
        if not self.enabled or not runners:
            return ""
        try:
            snap = self.build_snapshot_text(runners)
            prompt = (
                "Analizá el siguiente estado actual de los 5 pares del bot y devolvé tu lectura ejecutiva "
                "para el trader en Telegram:\n\n" + snap
            )
            ans = self._ask(SYSTEM_LIVE, prompt)
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

            ans = self._ask(SYSTEM_WEEKLY, "\n".join(parts))
            if not ans:
                return ""
            return f"📊 AUDITORÍA SEMANAL GEMINI ({self.model})\n{ans}"
        except Exception as e:
            print(f"[gemini] Aviso en auditoría semanal: {e}")
            return ""
