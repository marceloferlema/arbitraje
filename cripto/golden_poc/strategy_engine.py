"""
Motor matemático puro (Pandas/NumPy) de la estrategia:
"Rebote Obligado en Golden POC + Clímax de Volumen + Filtro R:R 2:1".

Implementa exactamente las reglas especificadas sobre una ventana móvil de `vp_lookback = 200`
velas cerradas (cero look-ahead bias).
"""
from dataclasses import dataclass, asdict
import numpy as np
import pandas as pd

from config import Config, DEFAULT_CONFIG


@dataclass
class EvaluationResult:
    symbol: str
    timeframe: str
    timestamp: pd.Timestamp
    close: float
    lowest_low: float
    highest_high: float
    poc_low: float
    poc_high: float
    poc_mid: float
    res_low: float
    res_high: float
    in_buy_zone: bool
    price_escaped: bool
    touched_poc_floor: bool
    vol_climax: bool
    wick_reject_or_bull: bool
    sqz_val: float
    sqz_prev: float
    sqz_turning_up: bool
    trigger_signal: bool
    action_state: str
    entry_price: float
    sl_price: float
    tp_price: float
    risk_pct: float
    reward_pct: float
    rr_ratio: float
    rr_status: str
    approved: bool
    alloc_pct: float
    margin_usd: float
    position_usd: float
    est_win_usd: float
    est_loss_usd: float

    def to_dict(self):
        d = asdict(self)
        d["timestamp"] = pd.Timestamp(self.timestamp).isoformat()
        return d


def rolling_linreg_last(y: np.ndarray, n: int) -> np.ndarray:
    """Regresión lineal móvil de longitud `n` evaluada en el último punto (= ta.linreg(y, n, 0))."""
    y = np.asarray(y, dtype=float)
    mask = np.isnan(y)
    y0 = np.where(mask, 0.0, y)
    t = np.arange(n, dtype=float)
    sum_t, sum_tt = t.sum(), (t * t).sum()
    sum_y = pd.Series(y0).rolling(n).sum().to_numpy()
    w = np.arange(n, dtype=float)[::-1]
    sum_ty = np.convolve(y0, w, mode="full")[: len(y0)]
    denom = n * sum_tt - sum_t ** 2
    slope = (n * sum_ty - sum_t * sum_y) / denom
    intercept = (sum_y - slope * sum_t) / n
    out = intercept + slope * (n - 1)
    nan_any = pd.Series(mask.astype(float)).rolling(n).max().to_numpy()
    out[(nan_any > 0) | np.isnan(nan_any)] = np.nan
    return out


def compute_indicators(df: pd.DataFrame, cfg: Config = DEFAULT_CONFIG) -> dict[str, np.ndarray]:
    """Precalcula de forma estrictamente causal SMA de volumen y Squeeze Momentum (20 períodos)."""
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)

    vol_sma = pd.Series(v).rolling(cfg.vol_len).mean().to_numpy()
    vol_spike = v >= (vol_sma * cfg.vol_mult)

    hh = pd.Series(h).rolling(cfg.sqz_len).max().to_numpy()
    ll = pd.Series(l).rolling(cfg.sqz_len).min().to_numpy()
    sma_c = pd.Series(c).rolling(cfg.sqz_len).mean().to_numpy()
    sqz_basis = ((hh + ll) / 2.0 + sma_c) / 2.0
    sqz_val = rolling_linreg_last(c - sqz_basis, cfg.sqz_len)

    return {
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "vol_sma": vol_sma,
        "vol_spike": vol_spike,
        "sqz_val": sqz_val,
    }


