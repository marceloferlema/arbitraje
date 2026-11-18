"""
Modo live: WebSocket de Binance Futures + motor de señales + seguimiento de operaciones.

- Cada cierre de vela 1H: refresca datos (REST incremental), recalcula contexto si cambió el día (UTC)
  y evalúa señales PRE y CONFIRMADA.
- Cada cierre de vela 1m: evalúa PRE-ALERTAS intravela (con la vela 1H en curso) y hace el seguimiento
  de las alertas emitidas (fill de la limit, TP, SL, time-stop, expiración).
"""
import json
import os
import queue
import threading
import time
import traceback

import pandas as pd
import websocket

from .alerts import Notifier, format_event, format_signal, fmt_ts, _p
from .backtest import trade_from_signal
from .data_source import BinanceFutures
from .strategy import Engine, EngineState
from .structure import build_context
from .trades import Trade, TradeSimulator

M1 = pd.Timedelta(minutes=1)
H1 = pd.Timedelta(hours=1)
ENGINE_BARS = 24 * 80


class LiveRunner:
    def __init__(self, cfg, telegram=False, kinds=("PRE", "CONFIRMADA"), intrabar_pre=True, executor=None, chat_id=None):
        self.cfg = cfg
        self.kinds = tuple(kinds)
        self.intrabar_pre = intrabar_pre
        self.executor = executor
        self.ds = BinanceFutures(cfg.data_dir)
        self.notifier = Notifier(telegram=telegram, log_dir=cfg.alerts_dir, chat_id=chat_id)
        if self.executor is not None and self.executor.notifier is None:
            self.executor.notifier = self.notifier
        self.sim = TradeSimulator(cfg)
        self.state_path = os.path.join(cfg.alerts_dir, f"state_{cfg.symbol}.json")
        self.state = EngineState()
        self.trades = []
        self.ctx = None
        self.ctx_day = None
        self.partial_1h = None
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.last_msg = time.time()

    # ------------------------------------------------------------------ datos
    def _hist_start(self):
        return pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=self.cfg.profile_lookback_days + 15)

    def refresh_data(self):
        s = self._hist_start()
        self.d1 = self.ds.load_klines(self.cfg.symbol, "1d", s, verbose=False)
        self.h1 = self.ds.load_klines(self.cfg.symbol, "1h", s, verbose=False)
        self.btc = (self.ds.load_klines(self.cfg.btc_symbol, "1h", s, verbose=False)
                    if self.cfg.symbol != self.cfg.btc_symbol else None)

    def refresh_context(self, force=False):
        day = pd.Timestamp.now(tz="UTC").normalize()
        if force or day != self.ctx_day:
            ctx = build_context(self.d1, self.h1, day, self.cfg)
            if ctx is None:
                raise RuntimeError("Historia insuficiente para construir el contexto")
            self.ctx, self.ctx_day = ctx, day
            print(f"[live] contexto diario recalculado ({day:%Y-%m-%d}): {len(ctx.zones)} zonas · {ctx.structure.summary()}")

    def engine(self, partial=None):
        h1 = self.h1.tail(ENGINE_BARS)
        if partial is not None:
            h1 = pd.concat([h1, partial])
            h1 = h1[~h1.index.duplicated(keep="last")]
        btc = self.btc.tail(ENGINE_BARS + 5) if self.btc is not None else None
        return Engine(self.cfg, h1, btc)

    # ---------------------------------------------------------------- estado
    def load_state(self):
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            self.state = EngineState.from_json(d.get("engine", {}))
            self.trades = [Trade.from_dict(t) for t in d.get("trades", [])]
            print(f"[live] estado restaurado: {len(self.trades)} alertas activas")

    def save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        active = [t for t in self.trades if not t.is_closed]
        data = {"engine": self.state.to_json(), "trades": [t.to_dict() for t in active]}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, self.state_path)

    # --------------------------------------------------------------- señales
    def handle_signals(self, sigs):
        for s in sigs:
            funding = self.ds.funding_now(self.cfg.symbol)
            self.notifier.send(format_signal(s, funding))
            self.notifier.log_signal(s)
            tr = trade_from_signal(s, self.cfg)
            n_open = sum(1 for x in self.trades if not x.is_closed and x.status == "ABIERTA")
            can_fill = n_open < self.cfg.max_open_positions
            if tr.market_entry:
                if can_fill:
                    self.sim._fill(tr, s.entry, s.time, maker=False)
                    if self.executor is not None:
                        self.executor.on_signal(s, tr, self.cfg)
                else:
                    tr.status = "BLOQUEADA"
                    ev = ("BLOQUEADA", s.time, s.entry)
                    tr.events.append(ev)
                    self.notifier.send(format_event(tr, ev))
            else:
                if self.executor is not None and can_fill:
                    self.executor.on_signal(s, tr, self.cfg)
            self.trades.append(tr)
        if sigs:
            self.save_state()

    def track(self, ts, o, h, l, c):
        changed = False
        for tr in [t for t in self.trades if not t.is_closed]:
            n_open = sum(1 for x in self.trades if x is not tr and not x.is_closed and x.status == "ABIERTA")
            can_fill = n_open < self.cfg.max_open_positions
            events = self.sim.step(tr, ts, o, h, l, c, ts + M1, market_open=False, can_fill=can_fill)
            for ev in events:
                changed = True
                self.notifier.send(format_event(tr, ev))
                self.notifier.log_event(tr, ev)
                if self.executor is not None:
                    self.executor.on_event(tr, ev, self.cfg)
            if tr.is_closed and tr.kind == "PRE":
                self.state.release_pre(tr.direction, tr.zone_key, ts, self.cfg.zone_cooldown_hours)
        if changed:
            self.trades = [t for t in self.trades if not t.is_closed]
            self.save_state()

    # ---------------------------------------------------------------- eventos
    def on_hour_close(self):
        time.sleep(3)
        self.refresh_data()
        self.refresh_context()
        eng = self.engine()
        t = len(eng.times) - 1
        sigs = eng.evaluate(t, self.ctx, self.state, self.kinds, equity=self.cfg.capital_usd)
        self.handle_signals(sigs)
        self.save_state()
        print(status_text(eng, t, self.ctx, self.trades, self.cfg), flush=True)

    def on_minute_close(self, k):
        ts = pd.Timestamp(int(k["t"]), unit="ms", tz="UTC")
        o, h, l, c = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"])
        self.track(ts, o, h, l, c)
        if self.intrabar_pre and "PRE" in self.kinds and self.partial_1h is not None and self.ctx is not None:
            eng = self.engine(self.partial_1h)
            t = len(eng.times) - 1
            now = ts + M1
            sigs = eng.evaluate(t, self.ctx, self.state, ("PRE",), equity=self.cfg.capital_usd,
                                partial=True, now=now)
            self.handle_signals(sigs)

    def _set_partial(self, k):
        ts = pd.Timestamp(int(k["t"]), unit="ms", tz="UTC")
        self.partial_1h = pd.DataFrame([{
            "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]), "close": float(k["c"]),
            "volume": float(k["v"]), "quote_volume": float(k["q"]), "trades": float(k["n"]),
            "taker_buy_base": float(k["V"]),
        }], index=pd.DatetimeIndex([ts], name="time"))

    # -------------------------------------------------------------- WebSocket
    def _on_message(self, ws, msg):
        self.last_msg = time.time()
        try:
            data = json.loads(msg).get("data", {})
            k = data.get("k")
            if not k:
                return
            if k["i"] == "1h":
                self._set_partial(k)
                if k["x"]:
                    self.q.put(("1h", k))
            elif k["i"] == "1m" and k["x"]:
                self.q.put(("1m", k))
        except Exception as e:
            print(f"[ws] error procesando mensaje: {e}")

    def _worker(self):
        while not self.stop.is_set():
            try:
                kind, k = self.q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if kind == "1h":
                    self.on_hour_close()
                else:
                    self.on_minute_close(k)
            except Exception:
                print("[live] error en worker:\n" + traceback.format_exc())

    def _ws_loop(self):
        sym = self.cfg.symbol.lower()
        url = BinanceFutures.ws_url([f"{sym}@kline_1m", f"{sym}@kline_1h"])
        backoff = 2
        while not self.stop.is_set():
            ws = websocket.WebSocketApp(
                url, on_message=self._on_message,
                on_open=lambda w: print(f"[ws] conectado {url}"),
                on_error=lambda w, e: print(f"[ws] error: {e}"),
                on_close=lambda w, c, m: print(f"[ws] desconectado ({c})"))
            t0 = time.time()
            ws.run_forever(ping_interval=30, ping_timeout=10)
            if self.stop.is_set():
                break
            if time.time() - t0 > 60:
                backoff = 2
            print(f"[ws] reconectando en {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            try:  # backfill por si nos perdimos el cierre de una vela 1H
                self.q.put(("1h", None))
            except Exception:
                pass

    def bootstrap(self):
        print(f"[live] cargando historia {self.cfg.symbol} / {self.cfg.btc_symbol}...")
        self.refresh_data()
        self.refresh_context(force=True)
        self.load_state()

    def run(self):
        self.bootstrap()
        eng = self.engine()
        t = len(eng.times) - 1
        print(status_text(eng, t, self.ctx, self.trades, self.cfg), flush=True)
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._ws_loop, daemon=True).start()
        print("[live] monitoreando en tiempo real. Ctrl+C para salir.")
        try:
            while True:
                time.sleep(30)
                if time.time() - self.last_msg > 180:
                    print("[live] ⚠️ sin mensajes del WebSocket hace más de 3 minutos")
        except KeyboardInterrupt:
            print("\n[live] deteniendo...")
            self.stop.set()
            self.save_state()


