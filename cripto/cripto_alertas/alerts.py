"""Formateo de alertas y notificadores (consola, Telegram, CSV)."""
import csv
import os
from datetime import timedelta, timezone

import pandas as pd
import requests

TZ_AR = timezone(timedelta(hours=-3), name="ART")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fmt_ts(ts):
    if ts is None:
        return "-"
    return pd.Timestamp(ts).tz_convert(TZ_AR).strftime("%d/%m %H:%M ART")


def _p(x):
    if x is None:
        return "-"
    return f"{x:,.4f}" if x < 10 else f"{x:,.2f}"


def tv_link(symbol):
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}.P"


def format_signal(s, funding=None):
    emoji = "🟢" if s.direction == "LONG" else "🔴"
    if s.kind == "PRE":
        head = f"🟡 PRE-ALERTA {emoji} {s.direction} {s.symbol} (Perp) – dejar orden LIMIT"
    else:
        head = f"{emoji} {s.direction} {s.symbol} (Perp) – SEÑAL CONFIRMADA"
    sz = s.sizing
    z, tz = s.zone, s.target_zone
    sgn_sl = "-" if s.direction == "LONG" else "+"
    sgn_tp = "+" if s.direction == "LONG" else "-"
    lines = [
        f"{head}  |  Score {s.score:.0f}/100",
        f"Emitida: {fmt_ts(s.time)}   Precio actual: {_p(s.price_now)}",
        f"Entrada ({s.entry_type.lower()}): {_p(s.entry)}   SL: {_p(s.sl)} ({sgn_sl}{s.sl_pct*100:.2f}%)   "
        f"TP: {_p(s.tp)} ({sgn_tp}{s.tp_pct*100:.2f}%)   R:R {s.rr:.2f}",
    ]
    if s.tp1_price is not None:
        lines.append(f"Gestión activa: TP1 parcial (50% en +1.5R) @ {_p(s.tp1_price)} + SL a Breakeven · Trailing desde +2.5R")
    if s.retest_entry is not None and abs(s.retest_entry - s.entry) / max(s.entry, 1e-9) >= 0.002:
        lines.append(f"Alternativa Limit (retesteo zona): {_p(s.retest_entry)} → R:R {s.retest_rr:.2f} "
                     f"(vs R:R {s.rr:.2f} a mercado ahora)")
    lines.extend([
        f"Tamaño sugerido: margen {sz['margin_usd']:.2f} USD ({sz['margin_pct']*100:.1f}% de {sz['equity']:.0f}) · "
        f"palanca {sz['leverage']}x · nocional {sz['notional_usd']:.2f} USD · qty {sz['qty']:.4f}",
        f"Riesgo si salta SL: {sz['risk_usd']:.2f} USD ({sz['risk_pct']*100:.2f}% del capital) · "
        f"Ganancia en TP: {sz['reward_usd']:.2f} USD",
        f"Zona HVN: {_p(z['low'])} – {_p(z['high'])} (vol {z['share']*100:.1f}% del perfil, "
        f"{z['support_rejections']} rebotes como soporte / {z['resistance_rejections']} como resistencia / "
        f"{z['crosses']} cruces){' · contiene POC' if z.get('is_poc') else ''}",
        f"Objetivo (zona de liquidez): {_p(tz['low'])} – {_p(tz['high'])}",
    ])
    lvl_name = "último mínimo" if s.direction == "LONG" else "último máximo"
    if s.kind == "CONFIRMADA":
        lines.append(f"Barrido: {lvl_name} {_p(s.level)} perforado, mecha {_p(s.sweep_extreme)}, "
                     f"reclaim al cierre 1H {_p(s.price_now)}")
    else:
        lines.append(f"Liquidez a barrer: {lvl_name} {_p(s.level)} (formado {fmt_ts(s.level_time)})")
    vol = f"{s.vol_ratio:.1f}x mediana 30d (percentil {s.vol_pct*100:.0f})" if s.vol_ratio and s.vol_pct is not None else "-"
    mom = {"UP": "girando ↑", "DOWN": "girando ↓", None: "-"}[s.mom_dir]
    lines.append(f"Volumen 1H: {vol}  ·  Squeeze: {'ON (comprimido)' if s.sqz_on else 'OFF'}, momentum {mom}")
    lines.append(f"Estructura 1D: {s.structure} → {s.bias_alignment}")
    btc_icon = {True: "✅", False: "⚠️", None: "·"}[s.btc_ok]
    fund = f"   Funding: {funding*100:+.4f}%" if funding is not None else ""
    lines.append(f"BTC: {s.btc_note} {btc_icon}{fund}")
    if s.kind == "PRE":
        lines.append(f"Validez orden: hasta {fmt_ts(s.valid_until)} · Time-stop: {s.max_hold_until_days} días desde el fill")
    else:
        lines.append(f"Time-stop: {s.max_hold_until_days} días")
    lines.append(f"TradingView: {tv_link(s.symbol)}")
    lines.append(f"ID: {s.id}")
    return "\n".join(lines)


