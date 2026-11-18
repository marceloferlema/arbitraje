"""
Simulación / seguimiento de operaciones (compartido por backtest y live).

Estados: PENDIENTE -> ABIERTA -> (TP | SL | TIEMPO)   ó   PENDIENTE -> (CANCELADA | EXPIRADA | BLOQUEADA)
"""
from dataclasses import dataclass, field, asdict

import pandas as pd


@dataclass
class Trade:
    signal_id: str
    kind: str
    direction: str
    entry: float
    sl: float
    tp: float
    qty: float
    notional: float
    risk_usd: float
    created: pd.Timestamp
    valid_until: pd.Timestamp = None
    max_hold: pd.Timedelta = pd.Timedelta(days=7)
    zone_key: int = None
    market_entry: bool = False
    status: str = "PENDIENTE"
    fill_time: pd.Timestamp = None
    fill_price: float = None
    exit_time: pd.Timestamp = None
    exit_price: float = None
    fees: float = 0.0
    funding: float = 0.0
    pnl: float = 0.0
    r_multiple: float = 0.0
    events: list = field(default_factory=list)
    resolved_intrabar: bool = False
    be_moved: bool = False
    initial_sl: float = None
    initial_qty: float = None
    partial_done: bool = False
    realized_partial_pnl: float = 0.0
    exchange_orders: dict = field(default_factory=dict)

    @property
    def sign(self):
        return 1 if self.direction == "LONG" else -1

    @property
    def is_closed(self):
        return self.status in ("TP", "SL", "TIEMPO", "CANCELADA", "EXPIRADA", "BLOQUEADA")

    def to_dict(self):
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, pd.Timestamp):
                d[k] = v.isoformat()
            elif isinstance(v, pd.Timedelta):
                d[k] = v.total_seconds() / 86400
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        for k in ("created", "valid_until", "fill_time", "exit_time"):
            if d.get(k):
                d[k] = pd.Timestamp(d[k])
        d["max_hold"] = pd.Timedelta(days=d.get("max_hold", 7))
        return cls(**d)


