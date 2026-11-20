"""
Motor de señales (idéntico para live y backtest).

Engine.evaluate(t, ctx, state) mira SOLO datos hasta la vela 1H `t` (inclusive) y devuelve señales:
  - PRE          : pre-alerta / orden limit en el piso (long) o techo (short) de una franja HVN
                   hacia la cual el precio se acerca tras (o por) barrer el último mínimo/máximo.
  - CONFIRMADA   : barrido del último mínimo/máximo dentro de una franja HVN + reclaim al cierre 1H
                   + volumen alto.
"""
import json
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from .indicators import atr, pivots, squeeze_momentum, volume_stats
from .risk import position_size
from .volume_profile import zones_above, zones_below

H1 = pd.Timedelta(hours=1)


@dataclass
class Signal:
    id: str
    kind: str                 # PRE / CONFIRMADA
    direction: str            # LONG / SHORT
    symbol: str
    time: pd.Timestamp        # momento de emisión (cierre de la vela 1H, o ahora si es intravela)
    bar_idx: int
    entry: float
    sl: float
    tp: float
    rr: float
    score: float
    entry_type: str           # LIMIT / MERCADO
    zone: dict
    target_zone: dict
    level: float              # último mínimo/máximo relevante
    level_time: str
    sweep_extreme: float = None
    price_now: float = None
    vol_pct: float = None
    vol_ratio: float = None
    mom_dir: str = None
    sqz_on: bool = None
    btc_ok: bool = None
    btc_note: str = ""
    structure: str = ""
    bias_alignment: str = ""
    valid_until: pd.Timestamp = None
    max_hold_until_days: int = 7
    retest_entry: float = None
    retest_rr: float = None
    tp1_price: float = None
    delta_1h: float = None
    sizing: dict = field(default_factory=dict)

    @property
    def sl_pct(self):
        return abs(self.entry - self.sl) / self.entry

    @property
    def tp_pct(self):
        return abs(self.tp - self.entry) / self.entry

    def to_dict(self):
        d = asdict(self)
        for k in ("time", "valid_until"):
            if d[k] is not None:
                d[k] = pd.Timestamp(d[k]).isoformat()
        d["sl_pct"], d["tp_pct"] = self.sl_pct, self.tp_pct
        return d


class EngineState:
    """Estado de deduplicación (persistible en JSON para el modo live)."""

    def __init__(self):
        self.fired_levels = set()      # "LONG|<iso pivot time>"
        self.pre_active = {}           # direction -> {"key": int, "until": iso}
        self.zone_cooldown = {}        # "LONG|<zone key>" -> iso hasta

    def pre_is_active(self, direction, now):
        a = self.pre_active.get(direction)
        return bool(a) and pd.Timestamp(a["until"]) > now

    def release_pre(self, direction, zone_key, now, cooldown_h):
        self.pre_active.pop(direction, None)
        self.zone_cooldown[f"{direction}|{zone_key}"] = (now + pd.Timedelta(hours=cooldown_h)).isoformat()

    def zone_in_cooldown(self, direction, zone_key, now):
        u = self.zone_cooldown.get(f"{direction}|{zone_key}")
        return bool(u) and pd.Timestamp(u) > now

    def to_json(self):
        return {"fired_levels": sorted(self.fired_levels), "pre_active": self.pre_active,
                "zone_cooldown": self.zone_cooldown}

    @classmethod
    def from_json(cls, d):
        s = cls()
        s.fired_levels = set(d.get("fired_levels", []))
        s.pre_active = d.get("pre_active", {})
        s.zone_cooldown = d.get("zone_cooldown", {})
        return s


