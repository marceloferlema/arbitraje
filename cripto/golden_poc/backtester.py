"""
Backtester Histórico Walk-Forward (Cero Look-Ahead Bias) — Estrategia Golden POC & R:R 2:1.

Soporta dos modos de entrada (--entry-mode):
  - market_on_signal : Entra al cierre de la vela que dispara el gatillo REBOTE OBLIGADO con R:R >= min_rr.
  - limit_poc        : Deja orden Limit en poc_mid cuando hay estructura/gatillo con R:R >= min_rr y llena si low <= poc_mid.
  - both             : Corre ambos modos y los compara lado a lado.

Ejemplos:
  python backtester.py --symbol SOL/USDT --timeframe 1h --days 180 --capital 1000 --leverage 3 --min-rr 2.0
  python backtester.py --symbol SOL/USDT --timeframe 1h --start 2022-01-01 --end 2026-11-01 --entry-mode both
"""
import argparse
import os
import sys
import numpy as np
import pandas as pd
try:
    from tabulate import tabulate
except ImportError:
    def tabulate(rows, headers="keys", **kwargs):
        return pd.DataFrame(rows).to_string(index=False)

from config import Config, DEFAULT_CONFIG
from data_loader import DataLoader
from strategy_engine import compute_indicators, evaluate_bar

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def run_walk_forward(
    df: pd.DataFrame,
    symbol: str = "SOL/USDT",
    timeframe: str = "1h",
    entry_mode: str = "market_on_signal",
    cfg: Config = DEFAULT_CONFIG,
    start_eval_ts: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.Series, dict]:
    ind = compute_indicators(df, cfg)
    o = ind["open"]
    h = ind["high"]
    l = ind["low"]
    c = ind["close"]
    times = df.index
    n = len(df)

    start_i = cfg.vp_lookback
    if start_eval_ts is not None:
        start_i = max(start_i, int(times.searchsorted(start_eval_ts)))

    capital = float(cfg.capital_total)
    fee_rate = cfg.fee_pct / 100.0
    last_signal_idx = -999_999

    active_trade = None
    pending_limit = None
    trades = []
    equity_points = []

    for i in range(start_i, n):
        ts = times[i]
        hi, lo, cl, op = float(h[i]), float(l[i]), float(c[i]), float(o[i])

        # 1. Si hay orden limit pendiente (modo limit_poc), verificar si se llena o expira
        if active_trade is None and pending_limit is not None:
            if i - pending_limit["created_idx"] > cfg.limit_valid_bars or hi >= pending_limit["tp"]:
                pending_limit = None
            elif lo <= pending_limit["entry"]:
                fill_px = op if op < pending_limit["entry"] else pending_limit["entry"]
                active_trade = {
                    **pending_limit,
                    "entry_time": ts,
                    "entry_idx": i,
                    "entry_price": fill_px,
                }
                pending_limit = None
                # Criterio conservador en la misma vela del fill: verificar SL primero
                if lo <= active_trade["sl"]:
                    _close_trade(active_trade, ts, i, active_trade["sl"], "SL", fee_rate, trades)
                    capital += active_trade["net_pnl_usd"]
                    active_trade = None

        # 2. Si hay posición abierta (de barras previas), evaluar SL / TP / Time-stop
        elif active_trade is not None:
            hit_sl = lo <= active_trade["sl"]
            hit_tp = hi >= active_trade["tp"]
            held_bars = i - active_trade["entry_idx"]
            if hit_sl:
                # Criterio conservador: si toca SL y TP en la misma vela, asume SL primero
                exit_px = op if op < active_trade["sl"] else active_trade["sl"]
                _close_trade(active_trade, ts, i, exit_px, "SL", fee_rate, trades)
                capital += active_trade["net_pnl_usd"]
                active_trade = None
            elif hit_tp:
                exit_px = op if op > active_trade["tp"] else active_trade["tp"]
                _close_trade(active_trade, ts, i, exit_px, "TP", fee_rate, trades)
                capital += active_trade["net_pnl_usd"]
                active_trade = None
            elif cfg.max_hold_bars and held_bars >= cfg.max_hold_bars:
                _close_trade(active_trade, ts, i, cl, "TIME_STOP", fee_rate, trades)
                capital += active_trade["net_pnl_usd"]
                active_trade = None

        # 3. Si no hay posición abierta, evaluar nueva señal estrictamente sobre df.iloc[i-200+1 : i+1]
        if active_trade is None:
            res = evaluate_bar(
                df, i, symbol=symbol, timeframe=timeframe, cfg=cfg,
                ind=ind, last_signal_idx=last_signal_idx, capital_total=capital,
                require_trigger=True,
            )
            if res is not None and res.trigger_signal and res.approved:
                last_signal_idx = i
                if entry_mode == "market_on_signal":
                    # Entrada al cierre de la vela de señal si el R:R desde close >= min_rr
                    entry_px = cl
                    sl_px = res.poc_low * (1.0 - cfg.sl_buffer_pct / 100.0)
                    if sl_px >= entry_px:
                        sl_px = entry_px * (1.0 - (cfg.sl_buffer_pct + 1.5) / 100.0)
                    tp_px = entry_px + (res.res_low - entry_px) * (cfg.tp_safety_pct / 100.0)
                    risk_p = abs(entry_px - sl_px) / entry_px * 100.0
                    rew_p = (tp_px - entry_px) / entry_px * 100.0
                    rr_now = (rew_p / risk_p) if (risk_p > 0 and rew_p > 0) else 0.0
                    if rr_now >= cfg.min_rr:
                        alloc = cfg.boost_alloc_pct if risk_p <= cfg.tight_sl_thr else cfg.base_alloc_pct
                        margin = capital * (alloc / 100.0)
                        pos_usd = margin * cfg.leverage
                        active_trade = {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "mode": entry_mode,
                            "signal_time": ts,
                            "entry_time": ts,
                            "entry_idx": i,
                            "entry_price": entry_px,
                            "sl": sl_px,
                            "tp": tp_px,
                            "rr_planned": rr_now,
                            "risk_pct": risk_p,
                            "alloc_pct": alloc,
                            "margin_usd": margin,
                            "position_usd": pos_usd,
                        }
                elif entry_mode == "limit_poc":
                    if res.in_buy_zone:
                        active_trade = {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "mode": entry_mode,
                            "signal_time": ts,
                            "entry_time": ts,
                            "entry_idx": i,
                            "entry_price": res.entry_price,
                            "sl": res.sl_price,
                            "tp": res.tp_price,
                            "rr_planned": res.rr_ratio,
                            "risk_pct": res.risk_pct,
                            "alloc_pct": res.alloc_pct,
                            "margin_usd": res.margin_usd,
                            "position_usd": res.position_usd,
                        }
                    else:
                        pending_limit = {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "mode": entry_mode,
                            "signal_time": ts,
                            "created_idx": i,
                            "entry": res.poc_mid,
                            "sl": res.sl_price,
                            "tp": res.tp_price,
                            "rr_planned": res.rr_ratio,
                            "risk_pct": res.risk_pct,
                            "alloc_pct": res.alloc_pct,
                            "margin_usd": res.margin_usd,
                            "position_usd": res.position_usd,
                        }

        unrealized = 0.0
        if active_trade is not None:
            ep = active_trade["entry_price"]
            unrealized = active_trade["position_usd"] * ((cl - ep) / ep) - active_trade["position_usd"] * fee_rate
        equity_points.append((ts, capital + unrealized))

    if active_trade is not None:
        _close_trade(active_trade, times[-1], n - 1, float(c[-1]), "END_OF_DATA", fee_rate, trades)
        capital += active_trade["net_pnl_usd"]

    trades_df = pd.DataFrame(trades)
    equity_s = pd.Series(
        [v for _, v in equity_points],
        index=pd.DatetimeIndex([t for t, _ in equity_points], name="time"),
        name="equity",
    )
    summary = compute_summary(trades_df, equity_s, cfg, entry_mode, timeframe)
    return trades_df, equity_s, summary


