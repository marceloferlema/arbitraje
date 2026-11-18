"""Backtest: replay vela a vela (1H) sin lookahead usando el mismo motor que el modo live."""
import json
import os

import numpy as np
import pandas as pd

from .strategy import Engine, EngineState
from .structure import build_context
from .trades import Trade, TradeSimulator

H1 = pd.Timedelta(hours=1)
M1 = pd.Timedelta(minutes=1)


def trade_from_signal(s, cfg):
    return Trade(
        signal_id=s.id, kind=s.kind, direction=s.direction, entry=s.entry, sl=s.sl, tp=s.tp,
        qty=s.sizing["qty"], notional=s.sizing["notional_usd"], risk_usd=s.sizing["risk_usd"],
        created=s.time, valid_until=s.valid_until, max_hold=pd.Timedelta(days=cfg.max_hold_days),
        zone_key=s.zone["key"], market_entry=(s.entry_type == "MERCADO"),
    )


class Backtester:
    def __init__(self, cfg, ds, d1, h1, btc_h1=None, funding=None, kinds=("PRE", "CONFIRMADA"),
                 start=None, end=None, label="", verbose=True):
        self.cfg, self.ds, self.d1, self.h1, self.btc_h1 = cfg, ds, d1, h1, btc_h1
        self.kinds = tuple(kinds)
        self.start = pd.Timestamp(start, tz="UTC") if start and pd.Timestamp(start).tzinfo is None else (
            pd.Timestamp(start) if start else h1.index[0])
        self.end = pd.Timestamp(end, tz="UTC") if end and pd.Timestamp(end).tzinfo is None else (
            pd.Timestamp(end) if end else h1.index[-1] + H1)
        self.label = label or "+".join(self.kinds)
        self.verbose = verbose
        self.funding = {}
        if funding is not None and len(funding):
            for ts, r in funding.items():
                self.funding[ts.floor("h")] = float(r)

    def run(self, engine=None):
        cfg, h1 = self.cfg, self.h1
        eng = engine or Engine(cfg, h1, self.btc_h1)
        state = EngineState()
        sim = TradeSimulator(cfg)
        times = h1.index
        start_pos = int(times.searchsorted(self.start))
        end_pos = int(times.searchsorted(self.end))
        O, Hh, L, C = eng.o, eng.h, eng.l, eng.c
        realized = 0.0
        trades, active, signals, curve = [], [], [], []
        ctx, ctx_day = None, None
        ctx_cache = getattr(eng, "_ctx_cache", None)
        if ctx_cache is None:
            ctx_cache = {}
            eng._ctx_cache = ctx_cache
        n_intrabar = 0
        for t in range(start_pos, end_pos):
            ts = times[t]
            bar_end = ts + H1
            day = ts.normalize()
            if day != ctx_day:
                if day not in ctx_cache:
                    ctx_cache[day] = build_context(self.d1, h1, day, cfg)
                ctx = ctx_cache[day]
                ctx_day = day
            o, h, l, c = O[t], Hh[t], L[t], C[t]

            rate = self.funding.get(ts)
            if rate:
                for tr in active:
                    sim.apply_funding(tr, rate, o)

            for tr in list(active):
                mo = tr.market_entry and tr.status == "PENDIENTE"
                n_open = sum(1 for x in active if x is not tr and x.status == "ABIERTA")
                can_fill = n_open < cfg.max_open_positions
                subs = None
                if cfg.intrabar_resolution and self.ds is not None and sim.ambiguous(tr, o, h, l, mo):
                    try:
                        subs = self.ds.intrabar_1m(cfg.symbol, ts)
                    except Exception as e:
                        if self.verbose:
                            print(f"[backtest] sin 1m para {ts}: {e}")
                if subs:
                    tr.resolved_intrabar = True
                    n_intrabar += 1
                    first = True
                    for ms, so, sh, sl_, sc in subs:
                        sts = pd.Timestamp(ms, unit="ms", tz="UTC")
                        sim.step(tr, sts, so, sh, sl_, sc, sts + M1, market_open=mo and first, can_fill=can_fill)
                        first = False
                        if tr.is_closed:
                            break
                else:
                    sim.step(tr, ts, o, h, l, c, bar_end, market_open=mo, can_fill=can_fill)
                if tr.is_closed:
                    active.remove(tr)
                    if tr.kind == "PRE":
                        state.release_pre(tr.direction, tr.zone_key, bar_end, cfg.zone_cooldown_hours)
                    if tr.status in ("TP", "SL", "TIEMPO"):
                        realized += tr.pnl

            equity = cfg.capital_usd + (realized if cfg.compound else 0.0)
            for s in eng.evaluate(t, ctx, state, self.kinds, equity=equity):
                signals.append(s)
                tr = trade_from_signal(s, cfg)
                active.append(tr)
                trades.append(tr)

            unreal = sum(tr.sign * tr.qty * (c - tr.fill_price) + tr.realized_partial_pnl - tr.fees - tr.funding
                         for tr in active if tr.status == "ABIERTA")
            curve.append((bar_end, cfg.capital_usd + realized + unreal))

        # cerrar lo abierto al final a precio de cierre (sólo para métricas)
        for tr in active:
            if tr.status == "ABIERTA":
                sim._close(tr, C[end_pos - 1], times[end_pos - 1] + H1, "TIEMPO", maker=False)
                tr.events.append(("FIN_BACKTEST", times[end_pos - 1] + H1, C[end_pos - 1]))
                realized += tr.pnl
        equity_curve = pd.Series([v for _, v in curve], index=pd.DatetimeIndex([k for k, _ in curve]), name="equity")
        summary = compute_metrics(trades, equity_curve, cfg, self.label)
        summary["intrabar_resueltas"] = n_intrabar
        return BacktestResult(self.label, trades, signals, equity_curve, summary, cfg)


