"""
Monitor en vivo para la estrategia Golden POC + Clímax de Volumen + Filtro R:R 2:1.

Ejemplos:
  python live_scanner.py --test-telegram
  python live_scanner.py --symbols SOL/USDT BTC/USDT ETH/USDT --timeframes 1h 1d --interval 300
"""
import argparse
import sys
import time
try:
    from tabulate import tabulate
except ImportError:
    import pandas as pd
    def tabulate(rows, headers=(), **kwargs):
        return pd.DataFrame(rows, columns=headers).to_string(index=False)

from config import DEFAULT_CONFIG
from data_loader import DataLoader
from strategy_engine import compute_indicators, evaluate_bar
from telegram_notifier import TelegramNotifier

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def scan_once(symbols: list[str], timeframes: list[str], loader: DataLoader,
              notifier: TelegramNotifier, cfg=DEFAULT_CONFIG, send_alerts: bool = True):
    rows = []
    for sym in symbols:
        for tf in timeframes:
            try:
                df = loader.load_ohlcv(sym, tf, limit_bars=250, use_cache=False)
                if len(df) < cfg.vp_lookback:
                    rows.append([sym, tf, len(df), "HISTORIA INSUFICIENTE", "-", "-", "-", "-", "-"])
                    continue
                ind = compute_indicators(df, cfg)
                res = evaluate_bar(df, len(df) - 1, symbol=sym, timeframe=tf, cfg=cfg, ind=ind)
                if res is None:
                    continue
                rows.append([
                    sym,
                    tf,
                    f"{res.close:,.2f}",
                    f"{res.poc_low:,.2f} - {res.poc_high:,.2f}",
                    f"{res.res_low:,.2f} - {res.res_high:,.2f}",
                    f"{res.entry_price:,.2f}",
                    f"{res.sl_price:,.2f} (-{res.risk_pct:.1f}%)",
                    f"{res.tp_price:,.2f} (+{res.reward_pct:.1f}%)",
                    f"{res.rr_ratio:.2f}:1 ({'OK' if res.approved else 'NO'})",
                    "SÍ" if res.trigger_signal else ("ZONA POC" if res.in_buy_zone else "NO"),
                ])

                should_alert = res.approved and (res.trigger_signal or res.in_buy_zone)
                if send_alerts and should_alert:
                    key = f"{sym}_{tf}_{res.timestamp.isoformat()}"
                    if not notifier.already_sent(key):
                        html = notifier.format_html(res)
                        print("\n" + "=" * 80)
                        print(html.replace("<b>", "").replace("</b>", "").replace("&gt;", ">"))
                        print("=" * 80)
                        if notifier.send_message(html):
                            notifier.mark_sent(key)
            except Exception as e:
                rows.append([sym, tf, "ERROR", str(e), "-", "-", "-", "-", "-", "-"])

    headers = ["Par", "TF", "Close", "Zona POC", "Resistencia", "Entrada", "SL", "TP", "R:R", "Gatillo"]
    print("\n" + tabulate(rows, headers=headers, tablefmt="github"))


def main():
    ap = argparse.ArgumentParser(description="Escáner en Vivo — Golden POC & R:R 2:1")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_CONFIG.symbols)
    ap.add_argument("--timeframes", nargs="+", default=DEFAULT_CONFIG.timeframes)
    ap.add_argument("--interval", type=int, default=300, help="Segundos entre escaneos (default: 300)")
    ap.add_argument("--min-rr", type=float, default=DEFAULT_CONFIG.min_rr)
    ap.add_argument("--test-telegram", action="store_true", help="Envía mensaje de prueba y escanea una vez")
    ap.add_argument("--once", action="store_true", help="Ejecuta un único escaneo y termina")
    args = ap.parse_args()

    cfg = DEFAULT_CONFIG.override(symbols=args.symbols, timeframes=args.timeframes, min_rr=args.min_rr)
    loader = DataLoader(cfg)
    notifier = TelegramNotifier(cfg)

    if args.test_telegram:
        print("[live] Probando envío a Telegram...")
        ok = notifier.send_test()
        print(f"[live] Resultado test Telegram: {'ENVIADO OK' if ok else 'MODO CONSOLA / SIN CREDENCIALES'}")
        scan_once(cfg.symbols, cfg.timeframes, loader, notifier, cfg, send_alerts=False)
        return

    if args.once:
        scan_once(cfg.symbols, cfg.timeframes, loader, notifier, cfg, send_alerts=True)
        return

    print(f"[live] Monitor iniciado para {cfg.symbols} en {cfg.timeframes} (cada {args.interval}s)...")
    while True:
        scan_once(cfg.symbols, cfg.timeframes, loader, notifier, cfg, send_alerts=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