def _close_trade(tr: dict, exit_ts: pd.Timestamp, exit_idx: int, exit_px: float,
                 reason: str, fee_rate: float, trades: list):
    ep = tr["entry_price"]
    pos_usd = tr["position_usd"]
    gross_ret = (exit_px - ep) / ep
    gross_usd = pos_usd * gross_ret
    fees_usd = pos_usd * fee_rate * 2.0
    net_usd = gross_usd - fees_usd
    risk_usd = pos_usd * (tr["risk_pct"] / 100.0)
    r_mult = net_usd / risk_usd if risk_usd > 0 else 0.0
    hold_hours = (exit_ts - tr["entry_time"]).total_seconds() / 3600.0
    tr.update({
        "exit_time": exit_ts,
        "exit_idx": exit_idx,
        "exit_price": exit_px,
        "exit_reason": reason,
        "gross_pnl_usd": round(gross_usd, 4),
        "fees_usd": round(fees_usd, 4),
        "net_pnl_usd": round(net_usd, 4),
        "r_multiple": round(r_mult, 3),
        "hold_hours": round(hold_hours, 1),
    })
    trades.append(tr)


def compute_summary(trades_df: pd.DataFrame, equity: pd.Series, cfg: Config, mode: str, timeframe: str) -> dict:
    init_cap = cfg.capital_total
    final_cap = float(equity.iloc[-1]) if len(equity) else init_cap
    dd = (equity / equity.cummax() - 1.0) * 100.0 if len(equity) else pd.Series([0.0])
    max_dd = float(dd.min()) if len(dd) else 0.0

    daily = equity.resample("1D").last().dropna() if len(equity) else pd.Series(dtype=float)
    rets = daily.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if len(rets) > 2 and rets.std() > 0 else 0.0

    if trades_df.empty:
        return {
            "modo": mode,
            "operaciones": 0,
            "win_rate_pct": 0.0,
            "rr_plan_promedio": 0.0,
            "avg_R": 0.0,
            "expectancy_usd": 0.0,
            "profit_factor": 0.0,
            "pnl_neto_usd": 0.0,
            "retorno_neto_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe": 0.0,
            "duracion_promedio_h": 0.0,
            "salidas": {},
        }

    pnl = trades_df["net_pnl_usd"].to_numpy()
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() < 0 else float("inf")

    return {
        "modo": mode,
        "operaciones": int(len(trades_df)),
        "win_rate_pct": round(float((pnl > 0).mean() * 100.0), 2),
        "rr_plan_promedio": round(float(trades_df["rr_planned"].mean()), 2),
        "avg_R": round(float(trades_df["r_multiple"].mean()), 3),
        "expectancy_usd": round(float(pnl.mean()), 2),
        "profit_factor": round(pf, 2),
        "pnl_neto_usd": round(float(pnl.sum()), 2),
        "retorno_neto_pct": round((final_cap / init_cap - 1.0) * 100.0, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "duracion_promedio_h": round(float(trades_df["hold_hours"].mean()), 1),
        "salidas": trades_df["exit_reason"].value_counts().to_dict(),
    }


def save_outputs(df: pd.DataFrame, trades_df: pd.DataFrame, equity: pd.Series,
                 out_dir: str, symbol: str, timeframe: str, mode: str):
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "backtest_trades.csv")
    if not trades_df.empty:
        cols = [
            "entry_time", "exit_time", "symbol", "timeframe", "mode",
            "entry_price", "tp", "sl", "rr_planned", "margin_usd", "position_usd",
            "net_pnl_usd", "r_multiple", "exit_reason", "hold_hours",
        ]
        trades_df[[c for c in cols if c in trades_df.columns]].to_csv(csv_path, index=False)
    else:
        pd.DataFrame().to_csv(csv_path, index=False)

    png_path = os.path.join(out_dir, "equity_curve.png")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw={"height_ratios": [1.2, 1.0]})
        sub_df = df.loc[equity.index[0]: equity.index[-1]]
        ax1.plot(sub_df.index, sub_df["close"], color="#90a4ae", linewidth=0.9, label=f"{symbol} ({timeframe})")
        if not trades_df.empty:
            ax1.scatter(pd.to_datetime(trades_df["entry_time"]), trades_df["entry_price"],
                        marker="^", color="#00e676", s=45, label="Entrada Long", zorder=5)
            win_m = trades_df["net_pnl_usd"] > 0
            ax1.scatter(pd.to_datetime(trades_df.loc[win_m, "exit_time"]), trades_df.loc[win_m, "exit_price"],
                        marker="o", color="#29b6f6", s=35, label="Salida Ganadora", zorder=5)
            ax1.scatter(pd.to_datetime(trades_df.loc[~win_m, "exit_time"]), trades_df.loc[~win_m, "exit_price"],
                        marker="x", color="#ff5252", s=40, label="Salida Perdedora", zorder=5)
        ax1.set_title(f"Backtest Golden POC & R:R 2:1 — {symbol} ({timeframe}) [{mode}]")
        ax1.set_ylabel("Precio (USDT)")
        ax1.legend(loc="upper left")
        ax1.grid(alpha=0.25)

        ax2.plot(equity.index, equity.values, color="#ffb300", linewidth=1.4, label="Capital (USD)")
        ax2.axhline(equity.iloc[0], color="#78909c", linestyle="--", linewidth=0.8)
        ax2.set_ylabel("Equity (USD)")
        ax2.set_xlabel("Fecha")
        ax2.legend(loc="upper left")
        ax2.grid(alpha=0.25)

        plt.tight_layout()
        fig.savefig(png_path, dpi=140)
        plt.close(fig)
    except Exception as e:
        print(f"[backtester] Aviso al generar PNG ({e})")
    return csv_path, png_path


