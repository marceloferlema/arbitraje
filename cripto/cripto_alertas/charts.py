"""Gráficos HTML (plotly): zonas de Volume Profile y reporte de backtest."""
import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def plot_context(d1, ctx, path, desc=None, days=400, symbol=""):
    d = d1.iloc[-days:]
    prof = ctx.profile
    fig = make_subplots(rows=1, cols=2, shared_yaxes=True, column_widths=[0.82, 0.18], horizontal_spacing=0.01)
    fig.add_trace(go.Candlestick(x=d.index, open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                                 name=symbol), row=1, col=1)
    ymin, ymax = d["low"].min() * 0.9, d["high"].max() * 1.1
    for z in ctx.zones:
        if z.high < ymin or z.low > ymax:
            continue
        alpha = 0.10 + 0.35 * z.wall_score
        fig.add_hrect(y0=z.low, y1=z.high, fillcolor=f"rgba(255,193,7,{alpha:.2f})", line_width=0, row=1, col=1)
        fig.add_annotation(x=d.index[0], y=z.high, xanchor="left", yanchor="bottom", showarrow=False,
                           font=dict(size=10, color="#ffc107"),
                           text=f"{z.low:.2f}–{z.high:.2f} | vol {z.share*100:.1f}% | rebotes {z.rejections} / cruces {z.crosses}",
                           row=1, col=1)
    for lvl, name, color in ((prof.poc, "POC", "#ff5252"), (prof.vah, "VAH", "#64b5f6"), (prof.val, "VAL", "#64b5f6")):
        fig.add_hline(y=lvl, line_dash="dot", line_color=color, annotation_text=f"{name} {lvl:.2f}", row=1, col=1)
    st = ctx.structure
    if st.trend == "RANGO":
        fig.add_shape(type="rect", x0=d.index[-30], x1=d.index[-1], y0=st.box_low, y1=st.box_high,
                      line=dict(color="#90caf9", dash="dash"), row=1, col=1)
    if desc:
        for k, color in (("last_low", "#00e676"), ("last_high", "#ff1744")):
            if desc.get(k):
                fig.add_hline(y=desc[k][1], line_color=color, line_width=1,
                              annotation_text=f"{'último mín' if k == 'last_low' else 'último máx'} {desc[k][1]:.2f}",
                              row=1, col=1)
    centers = prof.centers
    mask = (centers >= ymin) & (centers <= ymax)
    fig.add_trace(go.Bar(x=prof.hist[mask], y=centers[mask], orientation="h", marker_color="#78909c",
                         name="Volume Profile"), row=1, col=2)
    fig.update_layout(template="plotly_dark", height=850, xaxis_rangeslider_visible=False,
                      title=f"{symbol} 1D · Volume Profile {ctx.asof:%Y-%m-%d} · {st.summary()}", showlegend=False)
    fig.update_yaxes(range=[ymin, ymax])
    fig.write_html(path, include_plotlyjs="cdn")
    return path


def backtest_report(res, h1, path):
    trades = [t for t in res.trades if t.fill_time is not None]
    eq = res.equity
    px = h1[(h1.index >= eq.index[0] - pd.Timedelta(hours=1)) & (h1.index <= eq.index[-1])]
    px4 = px["close"].resample("4h").last().dropna()
    dd = (eq / eq.cummax() - 1) * 100
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.55, 0.3, 0.15], vertical_spacing=0.03,
                        subplot_titles=("Precio y operaciones", "Equity (USD)", "Drawdown %"))
    fig.add_trace(go.Scatter(x=px4.index, y=px4.values, line=dict(color="#90a4ae", width=1), name="close 4H"),
                  row=1, col=1)
    for direction, sym, color in (("LONG", "triangle-up", "#00e676"), ("SHORT", "triangle-down", "#ff1744")):
        sub = [t for t in trades if t.direction == direction]
        if sub:
            fig.add_trace(go.Scatter(
                x=[t.fill_time for t in sub], y=[t.fill_price for t in sub], mode="markers",
                marker=dict(symbol=sym, size=10, color=color), name=f"entrada {direction}",
                text=[f"{t.kind} {t.direction}<br>E {t.fill_price:.2f} SL {t.sl:.2f} TP {t.tp:.2f}<br>{t.status} {t.r_multiple:+.2f}R"
                      for t in sub], hoverinfo="text"), row=1, col=1)
    ex = [t for t in trades if t.exit_time is not None]
    if ex:
        fig.add_trace(go.Scatter(
            x=[t.exit_time for t in ex], y=[t.exit_price for t in ex], mode="markers",
            marker=dict(symbol="x", size=8, color=["#00e676" if t.pnl > 0 else "#ff5252" for t in ex]),
            name="salida", text=[f"{t.status} {t.pnl:+.2f} USD ({t.r_multiple:+.2f}R)" for t in ex],
            hoverinfo="text"), row=1, col=1)
        for t in ex:
            fig.add_trace(go.Scatter(x=[t.fill_time, t.exit_time], y=[t.fill_price, t.exit_price], mode="lines",
                                     line=dict(color="#00e676" if t.pnl > 0 else "#ff5252", width=1),
                                     showlegend=False, hoverinfo="skip"), row=1, col=1)
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, line=dict(color="#ffc107"), name="equity"), row=2, col=1)
    fig.add_trace(go.Scatter(x=dd.index, y=dd.values, fill="tozeroy", line=dict(color="#ef5350"), name="DD"),
                  row=3, col=1)
    fig.update_layout(template="plotly_dark", height=1000, title=f"Backtest {res.cfg.symbol} · {res.label}")
    summary_html = "<pre style='color:#ddd;background:#111;padding:12px'>" + \
        json.dumps(res.summary, indent=2, ensure_ascii=False, default=str) + "</pre>"
    tdf = res.trades_df()
    if not tdf.empty:
        cols = [c for c in ["signal_id", "kind", "direction", "status", "created", "fill_time", "entry", "fill_price",
                            "sl", "tp", "exit_time", "exit_price", "pnl", "r_multiple", "fees", "funding"] if c in tdf]
        table_html = tdf[cols].round(4).to_html(index=False, classes="t", border=0)
    else:
        table_html = "<p>Sin operaciones</p>"
    html = f"""<html><head><meta charset='utf-8'><title>Backtest {res.label}</title>
<style>body{{background:#0d0f12;color:#ddd;font-family:Arial}} table.t{{border-collapse:collapse;font-size:12px}}
table.t td,table.t th{{padding:3px 8px;border-bottom:1px solid #333}}</style></head><body>
{fig.to_html(include_plotlyjs='cdn', full_html=False)}
<h3>Resumen</h3>{summary_html}<h3>Operaciones</h3>{table_html}</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path
