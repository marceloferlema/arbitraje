"""
Descarga y caché local de datos OHLCV de Binance USD-M Futures.
Usa `ccxt` si está instalado y hace fallback automático al endpoint público REST de Binance Futures,
reutilizando también la caché existente en `cripto/data/binance` cuando está disponible.
"""
import os
import time
import pandas as pd
import requests

from config import DEFAULT_CONFIG

BASE_URL = "https://fapi.binance.com"
INTERVAL_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def normalize_symbol(symbol: str) -> str:
    """Convierte 'SOL/USDT:USDT' o 'SOL/USDT' a 'SOLUSDT'."""
    s = symbol.upper().split(":")[0]
    return s.replace("/", "").replace("-", "")


def ccxt_symbol(symbol: str) -> str:
    """Convierte 'SOLUSDT' o 'SOL/USDT' a formato ccxt swap 'SOL/USDT:USDT'."""
    raw = normalize_symbol(symbol)
    if raw.endswith("USDT"):
        base = raw[:-4]
        return f"{base}/USDT:USDT"
    return symbol


def _to_ms(x) -> int:
    ts = pd.Timestamp(x)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.timestamp() * 1000)


class DataLoader:
    def __init__(self, cfg=DEFAULT_CONFIG):
        self.cfg = cfg
        os.makedirs(self.cfg.data_dir, exist_ok=True)
        self.session = requests.Session()
        self._ccxt = None
        try:
            import ccxt  # type: ignore
            self._ccxt = ccxt.binanceusdm({"enableRateLimit": True})
        except Exception:
            self._ccxt = None

    def _cache_path(self, symbol: str, timeframe: str) -> str:
        raw = normalize_symbol(symbol)
        shared = os.path.join(self.cfg.shared_binance_dir, f"{raw}_{timeframe}.csv")
        if os.path.exists(shared):
            return shared
        return os.path.join(self.cfg.data_dir, f"{raw}_{timeframe}.csv")

    def _read_csv(self, path: str) -> pd.DataFrame | None:
        if not os.path.exists(path):
            return None
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, format="ISO8601", utc=True)
        df.index.name = "time"
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        return df[cols].astype(float).sort_index()

    def _fetch_batch(self, symbol: str, timeframe: str, since_ms: int, end_ms: int) -> list:
        if self._ccxt is not None:
            try:
                rows = self._ccxt.fetch_ohlcv(
                    ccxt_symbol(symbol), timeframe=timeframe, since=since_ms, limit=1500,
                    params={"endTime": end_ms},
                )
                return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows]
            except Exception:
                pass
        r = self.session.get(
            f"{BASE_URL}/fapi/v1/klines",
            params={
                "symbol": normalize_symbol(symbol),
                "interval": timeframe,
                "startTime": since_ms,
                "endTime": end_ms,
                "limit": 1500,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in data]

    def fetch_ohlcv_range(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        step = INTERVAL_MS[timeframe]
        cur = int(start_ms)
        rows = []
        while cur < end_ms:
            batch = self._fetch_batch(symbol, timeframe, cur, end_ms)
            if not batch:
                break
            rows.extend(batch)
            nxt = int(batch[-1][0]) + step
            if nxt <= cur:
                break
            cur = nxt
            if len(batch) < 1500:
                break
        if not rows:
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
            df.index = pd.DatetimeIndex([], tz="UTC", name="time")
            return df
        df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
        df.index.name = "time"
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        return df[~df.index.duplicated(keep="last")].sort_index()

    def load_ohlcv(self, symbol: str, timeframe: str = "1h", start=None, end=None,
                   days: int | None = None, limit_bars: int | None = None, use_cache: bool = True) -> pd.DataFrame:
        step = INTERVAL_MS[timeframe]
        now_ms = int(time.time() * 1000)
        end_ms = _to_ms(end) if end else now_ms
        if start is not None:
            start_ms = _to_ms(start)
        elif days is not None:
            start_ms = end_ms - int(days * 86_400_000) - 220 * step
        elif limit_bars is not None:
            start_ms = end_ms - int((limit_bars + 10) * step)
        else:
            start_ms = end_ms - 365 * 86_400_000

        path = self._cache_path(symbol, timeframe)
        cached = self._read_csv(path) if use_cache else None
        parts = []
        if cached is None or cached.empty:
            parts.append(self.fetch_ohlcv_range(symbol, timeframe, start_ms, end_ms))
        else:
            parts.append(cached)
            first_ms = int(cached.index[0].timestamp() * 1000)
            last_ms = int(cached.index[-1].timestamp() * 1000)
            if start_ms < first_ms - step:
                parts.append(self.fetch_ohlcv_range(symbol, timeframe, start_ms, first_ms - 1))
            if end_ms > last_ms + 2 * step:
                parts.append(self.fetch_ohlcv_range(symbol, timeframe, last_ms + step, end_ms))

        df = pd.concat([p for p in parts if p is not None and not p.empty])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        cutoff = pd.Timestamp(now_ms - step, unit="ms", tz="UTC")
        df = df[df.index <= cutoff]

        local_out = os.path.join(self.cfg.data_dir, f"{normalize_symbol(symbol)}_{timeframe}.csv")
        if use_cache and not df.empty and not os.path.exists(path):
            df.to_csv(local_out, index_label="time")

        s_ts = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        e_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        df = df[(df.index >= s_ts) & (df.index <= e_ts)].copy()
        if limit_bars is not None and len(df) > limit_bars:
            df = df.iloc[-limit_bars:].copy()
        return df
