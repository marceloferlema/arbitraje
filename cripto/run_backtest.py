"""
Backtest de la estrategia de alertas cripto (individual y multi-par / cartera).

Ejemplos:
  python cripto/run_backtest.py                                                   # SOLUSDT (PRE / CONFIRMADA / AMBAS)
  python cripto/run_backtest.py --mode confirmed --start 2022-01-01               # SOLUSDT sólo CONFIRMADA
  python cripto/run_backtest.py --multi --mode confirmed --start 2022-01-01       # Cartera multi-par completa
  python cripto/run_backtest.py --symbols SOLUSDT,BTCUSDT,ETHUSDT,BNBUSDT,XRPUSDT --mode confirmed
"""
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

from config_cripto import DEFAULT
from cripto_alertas.backtest import Backtester, BacktestResult, compute_metrics, save_result
from cripto_alertas.data_source import BinanceFutures
from cripto_alertas.strategy import Engine


def parse():
    ap = argparse.ArgumentParser(description="Backtest estrategia barrido de liquidez + Volume Profile")
    ap.add_argument("--symbol", default=DEFAULT.symbol)
    ap.add_argument("--symbols", default=None, help="Lista separada por comas (ej: SOLUSDT,BTCUSDT,ETHUSDT)")
    ap.add_argument("--multi", action="store_true", help="Correr sobre todo el universo configurado en DEFAULT.universe")
    ap.add_argument("--start", default="2022-03-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--mode", choices=["pre", "confirmed", "both"], default="both")
    ap.add_argument("--capital", type=float, dest="capital_usd")
    ap.add_argument("--rr-min", type=float, dest="rr_min")
    ap.add_argument("--min-score", type=float, dest="min_score")
    ap.add_argument("--btc-filter", choices=["strict", "soft", "off"], dest="btc_filter")
    ap.add_argument("--squeeze-confirm", action="store_true", default=None, dest="squeeze_confirm")
    ap.add_argument("--vol-pct", type=float, dest="vol_percentile_min")
    ap.add_argument("--approach", type=float, dest="approach_pct")
    ap.add_argument("--sl-buffer", type=float, dest="sl_buffer_atr_d")
    ap.add_argument("--max-hold", type=int, dest="max_hold_days")
    ap.add_argument("--wick-min", type=float, dest="wick_reject_min")
    ap.add_argument("--be-r", type=float, dest="breakeven_r")
    ap.add_argument("--pivot-1h", type=int, dest="pivot_len_1h")
    ap.add_argument("--sweep-bars", type=int, dest="sweep_bars")
    ap.add_argument("--allow-counter-bias", action="store_true")
    ap.add_argument("--no-intrabar", action="store_true")
    ap.add_argument("--no-compound", action="store_true")
    ap.add_argument("--max-open", type=int, dest="max_open_positions")
    ap.add_argument("--out", default=None, help="carpeta de salida")
    ap.add_argument("--no-report", action="store_true")
    return ap.parse_args()


def _run_single_symbol(sym, cfg_base, ds, btc_h1, start, end, books, out_dir, no_report):
    cfg = cfg_base.override(symbol=sym)
    hist_start = start - pd.Timedelta(days=cfg.profile_lookback_days + 10)
    print(f"\nCargando datos {sym} desde {hist_start:%Y-%m-%d}...")
    d1 = ds.load_klines(sym, "1d", hist_start, end)
    h1 = ds.load_klines(sym, "1h", hist_start, end)
    funding = ds.load_funding(sym, start, end)
    print(f"  1D: {len(d1)} velas ({d1.index[0]:%Y-%m-%d} → {d1.index[-1]:%Y-%m-%d}) · 1H: {len(h1)} velas · funding: {len(funding)}")
    sym_start = max(start, h1.index[0] + pd.Timedelta(days=cfg.profile_min_days))

    engine = Engine(cfg, h1, btc_h1 if sym != cfg.btc_symbol else None)
    results = {}
    for kinds in books:
        label = "AMBAS" if len(kinds) == 2 else kinds[0]
        print(f"▶ Backtest {sym} · {label} ({sym_start:%Y-%m-%d} → {(end or h1.index[-1]):%Y-%m-%d})...")
        bt = Backtester(cfg, None if not cfg.intrabar_resolution else ds, d1, h1,
                        btc_h1 if sym != cfg.btc_symbol else None, funding, kinds, sym_start, end, label)
        res = bt.run(engine=engine)
        results[label] = (res, h1)
        if out_dir:
            save_result(res, out_dir, None if no_report else h1)
    return results