def compute_volume_profile_window(
    w_low: np.ndarray,
    w_high: np.ndarray,
    w_vol: np.ndarray,
    vp_rows: int = 36,
    poc_zone_width: int = 1,
) -> tuple[float, float, float, float, float, float, float, float]:
    """
    Calcula el Perfil de Volumen sobre la ventana de `vp_lookback` velas y devuelve:
    (lowest_low, highest_high, range_span, poc_low, poc_high, poc_mid, res_low, res_high)
    """
    lowest_low = float(np.min(w_low))
    highest_high = float(np.max(w_high))
    range_span = highest_high - lowest_low
    if range_span <= 0.0:
        return lowest_low, highest_high, 0.0, lowest_low, highest_high, lowest_low, highest_high, highest_high

    price_step = range_span / vp_rows
    s_bins = np.clip(np.floor((w_low - lowest_low) / price_step).astype(int), 0, vp_rows - 1)
    e_bins = np.clip(np.floor((w_high - lowest_low) / price_step).astype(int), 0, vp_rows - 1)
    span_bins = np.maximum(1, e_bins - s_bins + 1)
    vol_per_bin = w_vol / span_bins

    diff = np.zeros(vp_rows + 1, dtype=float)
    np.add.at(diff, s_bins, vol_per_bin)
    np.add.at(diff, e_bins + 1, -vol_per_bin)
    bins = np.cumsum(diff)[:vp_rows]

    poc_bin = int(np.argmax(bins))
    poc_start_bin = max(0, poc_bin - poc_zone_width)
    poc_end_bin = min(vp_rows - 1, poc_bin + poc_zone_width)
    poc_low = lowest_low + poc_start_bin * price_step
    poc_high = lowest_low + (poc_end_bin + 1) * price_step
    poc_mid = (poc_low + poc_high) / 2.0

    search_start_bin = min(vp_rows - 2, poc_end_bin + 3)
    res_bin = min(vp_rows - 1, poc_end_bin + 6)
    if search_start_bin <= vp_rows - 1:
        sub = bins[search_start_bin:]
        if len(sub) > 0 and np.max(sub) > 0:
            res_bin = search_start_bin + int(np.argmax(sub))

    res_start_bin = max(poc_end_bin + 2, res_bin - poc_zone_width)
    res_end_bin = min(vp_rows - 1, res_bin + poc_zone_width)
    res_low = lowest_low + res_start_bin * price_step
    res_high = lowest_low + (res_end_bin + 1) * price_step

    return lowest_low, highest_high, range_span, poc_low, poc_high, poc_mid, res_low, res_high