def main():
    ap = argparse.ArgumentParser(description="Backtester Walk-Forward — Golden POC & R:R 2:1")
    ap.add_argument("--symbol", default="SOL/USDT")
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--days", type=int, default=180, help="Días hacia atrás si no se especifica --start")
    ap.add_argument("--start", default=None, help="Fecha inicio YYYY-MM-DD (sobreescribe --days)")
    ap.add_argument("--end", default=None, help="Fecha fin YYYY-MM-DD")
    ap.add_argument("--capital", type=float, default=DEFAULT_CONFIG.capital_total)
    ap.add_argument("--leverage", type=int, default=DEFAULT_CONFIG.leverage)
    ap.add_argument("--min-rr", type=float, default=DEFAULT_CONFIG.min_rr)
    ap.add_argument("--entry-mode", choices=["market_on_signal", "limit_poc", "both"], default="both")
    ap.add_argument("--max-hold-bars", type=int, default=DEFAULT_CONFIG.max_hold_bars)
    args = ap.parse_args()

    cfg = DEFAULT_CONFIG.override(
        capital_total=args.capital,
        leverage=args.leverage,
        min_rr=args.min_rr,
        max_hold_bars=args.max_hold_bars,
    )
    loader = DataLoader(cfg)

    if args.start:
        eval_start = pd.Timestamp(args.start, tz="UTC")
        hist_start = eval_start - pd.Timedelta(hours=(cfg.vp_lookback + 30) * (24 if args.timeframe == "1d" else 1))
        df = loader.load_ohlcv(args.symbol, args.timeframe, start=hist_start, end=args.end)
    else:
        df = loader.load_ohlcv(args.symbol, args.timeframe, days=args.days, end=args.end)
        eval_start = df.index[-1] - pd.Timedelta(days=args.days) if len(df) else None

    print(f"Datos cargados {args.symbol} ({args.timeframe}): {len(df)} velas ({df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d})")

    modes = ["market_on_signal", "limit_poc"] if args.entry_mode == "both" else [args.entry_mode]
    summaries = []
    for m in modes:
        trades_df, eq, summary = run_walk_forward(
            df, symbol=args.symbol, timeframe=args.timeframe,
            entry_mode=m, cfg=cfg, start_eval_ts=eval_start,
        )
        summaries.append(summary)
        csv_p, png_p = save_outputs(df, trades_df, eq, cfg.output_dir, args.symbol, args.timeframe, m)

    print("\n" + tabulate(summaries, headers="keys", tablefmt="github"))
    print(f"\nArchivos exportados en: {cfg.output_dir}")


if __name__ == "__main__":
    main()