class Engine:
    def __init__(self, cfg, h1, btc_h1=None):
        self.cfg = cfg
        self.h1 = h1
        self.times = h1.index
        self.o = h1["open"].to_numpy()
        self.h = h1["high"].to_numpy()
        self.l = h1["low"].to_numpy()
        self.c = h1["close"].to_numpy()
        self.atr_h = atr(h1, 14).to_numpy()
        vs = volume_stats(h1, cfg.vol_window_bars)
        self.vol_pct = vs["vol_pct"].to_numpy()
        self.vol_ratio = vs["vol_ratio"].to_numpy()
        sq = squeeze_momentum(h1)
        self.mom = sq["mom"].to_numpy()
        self.sqz = sq["sqz_on"].to_numpy()
        if "taker_buy_base" in h1.columns:
            vol_nz = h1["volume"].replace(0, np.nan)
            self.tb_ratio = (h1["taker_buy_base"] / vol_nz).to_numpy()
        else:
            self.tb_ratio = np.full(len(h1), 0.5)
        self.ema200 = h1["close"].ewm(span=200, adjust=False).mean().to_numpy()
        _delta = h1["close"].diff()
        _gain = _delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        _loss = (-_delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        self.rsi14 = (100 - (100 / (1 + _gain / _loss.replace(0, np.nan)))).to_numpy()
        ph, pl = pivots(h1, cfg.pivot_len_1h, cfg.pivot_len_1h)
        self.piv_hi = ph.to_numpy()
        self.piv_lo = pl.to_numpy()
        self.search_bars = cfg.pivot_search_days * 24
        self.btc = None
        if btc_h1 is not None and cfg.symbol != cfg.btc_symbol and cfg.btc_filter != "off":
            b = btc_h1.reindex(h1.index)
            self.btc = (b["low"].to_numpy(), b["high"].to_numpy(), b["close"].to_numpy())

    # ------------------------------------------------------------ niveles
    def candidate_levels(self, t, direction, confirmed_until=None):
        """Todos los mín (LONG) / máx (SHORT) relevantes intactos hasta t-sweep_bars (del más reciente al más antiguo)."""
        cfg = self.cfg
        piv = self.piv_lo if direction == "LONG" else self.piv_hi
        cu = t if confirmed_until is None else confirmed_until
        end = t - cfg.sweep_bars + 1
        out = []
        for k in range(len(piv) - 1, -1, -1):
            i, conf, p = int(piv[k][0]), int(piv[k][1]), float(piv[k][2])
            if conf > cu:
                continue
            if i < t - self.search_bars:
                break
            if end <= i + 1:
                continue
            if direction == "LONG":
                if self.l[i + 1:end].min() <= p:
                    continue
                if self.h[i + 1:end].max() < p * (1 + cfg.impulse_min_pct):
                    continue
            else:
                if self.h[i + 1:end].max() >= p:
                    continue
                if self.l[i + 1:end].min() > p * (1 - cfg.impulse_min_pct):
                    continue
            out.append((i, p))
        return out

    def last_level(self, t, direction, confirmed_until=None):
        """Último mín (LONG) / máx (SHORT) relevante: originó un impulso y sigue intacto hasta t-sweep_bars."""
        cands = self.candidate_levels(t, direction, confirmed_until=confirmed_until)
        return cands[0] if cands else None

    # ------------------------------------------------------------ filtros
    def btc_check(self, t, direction):
        if self.btc is None:
            return None, "sin filtro BTC"
        bl, bh, bc = self.btc
        r, ref = self.cfg.btc_recent_bars, self.cfg.btc_ref_bars
        if t - r - ref < 0 or np.isnan(bc[t]):
            return None, "BTC sin datos"
        if direction == "LONG":
            recent, base = np.nanmin(bl[t - r + 1:t + 1]), np.nanmin(bl[t - r - ref + 1:t - r + 1])
            bad = recent < base and bc[t] < base
            note = (f"BTC haciendo nuevos mínimos 7d ({recent:,.0f} < {base:,.0f})" if bad
                    else f"BTC sin nuevos mínimos {r}h (mín 7d {base:,.0f}, precio {bc[t]:,.0f})")
        else:
            recent, base = np.nanmax(bh[t - r + 1:t + 1]), np.nanmax(bh[t - r - ref + 1:t - r + 1])
            bad = recent > base and bc[t] > base
            note = (f"BTC haciendo nuevos máximos 7d ({recent:,.0f} > {base:,.0f})" if bad
                    else f"BTC sin nuevos máximos {r}h (máx 7d {base:,.0f}, precio {bc[t]:,.0f})")
        return (not bad), note

    def mom_direction(self, t):
        if t < 1 or np.isnan(self.mom[t]) or np.isnan(self.mom[t - 1]):
            return None
        return "UP" if self.mom[t] > self.mom[t - 1] else "DOWN"

    def sqz_turning(self, t, direction):
        """Giro de Squeeze Momentum desde zona extrema (mom < 0 girando ↑ en LONG, mom > 0 girando ↓ en SHORT)."""
        w0 = max(1, t - self.cfg.sweep_bars + 1)
        m_cur = self.mom[w0:t + 1]
        m_prv = self.mom[w0 - 1:t]
        if len(m_cur) == 0 or len(m_cur) != len(m_prv) or np.all(np.isnan(m_cur)):
            return False
        if direction == "LONG":
            return bool(np.any((m_cur < 0) & (m_cur > m_prv)))
        return bool(np.any((m_cur > 0) & (m_cur < m_prv)))

    def _score(self, zone, rr, vol_pct, alignment, btc_ok, mom_ok):
        s = 30 * zone.wall_score
        s += 15 * min(1.0, zone.share / 0.08)
        s += 20 * min(1.0, rr / 4.0)
        s += 15 * (vol_pct if vol_pct is not None and not np.isnan(vol_pct) else 0.5)
        s += {"A FAVOR": 10, "NEUTRAL": 5, "EN CONTRA": 0}[alignment]
        s += 5 if btc_ok in (True, None) else 0
        s += 5 if mom_ok else 0
        return round(float(s), 1)

    def _alignment(self, ctx, direction):
        pref = ctx.structure.preferred_direction()
        if pref is None:
            return "NEUTRAL"
        return "A FAVOR" if pref == direction else "EN CONTRA"

    # ------------------------------------------------------------ evaluación
    def evaluate(self, t, ctx, state, kinds=("PRE", "CONFIRMADA"), equity=None, partial=False, now=None):
        if ctx is None or t < 50:
            return []
        out = []
        now = now if now is not None else self.times[t] + H1
        equity = equity if equity is not None else self.cfg.capital_usd
        for direction in ("LONG", "SHORT"):
            if "CONFIRMADA" in kinds and not partial:
                s = self._confirmed(t, ctx, state, direction, equity, now)
                if s:
                    out.append(s)
            if "PRE" in kinds:
                s = self._pre(t, ctx, state, direction, equity, now, partial)
                if s:
                    out.append(s)
        if ("CONFIRMADA" in kinds and not partial
                and getattr(self.cfg, "capitulation_enabled", False)
                and not any(s.direction == "LONG" for s in out)):
            s_cap = self._capitulation_long(t, ctx, state, equity, now)
            if s_cap:
                out.append(s_cap)
        return out

    def _capitulation_long(self, t, ctx, state, equity, now):
        """
        Sniper de Capitulación Extrema (LONG táctico 2:1 en pisos de pánico real):
        Detecta velas de clímax de volumen + absorción agresiva cuando el precio está
        fuertemente sobreextendido debajo de la EMA200 y en sobreventa extrema (RSI <= 25).
        """
        cfg = self.cfg
        if t < 200:
            return None
        cap_key = -999
        if state.zone_in_cooldown("LONG", cap_key, now):
            return None
        o, h, l, c = float(self.o[t]), float(self.h[t]), float(self.l[t]), float(self.c[t])
        rng = h - l
        if rng <= 0 or c <= o:
            return None
        ema = float(self.ema200[t])
        if np.isnan(ema) or ema <= 0 or (c / ema - 1.0) > cfg.capitulation_ema_dist_pct:
            return None
        rsi_win = self.rsi14[max(0, t - 3):t + 1]
        if len(rsi_win) == 0 or np.all(np.isnan(rsi_win)) or float(np.nanmin(rsi_win)) > cfg.capitulation_rsi_max:
            return None
        vp = float(self.vol_pct[t])
        if np.isnan(vp) or vp < cfg.capitulation_vol_pct:
            return None
        wick_bull = (c - l) / rng
        if wick_bull < cfg.capitulation_wick_min:
            return None
        tb = float(self.tb_ratio[t])
        if np.isnan(tb) or tb < cfg.capitulation_tb_min:
            return None

        atr_h = float(self.atr_h[t])
        if np.isnan(atr_h) or atr_h <= 0:
            return None
        low3 = float(np.min(self.l[max(0, t - 2):t + 1]))
        entry = c
        sl = low3 - 0.5 * atr_h
        risk = entry - sl
        if risk <= 0 or (risk / entry) > 0.12:
            return None
        rr = float(getattr(cfg, "capitulation_rr", 2.0))
        tp = entry + rr * risk

        state.zone_cooldown[f"LONG|{cap_key}"] = (pd.Timestamp(now) + pd.Timedelta(hours=cfg.capitulation_cooldown_hours)).isoformat()
        sid = f"{cfg.symbol}-CAP-L-{pd.Timestamp(now):%Y%m%d%H%M}"
        md = self.mom_direction(t)
        btc_ok, btc_note = self.btc_check(t, "LONG")
        zone_dict = {
            "key": cap_key, "low": sl, "high": entry, "peak": low3, "share": 0.0, "prominence": 0.0,
            "support_rejections": 0, "resistance_rejections": 0, "crosses": 0, "is_poc": False,
            "mid": (sl + entry) / 2, "width_pct": (entry / sl - 1) if sl > 0 else 0.0, "wall_score": 1.0,
        }
        tz_dict = {
            "key": -998, "low": tp, "high": tp * 1.01, "peak": tp, "share": 0.0, "prominence": 0.0,
            "support_rejections": 0, "resistance_rejections": 0, "crosses": 0, "is_poc": False,
            "mid": tp * 1.005, "width_pct": 0.01, "wall_score": 0.5,
        }
        return Signal(
            id=sid, kind="CONFIRMADA", direction="LONG", symbol=cfg.symbol, time=pd.Timestamp(now), bar_idx=t,
            entry=float(entry), sl=float(sl), tp=float(tp), rr=round(rr, 2), score=88.0,
            entry_type="MERCADO", zone=zone_dict, target_zone=tz_dict, level=float(low3),
            level_time=self.times[t].isoformat(), sweep_extreme=float(low3),
            price_now=float(c), vol_pct=vp,
            vol_ratio=None if np.isnan(self.vol_ratio[t]) else float(self.vol_ratio[t]),
            mom_dir=md, sqz_on=bool(self.sqz[t]), btc_ok=btc_ok, btc_note=btc_note,
            structure=ctx.structure.summary(),
            bias_alignment=f"CAPITULACION EXTREMA (EMA200 {(c/ema-1)*100:.1f}%, RSI {float(np.nanmin(rsi_win)):.1f})",
            valid_until=None, max_hold_until_days=min(3, cfg.max_hold_days),
            retest_entry=None, retest_rr=None, tp1_price=None, delta_1h=round(tb, 3),
            sizing=position_size(entry, sl, tp, equity, cfg),
        )

    def delta_check(self, t, direction):
        """Absorción agresiva (taker_buy_base / volume) en la ventana de barrido."""
        w0 = max(0, t - self.cfg.sweep_bars + 1)
        sub = self.tb_ratio[w0:t + 1]
        if len(sub) == 0 or np.all(np.isnan(sub)):
            return True, 0.5
        if direction == "LONG":
            val = float(np.nanmax(sub))
            return val >= 0.495, val
        val = float(np.nanmin(sub))
        return val <= 0.505, val

    def _target(self, ctx, direction, entry, support_zone):
        cfg = self.cfg
        if direction == "LONG":
            for z in zones_above(ctx.zones, entry):
                if z is support_zone or z.low <= support_zone.high:
                    continue
                if z.low > entry * (1 + cfg.min_tp_pct):
                    return z, z.low * (1 - cfg.tp_buffer_pct)
        else:
            for z in zones_below(ctx.zones, entry):
                if z is support_zone or z.high >= support_zone.low:
                    continue
                if z.high < entry * (1 - cfg.min_tp_pct):
                    return z, z.high * (1 + cfg.tp_buffer_pct)
        return None, None

    def _build(self, kind, direction, t, ctx, zone, tz, entry, sl, tp, level, sweep_extreme, equity, now,
               state, vol_pct, entry_type):
        cfg = self.cfg
        risk = abs(entry - sl)
        if risk <= 0:
            return None
        rr = abs(tp - entry) / risk
        if rr < cfg.rr_min:
            return None
        btc_ok, btc_note = self.btc_check(t, direction)
        if cfg.btc_filter == "strict" and btc_ok is False:
            return None
        alignment = self._alignment(ctx, direction)
        if getattr(cfg, "no_counter_bias", False) and alignment == "EN CONTRA":
            return None
        delta_ok, delta_val = self.delta_check(t, direction)
        if kind == "CONFIRMADA" and getattr(cfg, "delta_confirm", False) and not delta_ok:
            return None
        md = self.mom_direction(t)
        mom_ok = self.sqz_turning(t, direction)
        if cfg.squeeze_confirm and not mom_ok:
            return None
        score = self._score(zone, rr, vol_pct, alignment, btc_ok, mom_ok)
        if score < cfg.min_score:
            return None
        retest_entry, retest_rr, tp1_price = None, None, None
        ptp_r = getattr(cfg, "partial_tp_r", 0.0) or 0.0
        if ptp_r > 0:
            tp1_price = float(entry + (1 if direction == "LONG" else -1) * ptp_r * risk)
        if kind == "CONFIRMADA":
            retest_entry = float(min(entry, zone.high) if direction == "LONG" else max(entry, zone.low))
            r_risk = abs(retest_entry - sl)
            retest_rr = round(float(abs(tp - retest_entry) / r_risk), 2) if r_risk > 0 else round(float(rr), 2)
        sid = f"{cfg.symbol}-{kind[:3]}-{direction[0]}-{pd.Timestamp(now):%Y%m%d%H%M}"
        return Signal(
            id=sid, kind=kind, direction=direction, symbol=cfg.symbol, time=pd.Timestamp(now), bar_idx=t,
            entry=float(entry), sl=float(sl), tp=float(tp), rr=round(float(rr), 2), score=score,
            entry_type=entry_type, zone=zone.to_dict(), target_zone=tz.to_dict(), level=float(level[1]),
            level_time=self.times[level[0]].isoformat(), sweep_extreme=sweep_extreme,
            price_now=float(self.c[t]),
            vol_pct=None if np.isnan(vol_pct) else float(vol_pct),
            vol_ratio=None if np.isnan(self.vol_ratio[t]) else float(self.vol_ratio[t]),
            mom_dir=md, sqz_on=bool(self.sqz[t]), btc_ok=btc_ok, btc_note=btc_note,
            structure=ctx.structure.summary(), bias_alignment=alignment,
            valid_until=(pd.Timestamp(now) + pd.Timedelta(hours=cfg.pre_valid_hours)) if kind == "PRE" else None,
            max_hold_until_days=cfg.max_hold_days,
            retest_entry=retest_entry, retest_rr=retest_rr,
            tp1_price=tp1_price, delta_1h=round(delta_val, 3),
            sizing=position_size(entry, sl, tp, equity, cfg),
        )

    def _confirmed(self, t, ctx, state, direction, equity, now):
        cfg = self.cfg
        cands_lvl = self.candidate_levels(t, direction)
        if not cands_lvl:
            return None
        w0 = t - cfg.sweep_bars + 1
        vol_pct = np.nanmax(self.vol_pct[w0:t + 1]) if not np.all(np.isnan(self.vol_pct[w0:t + 1])) else np.nan
        if np.isnan(vol_pct) or vol_pct < cfg.vol_percentile_min:
            return None
        o, h, l, c = self.o[t], self.h[t], self.l[t], self.c[t]
        rng = h - l
        wick_min = getattr(cfg, "wick_reject_min", 0.0)
        if direction == "LONG":
            if wick_min > 0 and (rng <= 0 or not ((c - l) / rng >= wick_min or c > o)):
                return None
            ext = float(self.l[w0:t + 1].min())
        else:
            if wick_min > 0 and (rng <= 0 or not ((h - c) / rng >= wick_min or c < o)):
                return None
            ext = float(self.h[w0:t + 1].max())

        buf = cfg.sl_buffer_atr_d * ctx.structure.atr_d
        entry = float(c)
        for lvl in cands_lvl[:2]:
            key = f"{direction}|{self.times[lvl[0]].isoformat()}"
            if key in state.fired_levels:
                break
            p = lvl[1]
            if direction == "LONG":
                if not (ext < p and c > p):
                    continue
                cands = [z for z in ctx.zones if ext <= z.high * 1.002 and ext >= z.low - buf]
                if not cands:
                    continue
                zone = max(cands, key=lambda z: z.high)
                if c < zone.low:
                    continue
                sl = min(zone.low, ext) - buf
            else:
                if not (ext > p and c < p):
                    continue
                cands = [z for z in ctx.zones if ext >= z.low * 0.998 and ext <= z.high + buf]
                if not cands:
                    continue
                zone = min(cands, key=lambda z: z.low)
                if c > zone.high:
                    continue
                sl = max(zone.high, ext) + buf
            tz, tp = self._target(ctx, direction, entry, zone)
            if tz is None:
                continue
            sig = self._build("CONFIRMADA", direction, t, ctx, zone, tz, entry, sl, tp, lvl, ext, equity, now,
                              state, vol_pct, "MERCADO")
            if sig:
                state.fired_levels.add(key)
                return sig
        return None

    def _pre(self, t, ctx, state, direction, equity, now, partial):
        cfg = self.cfg
        if state.pre_is_active(direction, now):
            return None
        lvl = self.last_level(t, direction, confirmed_until=t - 1 if partial else t)
        if lvl is None:
            return None
        p = lvl[1]
        c = self.c[t]
        buf = cfg.sl_buffer_atr_d * ctx.structure.atr_d
        chosen = None
        if direction == "LONG":
            for z in zones_below(ctx.zones, c):
                entry = z.low * (1 + cfg.entry_offset_pct)
                if entry >= c * 0.998:
                    continue
                if (c - z.high) / c > cfg.approach_pct:
                    break
                if not (z.low <= p <= z.high * (1 + cfg.level_zone_tol_pct)):
                    continue
                if state.zone_in_cooldown(direction, z.key, now):
                    continue
                chosen = (z, entry, z.low - buf)
                break
        else:
            for z in zones_above(ctx.zones, c):
                entry = z.high * (1 - cfg.entry_offset_pct)
                if entry <= c * 1.002:
                    continue
                if (z.low - c) / c > cfg.approach_pct:
                    break
                if not (z.low * (1 - cfg.level_zone_tol_pct) <= p <= z.high):
                    continue
                if state.zone_in_cooldown(direction, z.key, now):
                    continue
                chosen = (z, entry, z.high + buf)
                break
        if chosen is None:
            return None
        zone, entry, sl = chosen
        tz, tp = self._target(ctx, direction, entry, zone)
        if tz is None:
            return None
        if (direction == "LONG" and tp <= c) or (direction == "SHORT" and tp >= c):
            return None
        sig = self._build("PRE", direction, t, ctx, zone, tz, entry, sl, tp, lvl, None, equity, now, state,
                          self.vol_pct[t], "LIMIT")
        if sig:
            state.pre_active[direction] = {"key": zone.key, "until": sig.valid_until.isoformat()}
        return sig

    # ------------------------------------------------------------ diagnóstico
    def describe(self, t, ctx):
        c = self.c[t]
        lo = self.last_level(t, "LONG")
        hi = self.last_level(t, "SHORT")
        below = zones_below(ctx.zones, c)[:3]
        above = zones_above(ctx.zones, c)[:3]
        return {
            "price": float(c),
            "last_low": None if lo is None else (self.times[lo[0]].isoformat(), lo[1]),
            "last_high": None if hi is None else (self.times[hi[0]].isoformat(), hi[1]),
            "zones_below": [z.to_dict() for z in below],
            "zones_above": [z.to_dict() for z in above],
            "vol_pct": None if np.isnan(self.vol_pct[t]) else float(self.vol_pct[t]),
            "mom_dir": self.mom_direction(t),
            "sqz_on": bool(self.sqz[t]),
        }
