"""
Almacenamiento estructurado en SQLite (modo WAL, thread-safe) para telemetría en vivo del bot cripto.

Tablas:
  - signals:     Todas las señales emitidas con contexto, score y sizing.
  - events:      Todos los eventos de ejecución (FILL, TRAIL, BE, TP1, TP, SL, TIEMPO, BLOQUEADA).
  - near_misses: Velas 1H con volumen alto (>= P90) o cerca de capitulación (EMA200 <= -8%) que fueron
                 descartadas, con relleno automático de `ret_24h` y `ret_72h` para auditar filtros.
  - ai_reports:  Historial de análisis 4H y auditorías generadas por Gemini.
"""
import os
import sqlite3
import threading
import pandas as pd

_DB_LOCK = threading.Lock()


class BotStorage:
    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self.db_path = os.path.join(log_dir, "cripto_bot.db")
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self):
        with _DB_LOCK, self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    time TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    kind TEXT,
                    direction TEXT,
                    entry_type TEXT,
                    entry REAL,
                    sl REAL,
                    tp REAL,
                    rr REAL,
                    score REAL,
                    vol_pct REAL,
                    delta_1h REAL,
                    structure TEXT,
                    bias_alignment TEXT,
                    risk_usd REAL,
                    notional_usd REAL,
                    leverage INTEGER
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    signal_id TEXT,
                    symbol TEXT,
                    event TEXT,
                    price REAL,
                    pnl REAL,
                    r_multiple REAL
                );

                CREATE TABLE IF NOT EXISTS near_misses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL,
                    trend TEXT,
                    bias TEXT,
                    vol_pct REAL,
                    ema_dist_pct REAL,
                    rsi_min4 REAL,
                    tb_ratio REAL,
                    wick_bull REAL,
                    is_green INTEGER,
                    skip_reason TEXT,
                    ret_24h REAL,
                    ret_72h REAL,
                    UNIQUE(time, symbol)
                );

                CREATE TABLE IF NOT EXISTS ai_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    kind TEXT,
                    model TEXT,
                    content TEXT
                );
            """)

    # -------------------------------------------------------------- Escritura
    def log_signal(self, s):
        with _DB_LOCK, self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO signals
                (id, time, symbol, kind, direction, entry_type, entry, sl, tp, rr, score,
                 vol_pct, delta_1h, structure, bias_alignment, risk_usd, notional_usd, leverage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                s.id, pd.Timestamp(s.time).isoformat(), s.symbol, s.kind, s.direction, s.entry_type,
                float(s.entry), float(s.sl), float(s.tp), float(s.rr), float(s.score),
                s.vol_pct, getattr(s, "delta_1h", None), s.structure, s.bias_alignment,
                s.sizing.get("risk_usd"), s.sizing.get("notional_usd"), s.sizing.get("leverage"),
            ))

    def log_event(self, tr, event):
        name, ts, price = event
        sym = tr.signal_id.split("-")[0] if "-" in str(tr.signal_id) else ""
        with _DB_LOCK, self._connect() as conn:
            conn.execute("""
                INSERT INTO events (time, signal_id, symbol, event, price, pnl, r_multiple)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                pd.Timestamp(ts).isoformat(), tr.signal_id, sym, name,
                float(price), round(float(tr.pnl), 4), round(float(tr.r_multiple), 3),
            ))

    def log_near_miss(self, ts, symbol: str, ctx, desc: dict):
        """Registra una vela 1H de volumen alto o cercana al Sniper que no disparó entrada."""
        vp = desc.get("vol_pct")
        ed = desc.get("ema_dist_pct")
        # Guardar si hubo volumen >= P90 O si el precio está cerca de capitulación (EMA200 <= -8%)
        if not ((vp is not None and vp >= 0.90) or (ed is not None and ed <= -8.0)):
            return
        with _DB_LOCK, self._connect() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO near_misses
                (time, symbol, price, trend, bias, vol_pct, ema_dist_pct, rsi_min4,
                 tb_ratio, wick_bull, is_green, skip_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pd.Timestamp(ts).isoformat(), symbol, float(desc["price"]),
                ctx.structure.trend, ctx.structure.bias,
                vp, ed, desc.get("rsi_min4"), desc.get("tb_ratio"),
                desc.get("wick_bull"), 1 if desc.get("is_green") else 0,
                desc.get("skip_reason") or "filtros generales",
            ))

    def backfill_near_miss_outcomes(self, symbol: str, h1_df: pd.DataFrame):
        """Completa automáticamente ret_24h y ret_72h de los near_misses cuando ya pasó el tiempo."""
        if h1_df is None or h1_df.empty:
            return
        with _DB_LOCK, self._connect() as conn:
            rows = conn.execute("""
                SELECT id, time, price, ret_24h, ret_72h FROM near_misses
                WHERE symbol = ? AND (ret_24h IS NULL OR ret_72h IS NULL)
            """, (symbol,)).fetchall()
            if not rows:
                return
            last_ts = h1_df.index[-1]
            updates = []
            for row_id, ts_str, px, r24, r72 in rows:
                if not px or px <= 0:
                    continue
                t0 = pd.Timestamp(ts_str)
                if t0.tzinfo is None:
                    t0 = t0.tz_localize("UTC")
                new_r24, new_r72 = r24, r72
                if new_r24 is None and last_ts >= t0 + pd.Timedelta(hours=24):
                    sub = h1_df.loc[t0 + pd.Timedelta(hours=24):]
                    if len(sub):
                        new_r24 = round((float(sub["close"].iloc[0]) / px - 1.0) * 100.0, 2)
                if new_r72 is None and last_ts >= t0 + pd.Timedelta(hours=72):
                    sub = h1_df.loc[t0 + pd.Timedelta(hours=72):]
                    if len(sub):
                        new_r72 = round((float(sub["close"].iloc[0]) / px - 1.0) * 100.0, 2)
                if new_r24 != r24 or new_r72 != r72:
                    updates.append((new_r24, new_r72, row_id))
            if updates:
                conn.executemany("UPDATE near_misses SET ret_24h = ?, ret_72h = ? WHERE id = ?", updates)

    def log_ai_report(self, kind: str, model: str, content: str):
        if not content:
            return
        with _DB_LOCK, self._connect() as conn:
            conn.execute("""
                INSERT INTO ai_reports (time, kind, model, content)
                VALUES (?, ?, ?, ?)
            """, (pd.Timestamp.now(tz="UTC").isoformat(), kind, model, content))

    # --------------------------------------------------------- Export Auditoría
    def export_audit_markdown(self, days: int = 30, cfg=None) -> str:
        """Genera un resumen compacto en Markdown listo para pegar en chat y auditar con IA profunda."""
        cutoff = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).isoformat()
        with _DB_LOCK, self._connect() as conn:
            df_sig = pd.read_sql_query("SELECT * FROM signals WHERE time >= ? ORDER BY time", conn, params=(cutoff,))
            df_ev = pd.read_sql_query("SELECT * FROM events WHERE time >= ? ORDER BY time", conn, params=(cutoff,))
            df_nm = pd.read_sql_query("SELECT * FROM near_misses WHERE time >= ? ORDER BY time", conn, params=(cutoff,))
            df_ai = pd.read_sql_query("SELECT time, kind, model FROM ai_reports WHERE time >= ? ORDER BY time DESC LIMIT 5", conn, params=(cutoff,))

        lines = [
            f"# 📦 PAQUETE DE AUDITORÍA CUANTITATIVA (Últimos {days} días)",
            f"- Generado: `{pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M UTC}`",
            f"- Base SQLite: `{self.db_path}`",
        ]
        if cfg is not None:
            lines.append(
                f"- Config activa: pares=`{list(cfg.universe)}` | wick>={cfg.wick_reject_min} | vol>=P{cfg.vol_percentile_min*100:.0f} | "
                f"trail={cfg.trail_start_r}R/{cfg.trail_distance_r}R | lev={cfg.leverage_max}x | risk={cfg.max_risk_pct*100:.1f}% | "
                f"max_port={getattr(cfg, 'max_portfolio_positions', 3)}"
            )

        # 1. Señales y Resultados
        lines.append(f"\n## 1. Señales y Operaciones ({len(df_sig)} señales · {len(df_ev)} eventos)")
        closed = df_ev[df_ev["event"].isin(["TP", "SL", "TIEMPO"])] if len(df_ev) else pd.DataFrame()
        if len(closed):
            wr = (closed["pnl"] > 0).mean() * 100.0
            pnl = closed["pnl"].sum()
            avg_r = closed["r_multiple"].mean()
            dist = closed["event"].value_counts().to_dict()
            lines.append(f"- Cerradas: **{len(closed)}** | Win Rate: **{wr:.1f}%** | PnL: **{pnl:+.2f} USD** | Avg R: **{avg_r:+.3f}R** | Salidas: `{dist}`")
            lines.append("\n### Detalle de Operaciones Cerradas:")
            lines.append("```text\n" + closed[["time", "symbol", "signal_id", "event", "price", "pnl", "r_multiple"]].to_string(index=False) + "\n```")
        else:
            lines.append("- Sin operaciones cerradas en este período.")

        # 2. Near-Misses (Casi-señales con retorno a 24h y 72h)
        lines.append(f"\n## 2. Near-Misses / Casi-Señales ({len(df_nm)} registradas)")
        if len(df_nm):
            with_out = df_nm[df_nm["ret_24h"].notna()]
            lines.append(f"- Con retorno real calculado (24h): **{len(with_out)}** de {len(df_nm)}")
            if len(with_out):
                grp = with_out.groupby("symbol").agg(
                    n=("id", "count"),
                    avg_ret24=("ret_24h", "mean"),
                    avg_ret72=("ret_72h", "mean"),
                    max_rebote24=("ret_24h", "max"),
                    peor_caida24=("ret_24h", "min"),
                ).round(2)
                lines.append("\n### Resumen Near-Misses por Par (% movimiento real después del descarte):")
                lines.append("```text\n" + grp.to_string() + "\n```")
            cols = ["time", "symbol", "price", "vol_pct", "ema_dist_pct", "rsi_min4", "tb_ratio", "is_green", "ret_24h", "ret_72h", "skip_reason"]
            lines.append("\n### Últimas 20 Near-Misses:")
            lines.append("```text\n" + df_nm[cols].tail(20).to_string(index=False) + "\n```")
        else:
            lines.append("- Todavía no se registraron near-misses (se empiezan a grabar desde el próximo cierre 1H).")

        # 3. Reportes AI
        lines.append(f"\n## 3. Reportes Gemini guardados (últimos {len(df_ai)}):")
        if len(df_ai):
            lines.append("```text\n" + df_ai.to_string(index=False) + "\n```")

        out = "\n".join(lines)
        bundle_path = os.path.join(os.path.dirname(self.db_path), "audit_bundle.md")
        with open(bundle_path, "w", encoding="utf-8") as f:
            f.write(out)
        return out
