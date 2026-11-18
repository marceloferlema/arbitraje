"""Gestión de riesgo: tamaño de posición, margen y apalancamiento."""
import math


def position_size(entry, sl, tp, equity, cfg):
    sl_pct = abs(entry - sl) / entry
    tp_pct = abs(tp - entry) / entry
    # más margen cuanto más ajustado es el SL (interpolación lineal entre sl_tight y sl_wide)
    if sl_pct <= cfg.sl_tight_pct:
        margin_pct = cfg.margin_pct_max
    elif sl_pct >= cfg.sl_wide_pct:
        margin_pct = cfg.margin_pct_min
    else:
        f = (cfg.sl_wide_pct - sl_pct) / (cfg.sl_wide_pct - cfg.sl_tight_pct)
        margin_pct = cfg.margin_pct_min + f * (cfg.margin_pct_max - cfg.margin_pct_min)
    notional = equity * margin_pct * cfg.leverage_max
    max_risk = equity * cfg.max_risk_pct
    if notional * sl_pct > max_risk:
        notional = max_risk / sl_pct
    leverage = min(cfg.leverage_max, max(1, math.ceil(notional / (equity * margin_pct) - 1e-9)))
    margin = notional / leverage
    return {
        "equity": round(equity, 2),
        "margin_usd": round(margin, 2),
        "margin_pct": round(margin / equity, 4),
        "leverage": int(leverage),
        "notional_usd": round(notional, 2),
        "qty": notional / entry,
        "risk_usd": round(notional * sl_pct, 2),
        "risk_pct": round(notional * sl_pct / equity, 4),
        "reward_usd": round(notional * tp_pct, 2),
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
    }