def status_text(eng, t, ctx, trades, cfg):
    d = eng.describe(t, ctx)
    ts = eng.times[t] + H1
    lines = [f"\n──── {cfg.symbol} · {fmt_ts(ts)} · precio {_p(d['price'])} ────",
             f"Estructura 1D: {ctx.structure.summary()} · ATR 1D {_p(ctx.structure.atr_d)} · "
             f"POC {_p(ctx.profile.poc)} · VA {_p(ctx.profile.val)}–{_p(ctx.profile.vah)}"]
    if d["last_low"]:
        lines.append(f"Último mínimo relevante (liquidez abajo): {_p(d['last_low'][1])} ({fmt_ts(d['last_low'][0])})")
    if d["last_high"]:
        lines.append(f"Último máximo relevante (liquidez arriba): {_p(d['last_high'][1])} ({fmt_ts(d['last_high'][0])})")
    for name, zs in (("Zonas abajo", d["zones_below"]), ("Zonas arriba", d["zones_above"])):
        txt = " | ".join(f"{_p(z['low'])}–{_p(z['high'])} ({(z['mid']/d['price']-1)*100:+.1f}%, vol {z['share']*100:.1f}%, "
                         f"muralla {z['wall_score']:.2f})" for z in zs)
        lines.append(f"{name}: {txt or '-'}")
    vp = f"{d['vol_pct']*100:.0f}" if d["vol_pct"] is not None else "-"
    lines.append(f"Volumen 1H percentil {vp} · Squeeze {'ON' if d['sqz_on'] else 'OFF'} · momentum {d['mom_dir']}")
    act = [tr for tr in trades if not tr.is_closed]
    if act:
        for tr in act:
            lines.append(f"  · {tr.signal_id} {tr.status} E {_p(tr.entry)} SL {_p(tr.sl)} TP {_p(tr.tp)}")
    else:
        lines.append("Sin alertas activas.")
    return "\n".join(lines)
