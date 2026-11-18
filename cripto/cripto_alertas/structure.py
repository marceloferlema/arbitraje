"""Estructura de mercado: tendencia/rango diario, acumulación/distribución y último mín/máx relevante."""
from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .indicators import atr, pivots
from .volume_profile import build_profile


@dataclass
class DailyStructure:
    trend: str            # ALCISTA / BAJISTA / RANGO / INDEFINIDA
    bias: str             # ACUMULACION / DISTRIBUCION / NEUTRAL
    box_low: float
    box_high: float
    box_width_atr: float
    atr_d: float
    accdist_score: float
    delta_ratio: float
    updown_ratio: float
    last_pivot_high: float
    last_pivot_low: float
    swings_in_leg: int

    def preferred_direction(self):
        if self.bias == "ACUMULACION" or (self.trend == "ALCISTA" and self.bias != "DISTRIBUCION"):
            return "LONG"
        if self.bias == "DISTRIBUCION" or (self.trend == "BAJISTA" and self.bias != "ACUMULACION"):
            return "SHORT"
        return None

    def summary(self):
        if self.trend == "RANGO":
            s = f"RANGO {self.box_low:.2f}–{self.box_high:.2f}"
        else:
            s = self.trend
        return f"{s} · sesgo {self.bias} (score {self.accdist_score:+.2f})"

    def to_dict(self):
        return asdict(self)


def daily_structure(d1, cfg):
    k = cfg.pivot_len_1d
    a = atr(d1, 14)
    atr_d = float(a.iloc[-1]) if len(a) and not np.isnan(a.iloc[-1]) else float((d1["high"] - d1["low"]).mean())
    highs, lows = pivots(d1, k, k)
    n = len(d1)
    highs = highs[highs["confirm_idx"] <= n - 1]
    lows = lows[lows["confirm_idx"] <= n - 1]

    trend = "INDEFINIDA"
    if len(highs) >= 2 and len(lows) >= 2:
        h1_, h2_ = highs["price"].iloc[-2], highs["price"].iloc[-1]
        l1_, l2_ = lows["price"].iloc[-2], lows["price"].iloc[-1]
        if h2_ > h1_ and l2_ > l1_:
            trend = "ALCISTA"
        elif h2_ < h1_ and l2_ < l1_:
            trend = "BAJISTA"

    win = d1.iloc[-cfg.range_lookback_days:]
    box_low, box_high = float(win["low"].min()), float(win["high"].max())
    width_atr = (box_high - box_low) / atr_d if atr_d > 0 else np.inf
    if width_atr <= cfg.range_max_atr:
        trend = "RANGO"

    vol = win["volume"].sum()
    delta_ratio = float((2 * win["taker_buy_base"].sum() - vol) / vol) if vol > 0 else 0.0
    up = win.loc[win["close"] >= win["open"], "volume"].sum()
    updown = float((2 * up - vol) / vol) if vol > 0 else 0.0
    score = updown + 5 * delta_ratio
    if score > cfg.accdist_threshold:
        bias = "ACUMULACION"
    elif score < -cfg.accdist_threshold:
        bias = "DISTRIBUCION"
    else:
        bias = "NEUTRAL"

    # swings desde el último extremo mayor (aproximación al conteo de ondas)
    piv_all = pd.concat([highs.assign(kind="H"), lows.assign(kind="L")]).sort_values("idx")
    swings = 0
    if len(piv_all):
        recent = piv_all[piv_all["idx"] >= n - 90]
        kinds = recent["kind"].tolist()
        swings = sum(1 for i in range(1, len(kinds)) if kinds[i] != kinds[i - 1])

    return DailyStructure(
        trend=trend, bias=bias, box_low=box_low, box_high=box_high, box_width_atr=float(width_atr),
        atr_d=atr_d, accdist_score=float(score), delta_ratio=delta_ratio, updown_ratio=updown,
        last_pivot_high=float(highs["price"].iloc[-1]) if len(highs) else np.nan,
        last_pivot_low=float(lows["price"].iloc[-1]) if len(lows) else np.nan,
        swings_in_leg=swings,
    )


@dataclass
class Context:
    asof: pd.Timestamp       # inicio del día en que rige este contexto (todo lo usado es anterior)
    profile: object
    structure: DailyStructure

    @property
    def zones(self):
        return self.profile.zones


def build_context(d1_closed, h1_before, asof, cfg):
    """Construye el contexto diario usando sólo datos anteriores a `asof`."""
    start = asof - pd.Timedelta(days=cfg.profile_lookback_days)
    h1w = h1_before[(h1_before.index >= start) & (h1_before.index < asof)]
    d1w = d1_closed[(d1_closed.index >= start) & (d1_closed.index < asof)]
    if len(d1w) < cfg.profile_min_days or len(h1w) < cfg.profile_min_days * 20:
        return None
    prof = build_profile(h1w, d1w, bin_pct=cfg.profile_bin_pct, smooth_bins=cfg.profile_smooth_bins,
                         hvn_percentile=cfg.hvn_percentile, zone_expand=cfg.zone_expand,
                         zone_max_width_pct=cfg.zone_max_width_pct, zone_min_share=cfg.zone_min_share,
                         value_area_pct=cfg.value_area_pct)
    struct = daily_structure(d1w, cfg)
    return Context(asof=asof, profile=prof, structure=struct)