class TradeSimulator:
    def __init__(self, cfg):
        self.cfg = cfg

    # ---------------------------------------------------------------- core
    def _fill(self, tr, price, ts, maker):
        tr.status = "ABIERTA"
        tr.fill_time = ts
        tr.fill_price = price
        if tr.initial_sl is None:
            tr.initial_sl = tr.sl
        if tr.initial_qty is None:
            tr.initial_qty = tr.qty
        fee = self.cfg.fee_maker if maker else self.cfg.fee_taker
        tr.fees += tr.qty * price * fee
        tr.events.append(("FILL", ts, price))

    def _close(self, tr, price, ts, reason, maker):
        tr.status = reason
        tr.exit_time = ts
        tr.exit_price = price
        fee = self.cfg.fee_maker if maker else self.cfg.fee_taker
        tr.fees += tr.qty * price * fee
        gross = tr.sign * tr.qty * (price - tr.fill_price) + tr.realized_partial_pnl
        tr.pnl = gross - tr.fees - tr.funding
        tr.r_multiple = tr.pnl / tr.risk_usd if tr.risk_usd > 0 else 0.0
        tr.events.append((reason, ts, price))

    def ambiguous(self, tr, o, h, l, market_open=False):
        """True si el orden de los eventos dentro de la vela cambia el resultado."""
        long = tr.direction == "LONG"
        hit_sl = (l <= tr.sl) if long else (h >= tr.sl)
        hit_tp = (h >= tr.tp) if long else (l <= tr.tp)
        if tr.status == "ABIERTA" or (tr.status == "PENDIENTE" and market_open):
            return hit_sl and hit_tp
        if tr.status == "PENDIENTE":
            touched = (l <= tr.entry) if long else (h >= tr.entry)
            return touched and hit_tp
        return False

    def step(self, tr, ts, o, h, l, c, bar_end, market_open=False, can_fill=True):
        """Procesa una vela (criterio conservador ante ambigüedad). Devuelve lista de eventos nuevos."""
        n0 = len(tr.events)
        slip = self.cfg.slippage_pct
        long = tr.direction == "LONG"
        if tr.status == "PENDIENTE":
            if tr.valid_until is not None and ts >= tr.valid_until:
                tr.status = "EXPIRADA"
                tr.events.append(("EXPIRADA", ts, o))
                return tr.events[n0:]
            if market_open:
                if not can_fill:
                    tr.status = "BLOQUEADA"
                    tr.events.append(("BLOQUEADA", ts, o))
                    return tr.events[n0:]
                self._fill(tr, o * (1 + slip * tr.sign), ts, maker=False)
            else:
                touched = (l <= tr.entry) if long else (h >= tr.entry)
                if touched:
                    if not can_fill:
                        tr.status = "BLOQUEADA"
                        tr.events.append(("BLOQUEADA", ts, tr.entry))
                        return tr.events[n0:]
                    gap = (o <= tr.entry) if long else (o >= tr.entry)
                    self._fill(tr, o if gap else tr.entry, ts, maker=not gap)
                else:
                    if (h >= tr.tp) if long else (l <= tr.tp):
                        tr.status = "CANCELADA"
                        tr.events.append(("CANCELADA", ts, tr.tp))
                    return tr.events[n0:]
            filled_at_open = market_open or (o <= tr.entry if long else o >= tr.entry)
            # misma vela del fill: SL primero (conservador); TP sólo si se llenó en la apertura
            if (l <= tr.sl) if long else (h >= tr.sl):
                self._close(tr, tr.sl * (1 - slip * tr.sign), ts, "SL", maker=False)
            elif filled_at_open and ((h >= tr.tp) if long else (l <= tr.tp)):
                self._close(tr, tr.tp, ts, "TP", maker=True)
            return tr.events[n0:]

        if tr.status == "ABIERTA":
            hit_sl = (l <= tr.sl) if long else (h >= tr.sl)
            hit_tp = (h >= tr.tp) if long else (l <= tr.tp)
            if hit_sl:
                gap = (o <= tr.sl) if long else (o >= tr.sl)
                px = (o if gap else tr.sl) * (1 - slip * tr.sign)
                self._close(tr, px, ts, "SL", maker=False)
            elif hit_tp:
                gap = (o >= tr.tp) if long else (o <= tr.tp)
                self._close(tr, o if gap else tr.tp, ts, "TP", maker=True)
            elif bar_end - tr.fill_time >= tr.max_hold:
                self._close(tr, c * (1 - slip * tr.sign), bar_end, "TIEMPO", maker=False)
            else:
                base_sl = tr.initial_sl if tr.initial_sl is not None else tr.sl
                init_risk = abs(tr.fill_price - base_sl)
                if init_risk > 0:
                    # 1. TP parcial (TP1)
                    ptp_r = getattr(self.cfg, "partial_tp_r", 0.0) or 0.0
                    ptp_frac = getattr(self.cfg, "partial_tp_frac", 0.5) or 0.0
                    if ptp_r > 0 and ptp_frac > 0 and not tr.partial_done:
                        tp1_px = tr.fill_price + tr.sign * ptp_r * init_risk
                        if (long and h >= tp1_px) or ((not long) and l <= tp1_px):
                            close_qty = tr.qty * ptp_frac
                            tr.realized_partial_pnl += tr.sign * close_qty * (tp1_px - tr.fill_price)
                            tr.fees += close_qty * tp1_px * self.cfg.fee_maker
                            tr.qty -= close_qty
                            tr.partial_done = True
                            tr.events.append(("TP1", ts, tp1_px))

                    # 2. Mover SL a Breakeven
                    be_r = getattr(self.cfg, "breakeven_r", 0.0) or 0.0
                    if be_r > 0 and not tr.be_moved:
                        off_r = getattr(self.cfg, "breakeven_offset_r", 0.10) or 0.0
                        if long and h >= tr.fill_price + be_r * init_risk:
                            new_sl = tr.fill_price + off_r * init_risk
                            if new_sl > tr.sl:
                                tr.sl = new_sl
                                tr.be_moved = True
                                tr.events.append(("BE", ts, new_sl))
                        elif (not long) and l <= tr.fill_price - be_r * init_risk:
                            new_sl = tr.fill_price - off_r * init_risk
                            if new_sl < tr.sl:
                                tr.sl = new_sl
                                tr.be_moved = True
                                tr.events.append(("BE", ts, new_sl))

                    # 3. Trailing Stop en R después de trail_start_r
                    tr_start = getattr(self.cfg, "trail_start_r", 0.0) or 0.0
                    tr_dist = getattr(self.cfg, "trail_distance_r", 1.25) or 1.25
                    if tr_start > 0:
                        if long and h >= tr.fill_price + tr_start * init_risk:
                            cand_sl = h - tr_dist * init_risk
                            if cand_sl > tr.sl:
                                tr.sl = cand_sl
                        elif (not long) and l <= tr.fill_price - tr_start * init_risk:
                            cand_sl = l + tr_dist * init_risk
                            if cand_sl < tr.sl:
                                tr.sl = cand_sl
        return tr.events[n0:]

    def apply_funding(self, tr, rate, mark_price):
        if tr.status == "ABIERTA":
            tr.funding += tr.sign * rate * tr.qty * mark_price