class BacktestResult:
    def __init__(self, label, trades, signals, equity, summary, cfg):
        self.label, self.trades, self.signals, self.equity, self.summary, self.cfg = \
            label, trades, signals, equity, summary, cfg

    def trades_df(self):
        rows = []
        for t in self.trades:
            d = t.to_dict()
            d.pop("events", None)
            rows.append(d)
        return pd.DataFrame(rows)

    def signals_df(self):
        rows = []
        for s in self.signals:
            d = s.to_dict()
            d["zone_low"], d["zone_high"] = s.zone["low"], s.zone["high"]
            d["zone_wall"], d["target_low"], d["target_high"] = s.zone["wall_score"], s.target_zone["low"], s.target_zone["high"]
            for k in ("zone", "target_zone", "sizing"):
                d.pop(k, None)
            d.update({f"size_{k}": v for k, v in s.sizing.items()})
            rows.append(d)
        return pd.DataFrame(rows)


def compute_metrics(trades, equity, cfg, label=""):
    closed = [t for t in trades if t.status in ("TP", "SL", "TIEMPO")]
    n_sig = len(trades)
    pnl = np.array([t.pnl for t in closed]) if closed else np.array([])
    rs = np.array([t.r_multiple for t in closed]) if closed else np.array([])
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    eq = equity if len(equity) else pd.Series([cfg.capital_usd])
    dd = (eq / eq.cummax() - 1)
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9) if len(eq) > 1 else 1e-9
    final = float(eq.iloc[-1])
    holds = [(t.exit_time - t.fill_time).total_seconds() / 3600 for t in closed]
    by = {}
    for key in ("PRE", "CONFIRMADA"):
        for d in ("LONG", "SHORT"):
            sub = [t for t in closed if t.kind == key and t.direction == d]
            if sub:
                by[f"{key}_{d}"] = {
                    "trades": len(sub),
                    "win_rate": round(sum(1 for t in sub if t.pnl > 0) / len(sub), 3),
                    "avg_R": round(float(np.mean([t.r_multiple for t in sub])), 3),
                    "pnl": round(float(sum(t.pnl for t in sub)), 2),
                }
    pre = [t for t in trades if t.kind == "PRE"]
    pre_filled = [t for t in pre if t.fill_time is not None]
    status_counts = pd.Series([t.status for t in trades]).value_counts().to_dict() if trades else {}
    return {
        "label": label,
        "desde": str(eq.index[0]) if len(eq) else None,
        "hasta": str(eq.index[-1]) if len(eq) else None,
        "señales": n_sig,
        "estados": status_counts,
        "trades_cerrados": len(closed),
        "fill_rate_pre": round(len(pre_filled) / len(pre), 3) if pre else None,
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "tp_rate": round(sum(1 for t in closed if t.status == "TP") / len(closed), 3) if closed else None,
        "avg_R": round(float(rs.mean()), 3) if len(rs) else None,
        "expectancy_usd": round(float(pnl.mean()), 2) if len(pnl) else None,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if len(losses) and losses.sum() < 0 else None,
        "pnl_total_usd": round(float(pnl.sum()), 2),
        "capital_final": round(final, 2),
        "retorno_pct": round((final / cfg.capital_usd - 1) * 100, 2),
        "cagr_pct": round(((final / cfg.capital_usd) ** (1 / years) - 1) * 100, 2) if final > 0 else None,
        "max_drawdown_pct": round(float(dd.min()) * 100, 2),
        "sharpe": round(float(rets.mean() / rets.std() * np.sqrt(365)), 2) if len(rets) > 2 and rets.std() > 0 else None,
        "hold_promedio_h": round(float(np.mean(holds)), 1) if holds else None,
        "fees_usd": round(float(sum(t.fees for t in closed)), 2),
        "funding_usd": round(float(sum(t.funding for t in closed)), 2),
        "por_tipo": by,
    }


def save_result(res, out_dir, h1=None):
    os.makedirs(out_dir, exist_ok=True)
    tag = res.label.replace("+", "_").lower()
    res.trades_df().to_csv(os.path.join(out_dir, f"trades_{tag}.csv"), index=False)
    res.signals_df().to_csv(os.path.join(out_dir, f"signals_{tag}.csv"), index=False)
    res.equity.to_csv(os.path.join(out_dir, f"equity_{tag}.csv"), index_label="time")
    with open(os.path.join(out_dir, f"summary_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(res.summary, f, indent=2, ensure_ascii=False, default=str)
    if h1 is not None:
        try:
            from .charts import backtest_report
            backtest_report(res, h1, os.path.join(out_dir, f"report_{tag}.html"))
        except ImportError:
            pass