EVENT_TEXT = {
    "FILL": "✅ Orden ejecutada",
    "TP1": "💰 TP1 parcial (+1.5R) alcanzado (cierra 50%)",
    "BE": "🛡️ Stop Loss movido a Breakeven (+1.5R alcanzado)",
    "TP": "🎯 TAKE PROFIT final alcanzado",
    "SL": "🛑 STOP LOSS tocado",
    "TIEMPO": "⏱️ Time-stop: cerrar posición",
    "CANCELADA": "❎ Setup cancelado (el precio llegó al objetivo sin ejecutar la entrada)",
    "EXPIRADA": "⌛ Orden limit expirada sin ejecutarse",
    "BLOQUEADA": "⛔ Señal no tomada (máximo de posiciones abiertas)",
}


def format_event(tr, event):
    name, ts, price = event
    txt = EVENT_TEXT.get(name, name)
    lines = [f"{txt} – {tr.direction} {tr.signal_id}", f"{fmt_ts(ts)} · precio {_p(price)}"]
    if name == "FILL":
        lines.append(f"SL {_p(tr.sl)} · TP {_p(tr.tp)}")
    if name in ("TP", "SL", "TIEMPO"):
        lines.append(f"Resultado: {tr.pnl:+.2f} USD ({tr.r_multiple:+.2f}R)")
    return "\n".join(lines)


def load_env():
    env = {}
    path = os.path.join(ROOT_DIR, ".env")
    try:
        from dotenv import dotenv_values
        env = {k: v for k, v in dotenv_values(path).items() if v is not None}
    except Exception:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, v = line.strip().split("=", 1)
                        env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TELEGRAM_TOKEN", "CHAT_ID", "CRIPTO_TELEGRAM_TOKEN", "CRIPTO_CHAT_ID",
              "BINGX_API_KEY", "BINGX_SECRET", "BINGX_SECRET_KEY"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    return env


class Notifier:
    def __init__(self, telegram=False, log_dir=None, console=True, chat_id=None, token=None):
        self.console = console
        self.telegram = telegram
        self.log_dir = log_dir
        env = load_env()
        self.token = token or env.get("CRIPTO_TELEGRAM_TOKEN") or env.get("TELEGRAM_TOKEN")
        self.chat_id = chat_id or env.get("CRIPTO_CHAT_ID") or env.get("CHAT_ID")
        if telegram and not (self.token and self.chat_id):
            print("⚠️ TELEGRAM_TOKEN / CHAT_ID no configurados en .env: Telegram desactivado.")
            self.telegram = False
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

    def send(self, text):
        if self.console:
            print("\n" + "=" * 90 + "\n" + text + "\n" + "=" * 90, flush=True)
        if self.telegram:
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                  json={"chat_id": self.chat_id, "text": text[:4000],
                                        "disable_web_page_preview": True}, timeout=10)
                if r.status_code != 200:
                    print(f"⚠️ Telegram respondió {r.status_code}: {r.text[:200]}")
            except Exception as e:
                print(f"⚠️ Error enviando Telegram: {e}")

    def log_signal(self, s):
        if not self.log_dir:
            return
        path = os.path.join(self.log_dir, "alertas.csv")
        d = s.to_dict()
        row = {k: d[k] for k in ("id", "kind", "direction", "symbol", "time", "entry_type", "entry", "sl", "tp",
                                 "rr", "score", "level", "sweep_extreme", "price_now", "vol_pct", "btc_ok",
                                 "structure", "bias_alignment", "valid_until")}
        row.update(zone_low=s.zone["low"], zone_high=s.zone["high"], target_low=s.target_zone["low"],
                   margin_usd=s.sizing["margin_usd"], leverage=s.sizing["leverage"],
                   notional_usd=s.sizing["notional_usd"], risk_usd=s.sizing["risk_usd"])
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)

    def log_event(self, tr, event):
        if not self.log_dir:
            return
        path = os.path.join(self.log_dir, "eventos.csv")
        name, ts, price = event
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "signal_id", "event", "price", "pnl", "r"])
            w.writerow([pd.Timestamp(ts).isoformat(), tr.signal_id, name, price, round(tr.pnl, 4),
                        round(tr.r_multiple, 3)])
