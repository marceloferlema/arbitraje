"""Indicadores causales (sin lookahead): ATR, pivots confirmados, percentil de volumen, Squeeze Momentum."""
import numpy as np
import pandas as pd


def true_range(df):
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
                     axis=1).max(axis=1)


def atr(df, n=14):
    return true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def pivots(df, left, right):
    """
    Pivots tipo fractal. Un pivot en la vela i sólo se CONFIRMA en la vela i+right.
    Devuelve dos arrays estructurados: highs y lows con columnas (idx, confirm_idx, price).
    """
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    win = left + right + 1
    roll_max = pd.Series(h).rolling(win).max().shift(-right).to_numpy()
    roll_min = pd.Series(l).rolling(win).min().shift(-right).to_numpy()
    ph = np.where(h == roll_max)[0]
    pl = np.where(l == roll_min)[0]

    def _dedupe(idx, values):
        # si hay valores iguales consecutivos dentro de la ventana, quedarse con el primero
        out = []
        for i in idx:
            if out and i - out[-1] <= right and values[i] == values[out[-1]]:
                continue
            out.append(i)
        return np.array(out, dtype=int)

    ph = _dedupe(ph, h)
    pl = _dedupe(pl, l)
    highs = pd.DataFrame({"idx": ph, "confirm_idx": ph + right, "price": h[ph] if len(ph) else []})
    lows = pd.DataFrame({"idx": pl, "confirm_idx": pl + right, "price": l[pl] if len(pl) else []})
    return highs, lows


def rolling_linreg_last(y, n):
    """Valor de la regresión lineal (ventana n) en el último punto, vectorizado (= ta.linreg(y, n, 0))."""
    y = np.asarray(y, dtype=float)
    mask = np.isnan(y)
    y0 = np.where(mask, 0.0, y)
    t = np.arange(n, dtype=float)
    sum_t, sum_tt = t.sum(), (t * t).sum()
    sum_y = pd.Series(y0).rolling(n).sum().to_numpy()
    w = np.arange(n, dtype=float)[::-1]          # pesos n-1..0 => sum_k k*y[i-n+1+k]
    sum_ty = np.convolve(y0, w, mode="full")[: len(y0)]
    denom = n * sum_tt - sum_t ** 2
    slope = (n * sum_ty - sum_t * sum_y) / denom
    intercept = (sum_y - slope * sum_t) / n
    out = intercept + slope * (n - 1)
    nan_any = pd.Series(mask.astype(float)).rolling(n).max().to_numpy()
    out[(nan_any > 0) | np.isnan(nan_any)] = np.nan
    return out


def squeeze_momentum(df, length=20, mult=2.0, length_kc=20, mult_kc=1.5):
    """Squeeze Momentum Indicator (LazyBear). Devuelve DataFrame con mom, sqz_on."""
    c, h, l = df["close"], df["high"], df["low"]
    basis = c.rolling(length).mean()
    dev = mult * c.rolling(length).std(ddof=0)
    upper_bb, lower_bb = basis + dev, basis - dev
    ma = c.rolling(length_kc).mean()
    rangema = true_range(df).rolling(length_kc).mean()
    upper_kc, lower_kc = ma + rangema * mult_kc, ma - rangema * mult_kc
    sqz_on = (lower_bb > lower_kc) & (upper_bb < upper_kc)
    mid = ((h.rolling(length_kc).max() + l.rolling(length_kc).min()) / 2 + c.rolling(length_kc).mean()) / 2
    mom = rolling_linreg_last((c - mid).to_numpy(), length_kc)
    return pd.DataFrame({"mom": mom, "sqz_on": sqz_on.to_numpy()}, index=df.index)


def volume_stats(df, window):
    v = df["volume"]
    pct = v.rolling(window, min_periods=max(48, window // 4)).rank(pct=True)
    med = v.rolling(window, min_periods=max(48, window // 4)).median()
    return pd.DataFrame({"vol_pct": pct, "vol_ratio": v / med}, index=df.index)