def _build_portfolio_result(per_symbol_results, cfg, label):
    all_trades = []
    all_signals = []
    eq_deltas = []
    for sym, res_map in per_symbol_results.items():
        if label not in res_map:
            continue
        res, _ = res_map[label]
        all_trades.extend(res.trades)
        all_signals.extend(res.signals)
        eq_deltas.append(res.equity - cfg.capital_usd)

    all_trades.sort(key=lambda t: t.created)
    all_signals.sort(key=lambda s: s.time)
    if eq_deltas:
        combined_delta = pd.concat(eq_deltas, axis=1).sort_index().ffill().fillna(0.0).sum(axis=1)
        portfolio_eq = cfg.capital_usd + combined_delta
        portfolio_eq.name = "equity"
    else:
        portfolio_eq = pd.Series([cfg.capital_usd], index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")]))

    summary = compute_metrics(all_trades, portfolio_eq, cfg, label=f"PORTFOLIO_{label}")
    summary["pares"] = list(per_symbol_results.keys())
    summary["por_par"] = {
        sym: {
            "trades": res_map[label][0].summary["trades_cerrados"],
            "win_rate": res_map[label][0].summary["win_rate"],
            "avg_R": res_map[label][0].summary["avg_R"],
            "profit_factor": res_map[label][0].summary["profit_factor"],
            "pnl_usd": res_map[label][0].summary["pnl_total_usd"],
        }
        for sym, res_map in per_symbol_results.items() if label in res_map
    }
    return BacktestResult(f"PORTFOLIO_{label}", all_trades, all_signals, portfolio_eq, summary, cfg)


def main():
    a = parse()
    exclude = {"start", "end", "mode", "out", "symbols", "multi", "no_intrabar", "no_compound", "allow_counter_bias", "no_report"}
    cfg = DEFAULT.override(**{k: v for k, v in vars(a).items() if k not in exclude})
    if a.no_intrabar:
        cfg = cfg.override(intrabar_resolution=False)
    if a.no_compound:
        cfg = cfg.override(compound=False)
    if a.allow_counter_bias:
        cfg = cfg.override(no_counter_bias=False)

    if a.symbols:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    elif a.multi:
        symbols = list(cfg.universe)
    else:
        symbols = [cfg.symbol.upper()]

    start = pd.Timestamp(a.start, tz="UTC")
    end = pd.Timestamp(a.end, tz="UTC") if a.end else None
    hist_start = start - pd.Timedelta(days=cfg.profile_lookback_days + 10)

    ds = BinanceFutures(cfg.data_dir)
    btc_h1 = ds.load_klines(cfg.btc_symbol, "1h", hist_start, end)

    books = {"pre": [("PRE",)], "confirmed": [("CONFIRMADA",)],
             "both": [("PRE",), ("CONFIRMADA",), ("PRE", "CONFIRMADA")]}[a.mode]

    tag_sym = "MULTI" if len(symbols) > 1 else symbols[0]
    out = a.out or os.path.join(cfg.reports_dir, f"bt_{tag_sym}_{datetime.now():%Y%m%d_%H%M%S}")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "config.json"), "w", encoding="utf-8") as f:
        d_cfg = cfg.to_dict()
        d_cfg["symbols_run"] = symbols
        json.dump(d_cfg, f, indent=2, default=str)

    if len(symbols) == 1:
        res_map = _run_single_symbol(symbols[0], cfg, ds, btc_h1, start, end, books, out, a.no_report)
        rows = []
        for kinds in books:
            label = "AMBAS" if len(kinds) == 2 else kinds[0]
            s = res_map[label][0].summary
            rows.append({k: s.get(k) for k in ("label", "señales", "trades_cerrados", "fill_rate_pre", "win_rate",
                                                "avg_R", "profit_factor", "pnl_total_usd", "retorno_pct",
                                                "max_drawdown_pct", "sharpe", "hold_promedio_h")})
            print(json.dumps(s, indent=2, ensure_ascii=False, default=str))
        print("\n" + "=" * 100)
        print(pd.DataFrame(rows).to_string(index=False))
        print(f"\nResultados en: {out}")
        return

    # Modo Multi-Par (Cartera)
    per_sym = {}
    rows = []
    for sym in symbols:
        sym_out = os.path.join(out, sym)
        os.makedirs(sym_out, exist_ok=True)
        res_map = _run_single_symbol(sym, cfg, ds, btc_h1, start, end, books, sym_out, a.no_report)
        per_sym[sym] = res_map
        for kinds in books:
            label = "AMBAS" if len(kinds) == 2 else kinds[0]
            s = res_map[label][0].summary
            rows.append({
                "par": sym, "modo": label, "señales": s["señales"], "trades": s["trades_cerrados"],
                "win_rate": s["win_rate"], "avg_R": s["avg_R"], "PF": s["profit_factor"],
                "pnl_usd": s["pnl_total_usd"], "ret_pct": s["retorno_pct"],
                "max_dd_pct": s["max_drawdown_pct"], "sharpe": s["sharpe"],
            })

    for kinds in books:
        label = "AMBAS" if len(kinds) == 2 else kinds[0]
        port_res = _build_portfolio_result(per_sym, cfg, label)
        save_result(port_res, out, None)
        ps = port_res.summary
        rows.append({
            "par": f"★ PORTFOLIO ({len(symbols)}p)", "modo": label,
            "señales": ps["señales"], "trades": ps["trades_cerrados"],
            "win_rate": ps["win_rate"], "avg_R": ps["avg_R"], "PF": ps["profit_factor"],
            "pnl_usd": ps["pnl_total_usd"], "ret_pct": ps["retorno_pct"],
            "max_dd_pct": ps["max_drawdown_pct"], "sharpe": ps["sharpe"],
        })
        print("\n" + json.dumps(ps, indent=2, ensure_ascii=False, default=str))

    print("\n" + "=" * 110)
    print(pd.DataFrame(rows).to_string(index=False))
    print(f"\nResultados consolidados en: {out}")


if __name__ == "__main__":
    main()
