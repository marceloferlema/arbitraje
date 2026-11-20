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
    def __init__(self, cfg, telegram=False, kinds=("PRE", "CONFIRMADA"), intrabar_pre=True,
                 executor=None, chat_id=None, portfolio_open_fn=None):
        self.cfg = cfg
        self.kinds = tuple(kinds)
        self.intrabar_pre = intrabar_pre
        self.executor = executor
        self.portfolio_open_fn = portfolio_open_fn
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
    def _current_equity(self) -> float:
        """Interés compuesto real: consulta el saldo vivo del exchange si hay ejecutor activo."""
        if self.executor is not None and not getattr(self.executor, "dry_run", True):
            try:
                bal = self.executor.get_balance()
                eq = float(bal.get("balance", 0.0))
                if eq > 10.0:
                    return eq
            except Exception as e:
                print(f"[live] Aviso consultando saldo real para sizing ({e}); usando capital_usd={self.cfg.capital_usd}")
        return float(self.cfg.capital_usd)

    def _can_open_position(self, exclude_tr=None) -> bool:
        n_open = sum(1 for x in self.trades if x is not exclude_tr and not x.is_closed and x.status == "ABIERTA")
        if n_open >= self.cfg.max_open_positions:
            return False
        max_port = getattr(self.cfg, "max_portfolio_positions", None)
        if max_port and self.portfolio_open_fn is not None:
            if self.portfolio_open_fn() >= max_port:
                return False
        return True

    def handle_signals(self, sigs):
        for s in sigs:
            funding = self.ds.funding_now(self.cfg.symbol)
            self.notifier.send(format_signal(s, funding))
            self.notifier.log_signal(s)
            tr = trade_from_signal(s, self.cfg)
            can_fill = self._can_open_position()
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
            can_fill = self._can_open_position(exclude_tr=tr)
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
        if self.h1 is None or len(self.h1) == 0:
            return
        last_bar_ts = self.h1.index[-1]
        if getattr(self, "_last_eval_bar", None) == last_bar_ts:
            return
        self._last_eval_bar = last_bar_ts
        eng = self.engine()
        t = len(eng.times) - 1
        equity = self._current_equity()
        sigs = eng.evaluate(t, self.ctx, self.state, self.kinds, equity=equity)
        self.handle_signals(sigs)
        self.save_state()
        if self.notifier.storage is not None:
            try:
                self.notifier.storage.backfill_near_miss_outcomes(self.cfg.symbol, self.h1)
                if not sigs:
                    self.notifier.storage.log_near_miss(last_bar_ts + H1, self.cfg.symbol, self.ctx, eng.describe(t, self.ctx))
            except Exception as e:
                print(f"[storage] aviso registrando telemetría {self.cfg.symbol}: {e}")
        print(status_text(eng, t, self.ctx, self.trades, self.cfg, fired=bool(sigs)), flush=True)

    def on_minute_close(self, k):
        ts = pd.Timestamp(int(k["t"]), unit="ms", tz="UTC")
        o, h, l, c = float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"])
        self.track(ts, o, h, l, c)
        if self.intrabar_pre and "PRE" in self.kinds and self.partial_1h is not None and self.ctx is not None:
            eng = self.engine(self.partial_1h)
            t = len(eng.times) - 1
            now = ts + M1
            sigs = eng.evaluate(t, self.ctx, self.state, ("PRE",), equity=self._current_equity(),
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
        streams = [f"{sym}@kline_1m", f"{sym}@kline_1h"]
        urls = [BinanceFutures.ws_url(streams), BinanceFutures.ws_fallback_url(streams)]
        url_idx = 0
        backoff = 2
        while not self.stop.is_set():
            url = urls[url_idx % len(urls)]
            ws = websocket.WebSocketApp(
                url, on_message=self._on_message,
                on_open=lambda w, u=url: print(f"[ws] conectado {u}"),
                on_error=lambda w, e: print(f"[ws] error: {e}"),
                on_close=lambda w, c, m: print(f"[ws] desconectado ({c})"))
            t0 = time.time()
            ws.run_forever(ping_interval=30, ping_timeout=10)
            if self.stop.is_set():
                break
            elapsed = time.time() - t0
            if elapsed > 60:
                backoff = 2
            else:
                # Si falló rápido (ej. bloqueo 451 en fstream), alternar al endpoint fallback (data-stream.binance.vision)
                url_idx += 1
            print(f"[ws] reconectando en {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            try:  # backfill por si nos perdimos el cierre de una vela 1H
                self.q.put(("1h", None))
            except Exception:
                pass

    def check_hourly_failsafe(self):
        """Failsafe por REST: si cambió la hora UTC y el WS no disparó cierre 1H, lo fuerza."""
        if self.h1 is None or len(self.h1) == 0:
            return
        now = pd.Timestamp.now(tz="UTC")
        expected_last_closed = now.floor("1h") - H1
        last_bar = self.h1.index.max()
        if last_bar < expected_last_closed and now >= now.floor("1h") + pd.Timedelta(seconds=20):
            if getattr(self, "_last_failsafe_hour", None) != expected_last_closed:
                self._last_failsafe_hour = expected_last_closed
                print(f"[live] ⏱️ Failsafe REST disparado para {self.cfg.symbol} (cierre {expected_last_closed:%H:%M} UTC)")
                self.q.put(("1h", None))

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
                self.check_hourly_failsafe()
                if time.time() - self.last_msg > 180:
                    print("[live] ⚠️ sin mensajes del WebSocket hace más de 3 minutos")
        except KeyboardInterrupt:
            print("\n[live] deteniendo...")
            self.stop.set()
            self.save_state()


def status_text(eng, t, ctx, trades, cfg, fired=False):
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
    if getattr(cfg, "capitulation_enabled", False):
        ed = f"{d['ema_dist_pct']:+.1f}%" if d.get("ema_dist_pct") is not None else "-"
        rs = f"{d['rsi_min4']:.1f}" if d.get("rsi_min4") is not None else "-"
        tb = f"{d['tb_ratio']*100:.0f}%" if d.get("tb_ratio") is not None else "-"
        col = "VERDE" if d.get("is_green") else "ROJA"
        lines.append(f"Sniper Capitulación: EMA200 {ed} (req <=-10%) · RSI_min4 {rs} (req <=25) · "
                     f"TakerBuy {tb} (req >=53%) · Vela {col} (cierre {d.get('wick_bull', 0)*100:.0f}%)")
    if not fired and d.get("skip_reason"):
        lines.append(f"Filtro (Vol alto sin entrada): {d['skip_reason']}")
    act = [tr for tr in trades if not tr.is_closed]
    if act:
        for tr in act:
            lines.append(f"  · {tr.signal_id} {tr.status} E {_p(tr.entry)} SL {_p(tr.sl)} TP {_p(tr.tp)}")
    else:
        lines.append("Sin alertas activas.")
    return "\n".join(lines)
