"""
Capa de datos: Binance USDⓈ-M Futures (API pública, sin API key).

- Klines REST paginadas (hasta 1500 velas por request) con caché CSV incremental.
- Historial de funding rate (para costos en backtest).
- Velas 1m on-demand para resolver ambigüedades intravela en el backtest.
"""
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests

BASE_URL = "https://fapi.binance.com"
WS_URL = "wss://fstream.binance.com/stream"

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}
_KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
               "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
KEEP_COLS = ["open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base"]


def now_ms():
    return int(time.time() * 1000)


def to_ms(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return int(x)
    ts = pd.Timestamp(x)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp() * 1000)


def klines_to_df(rows):
    if not rows:
        df = pd.DataFrame(columns=KEEP_COLS, dtype=float)
        df.index = pd.DatetimeIndex([], tz="UTC", name="time")
        return df
    df = pd.DataFrame(rows, columns=_KLINE_COLS)
    df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df.index.name = "time"
    df = df[KEEP_COLS].astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


class BinanceFutures:
    name = "binance"

    def __init__(self, cache_dir, timeout=15):
        self.cache_dir = os.path.join(cache_dir, self.name)
        os.makedirs(self.cache_dir, exist_ok=True)
        self.session = requests.Session()
        self.timeout = timeout
        self._intrabar_cache_path = os.path.join(self.cache_dir, "intrabar_1m.json")
        self._intrabar_cache = None

    # ------------------------------------------------------------------ HTTP
    def _get(self, path, params, retries=5):
        for attempt in range(retries):
            try:
                r = self.session.get(BASE_URL + path, params=params, timeout=self.timeout)
                if r.status_code in (418, 429):
                    wait = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
                    print(f"[datos] rate limit ({r.status_code}), esperando {wait}s...")
                    time.sleep(wait)
                    continue
                if r.status_code == 451:
                    if path == "/fapi/v1/klines":
                        p2 = dict(params)
                        p2["limit"] = min(int(p2.get("limit", 1000)), 1000)
                        r2 = self.session.get("https://data-api.binance.vision/api/v3/klines",
                                              params=p2, timeout=self.timeout)
                        r2.raise_for_status()
                        return r2.json()
                    if path == "/fapi/v1/ticker/price":
                        r2 = self.session.get("https://data-api.binance.vision/api/v3/ticker/price",
                                              params=params, timeout=self.timeout)
                        r2.raise_for_status()
                        return r2.json()
                    return []
                r.raise_for_status()
                return r.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                wait = 2 * (attempt + 1)
                print(f"[datos] error de red ({e.__class__.__name__}), reintento en {wait}s...")
                time.sleep(wait)
        raise RuntimeError(f"No se pudo obtener {path} {params}")

    # --------------------------------------------------------------- Klines
    def fetch_klines(self, symbol, interval, start_ms, end_ms=None, verbose=False):
        step = INTERVAL_MS[interval]
        end_ms = end_ms or now_ms()
        rows, cur, n_req = [], int(start_ms), 0
        while cur < end_ms:
            data = self._get("/fapi/v1/klines", {
                "symbol": symbol, "interval": interval,
                "startTime": cur, "endTime": end_ms, "limit": 1500,
            })
            n_req += 1
            if not data:
                break
            rows.extend(data)
            nxt = int(data[-1][0]) + step
            if nxt <= cur:
                break
            cur = nxt
            if verbose and n_req % 20 == 0:
                print(f"[datos] {symbol} {interval}: {len(rows)} velas descargadas...")
            if len(data) < 1500:
                break
        return klines_to_df(rows)

    def _cache_file(self, symbol, interval):
        return os.path.join(self.cache_dir, f"{symbol}_{interval}.csv")

    def _read_cache(self, symbol, interval):
        path = self._cache_file(symbol, interval)
        if not os.path.exists(path):
            return None
        df = pd.read_csv(path, index_col="time")
        df.index = pd.to_datetime(df.index, format="ISO8601", utc=True)
        return df.astype(float).sort_index()

    def load_klines(self, symbol, interval, start=None, end=None, use_cache=True, verbose=True):
        """Devuelve velas CERRADAS en [start, end). Usa caché y descarga sólo lo faltante."""
        step = INTERVAL_MS[interval]
        start_ms = to_ms(start) if start is not None else now_ms() - 1000 * step
        end_ms = to_ms(end) if end is not None else now_ms()
        cached = self._read_cache(symbol, interval) if use_cache else None
        parts = []
        if cached is None or cached.empty:
            if verbose:
                print(f"[datos] descargando {symbol} {interval} desde {pd.Timestamp(start_ms, unit='ms', tz='UTC'):%Y-%m-%d}...")
            parts.append(self.fetch_klines(symbol, interval, start_ms, end_ms, verbose))
        else:
            parts.append(cached)
            first_ms = int(cached.index[0].timestamp() * 1000)
            last_ms = int(cached.index[-1].timestamp() * 1000)
            if start_ms < first_ms - step:
                if verbose:
                    print(f"[datos] completando historia previa {symbol} {interval}...")
                parts.append(self.fetch_klines(symbol, interval, start_ms, first_ms - 1, verbose))
            if end_ms > last_ms + 2 * step - 1:
                parts.append(self.fetch_klines(symbol, interval, last_ms + step, end_ms, verbose))
        df = pd.concat([p for p in parts if p is not None and not p.empty])
        df = df[~df.index.duplicated(keep="last")].sort_index()
        # sólo velas cerradas
        cutoff = pd.Timestamp(now_ms() - step, unit="ms", tz="UTC")
        df = df[df.index <= cutoff]
        if use_cache and not df.empty:
            df.to_csv(self._cache_file(symbol, interval), index_label="time")
        s = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        e = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        return df[(df.index >= s) & (df.index < e)].copy()

    # -------------------------------------------------------------- Funding
    def load_funding(self, symbol, start, end=None):
        path = os.path.join(self.cache_dir, f"{symbol}_funding.csv")
        start_ms, end_ms = to_ms(start), to_ms(end) or now_ms()
        cached = None
        if os.path.exists(path):
            cached = pd.read_csv(path, index_col="time")
            cached.index = pd.to_datetime(cached.index, format="ISO8601", utc=True)
        fetch_from = start_ms
        if cached is not None and not cached.empty:
            first = int(cached.index[0].timestamp() * 1000)
            if first <= start_ms + 8 * 3600_000:
                fetch_from = int(cached.index[-1].timestamp() * 1000) + 1
        rows, cur = [], fetch_from
        while cur < end_ms:
            data = self._get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": cur,
                                                       "endTime": end_ms, "limit": 1000})
            if not data:
                break
            rows.extend(data)
            cur = int(data[-1]["fundingTime"]) + 1
            if len(data) < 1000:
                break
        new = pd.DataFrame(rows)
        if not new.empty:
            new.index = pd.to_datetime(new["fundingTime"].astype("int64"), unit="ms", utc=True)
            new.index.name = "time"
            new = new[["fundingRate"]].astype(float).rename(columns={"fundingRate": "rate"})
        frames = [f for f in (cached, new) if f is not None and not f.empty]
        if not frames:
            return pd.Series(dtype=float)
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_csv(path, index_label="time")
        s = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        return df[df.index >= s]["rate"]

    # ------------------------------------------------- Intrabar (1m on-demand)
    def intrabar_1m(self, symbol, hour_open_ts):
        """Velas 1m de una hora concreta (para resolver SL/TP en la misma vela 1H)."""
        if self._intrabar_cache is None:
            self._intrabar_cache = {}
            if os.path.exists(self._intrabar_cache_path):
                try:
                    with open(self._intrabar_cache_path, "r", encoding="utf-8") as f:
                        self._intrabar_cache = json.load(f)
                except Exception:
                    self._intrabar_cache = {}
        key = f"{symbol}|{int(hour_open_ts.timestamp())}"
        if key not in self._intrabar_cache:
            start = int(hour_open_ts.timestamp() * 1000)
            data = self._get("/fapi/v1/klines", {"symbol": symbol, "interval": "1m",
                                                  "startTime": start, "endTime": start + 3600_000 - 1,
                                                  "limit": 60})
            self._intrabar_cache[key] = [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])]
                                         for r in data]
            with open(self._intrabar_cache_path, "w", encoding="utf-8") as f:
                json.dump(self._intrabar_cache, f)
        return self._intrabar_cache[key]

    # ------------------------------------------------------------ Varios
    def ticker_price(self, symbol):
        data = self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def funding_now(self, symbol):
        try:
            data = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
            return float(data.get("lastFundingRate", 0.0))
        except Exception:
            return None

    @staticmethod
    def ws_url(streams):
        return f"{WS_URL}?streams={'/'.join(streams)}"


def utc_now():
    return datetime.now(timezone.utc)