def evaluate_bar(
    df: pd.DataFrame,
    idx: int,
    symbol: str = "SOL/USDT",
    timeframe: str = "1h",
    cfg: Config = DEFAULT_CONFIG,
    ind: dict[str, np.ndarray] | None = None,
    last_signal_idx: int = -999_999,
    capital_total: float | None = None,
    require_trigger: bool = False,
) -> EvaluationResult | None:
    """Evalúa la barra `idx` usando exclusivamente la sub-ventana `[idx - vp_lookback + 1 : idx + 1]`."""
    L_win = cfg.vp_lookback
    if idx < L_win - 1 or idx < 1:
        return None
    if ind is None:
        ind = compute_indicators(df, cfg)

    o = float(ind["open"][idx])
    h = float(ind["high"][idx])
    l = float(ind["low"][idx])
    c = float(ind["close"][idx])

    cooldown_ok = (idx - last_signal_idx) >= cfg.signal_cooldown
    sqz_val = float(ind["sqz_val"][idx]) if not np.isnan(ind["sqz_val"][idx]) else 0.0
    sqz_prev = float(ind["sqz_val"][idx - 1]) if not np.isnan(ind["sqz_val"][idx - 1]) else 0.0
    sqz_turning_up = (sqz_val < 0.0) and (sqz_val > sqz_prev)
    vb0 = max(0, idx - cfg.vol_lookback_bars + 1)
    vol_climax = bool(np.any(ind["vol_spike"][vb0: idx + 1]))
    c_range = h - l
    wick_reject_or_bull = (c_range > 0.0 and ((c - l) / c_range) >= cfg.wick_min_ratio) or (c > o)

    if require_trigger and not (cooldown_ok and sqz_turning_up and vol_climax and wick_reject_or_bull):
        return None

    w0 = idx - L_win + 1
    w_low = ind["low"][w0: idx + 1]
    w_high = ind["high"][w0: idx + 1]
    w_vol = ind["volume"][w0: idx + 1]

    lowest_low, highest_high, range_span, poc_low, poc_high, poc_mid, res_low, res_high = (
        compute_volume_profile_window(w_low, w_high, w_vol, cfg.vp_rows, cfg.poc_zone_width)
    )
    if range_span <= 0.0:
        return None

    # 2. Condiciones de Gatillo ("Rebote Obligado en POC")
    touched_poc_floor = (l <= poc_high) and (l <= lowest_low + range_span * cfg.floor_pct)
    trigger_signal = (
        touched_poc_floor and vol_climax and wick_reject_or_bull and sqz_turning_up and cooldown_ok
    )

    # 3. Cálculo de Niveles Operativos, Stop Loss Ajustado y Filtro R:R >= 2:1
    in_buy_zone = (poc_low * 0.99) <= c <= (poc_high * 1.01)
    price_escaped = c > (poc_high * 1.01)

    if in_buy_zone:
        entry_price = c
        action_state = "EN ZONA DE COMPRA - ENTRAR AHORA"
    elif price_escaped:
        entry_price = poc_mid
        action_state = "PRECIO ESCAPADO - PROGRAMAR ORDEN LIMIT EN POC"
    else:
        entry_price = c if c > 0 else poc_mid
        action_state = "ESPERAR CONFIRMACIÓN EN ZONA POC"

    sl_price = poc_low * (1.0 - cfg.sl_buffer_pct / 100.0)
    if sl_price >= entry_price:
        sl_price = entry_price * (1.0 - (cfg.sl_buffer_pct + 1.5) / 100.0)

    tp_price = entry_price + (res_low - entry_price) * (cfg.tp_safety_pct / 100.0)

    risk_pct = abs(entry_price - sl_price) / entry_price * 100.0 if entry_price > 0 else 0.0
    reward_pct = (tp_price - entry_price) / entry_price * 100.0 if entry_price > 0 else 0.0
    rr_ratio = (reward_pct / risk_pct) if (risk_pct > 0 and reward_pct > 0) else 0.0

    if rr_ratio >= cfg.min_rr:
        rr_status = f"APROBADO ({rr_ratio:.2f} : 1 >= {cfg.min_rr:.1f}:1)"
        approved = True
    elif rr_ratio > 1.0:
        rr_status = f"MARGINAL - NO OPERAR ({rr_ratio:.2f} : 1)"
        approved = False
    else:
        rr_status = f"RECHAZADO ({rr_ratio:.2f} : 1)"
        approved = False

    # 4. Gestión de Capital y Apalancamiento (2%-5% a 1x-3x)
    cap = capital_total if capital_total is not None else cfg.capital_total
    alloc_pct = cfg.boost_alloc_pct if (0.0 < risk_pct <= cfg.tight_sl_thr) else cfg.base_alloc_pct
    margin_usd = cap * (alloc_pct / 100.0)
    position_usd = margin_usd * cfg.leverage
    est_win_usd = position_usd * max(0.0, reward_pct / 100.0)
    est_loss_usd = position_usd * (risk_pct / 100.0)

    return EvaluationResult(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=df.index[idx],
        close=c,
        lowest_low=lowest_low,
        highest_high=highest_high,
        poc_low=poc_low,
        poc_high=poc_high,
        poc_mid=poc_mid,
        res_low=res_low,
        res_high=res_high,
        in_buy_zone=in_buy_zone,
        price_escaped=price_escaped,
        touched_poc_floor=touched_poc_floor,
        vol_climax=vol_climax,
        wick_reject_or_bull=wick_reject_or_bull,
        sqz_val=sqz_val,
        sqz_prev=sqz_prev,
        sqz_turning_up=sqz_turning_up,
        trigger_signal=trigger_signal,
        action_state=action_state,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        risk_pct=risk_pct,
        reward_pct=reward_pct,
        rr_ratio=rr_ratio,
        rr_status=rr_status,
        approved=approved,
        alloc_pct=alloc_pct,
        margin_usd=margin_usd,
        position_usd=position_usd,
        est_win_usd=est_win_usd,
        est_loss_usd=est_loss_usd,
    )
