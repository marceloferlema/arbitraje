"""
CLI modo tiempo real para alertas cripto (individual o multi-par en paralelo) + Ejecución en BingX Demo (VST).

Ejemplos:
  python cripto/run_live.py --test-bingx                                            # Prueba conexión y ciclo de órdenes en BingX Demo (VST)
  python cripto/run_live.py --multi --mode confirmed --telegram --bingx-demo        # Monitoreo multi-par + ejecución automática en BingX Demo (VST)
  python cripto/run_live.py --symbol SOLUSDT --mode confirmed --telegram --bingx-demo
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

from config_cripto import DEFAULT
from cripto_alertas.backtest import trade_from_signal
from cripto_alertas.bingx import BingXExecutor
from cripto_alertas.live import LiveRunner
from cripto_alertas.strategy import Signal


def parse():
    ap = argparse.ArgumentParser(description="Monitor en vivo – Barrido de liquidez + Volume Profile + BingX")
    ap.add_argument("--symbol", default=DEFAULT.symbol)
    ap.add_argument("--symbols", default=None, help="Lista de símbolos separados por coma")
    ap.add_argument("--multi", action="store_true", help="Monitorear todos los pares de DEFAULT.universe")
    ap.add_argument("--mode", choices=["pre", "confirmed", "both"], default="confirmed")
    ap.add_argument("--telegram", action="store_true", help="Enviar alertas a Telegram")
    ap.add_argument("--test-telegram", action="store_true", help="Enviar un mensaje de prueba al bot de Telegram Cripto")
    ap.add_argument("--chat-id", default=None, help="Sobreescribir CHAT_ID de destino en Telegram")
    ap.add_argument("--find-chats", action="store_true", help="Listar los chat_id donde el bot de Telegram recibió mensajes")
    ap.add_argument("--bingx-demo", action="store_true", help="Ejecutar órdenes automáticamente en BingX Modo Demo (VST)")
    ap.add_argument("--bingx-live", action="store_true", help="Ejecutar órdenes automáticamente en BingX Modo Real (USDT)")
    ap.add_argument("--bingx-dry-run", action="store_true", help="Simular llamadas a BingX sin enviar órdenes reales/VST")
    ap.add_argument("--test-bingx", action="store_true", help="Probar conexión a BingX Demo (VST), contratos y flujo de órdenes")
    ap.add_argument("--min-score", type=float, dest="min_score")
    ap.add_argument("--btc-filter", choices=["strict", "soft", "off"], dest="btc_filter")
    ap.add_argument("--be-r", type=float, dest="breakeven_r")
    ap.add_argument("--no-intrabar-pre", action="store_true")
    return ap.parse_args()


def run_bingx_test(cfg, symbols, dry_run=False):
    print("=" * 85)
    print("🧪 PRUEBA DE INTEGRACIÓN BINGX PERPETUAL FUTURES — MODO DEMO (VST)")
    print("=" * 85)
    ex = BingXExecutor(demo=True, dry_run=dry_run)
    contracts = ex.load_contracts()
    print(f"[bingx] Contratos cargados desde {ex.base_url}: {len(contracts)} pares disponibles.\n")

    print("Especificaciones de los pares de la cartera en BingX:")
    for sym in symbols:
        info = ex.contract_info(sym)
        print(f"  · {sym:8s} -> BingX: {info['symbol']:10s} | minQty: {info['min_qty']:<8} | "
              f"qtyPrec: {info['qty_prec']} | pricePrec: {info['price_prec']} | minUSDT: {info['min_usdt']}")

    if ex.api_key and ex.api_secret and not dry_run:
        try:
            bal = ex.get_balance()
            print(f"\n💰 Balance Cuenta Demo BingX ({bal['asset']}): "
                  f"Total={bal['balance']:,.2f} {bal['asset']} | Disponible={bal['availableMargin']:,.2f} {bal['asset']}")
        except Exception as e:
            print(f"\n⚠️ No se pudo consultar el balance VST ({e}). Pasando a simulación de flujo de órdenes.")
            ex.dry_run = True
    else:
        print("\nℹ️ Para que el bot opere contra tu cuenta Demo VST real de BingX, agregá en tu archivo `.env`:")
        print("   BINGX_API_KEY=tu_api_key_de_bingx")
        print("   BINGX_SECRET=tu_secret_key_de_bingx")
        print("\nSimulando ciclo completo de una señal CONFIRMADA (Entrada + SL + TP1 35% + TP2 65% + Breakeven):")
        ex.dry_run = True

    now = pd.Timestamp.now(tz="UTC")
    dummy_sig = Signal(
        id="XRPUSDT-CON-L-TEST", kind="CONFIRMADA", direction="LONG", symbol="XRPUSDT",
        time=now, bar_idx=0, entry=1.1240, sl=1.0890, tp=1.2480, rr=3.54, score=78.0,
        entry_type="MERCADO", zone={"key": 1, "low": 1.095, "high": 1.115, "wall_score": 0.75},
        target_zone={"low": 1.2518, "high": 1.289}, level=1.1180, level_time=now.isoformat(),
        tp1_price=1.1940,
        sizing={"equity": 1000.0, "margin_usd": 43.8, "margin_pct": 0.0438, "leverage": 3,
                "notional_usd": 131.4, "qty": 116.9039, "risk_usd": 4.09, "risk_pct": 0.0041,
                "reward_usd": 14.49, "sl_pct": 0.0311, "tp_pct": 0.1103},
    )
    tr = trade_from_signal(dummy_sig, cfg)
    print("\n1️⃣ Al dispararse la señal CONFIRMADA:")
    ex.on_signal(dummy_sig, tr, cfg)

    print("\n2️⃣ Cuando el precio alcanza +1.5R (evento BE -> mueve SL a Breakeven +0.15R):")
    tr.sl = 1.1292
    tr.be_moved = True
    ex.on_event(tr, ("BE", now, tr.sl), cfg)

    print("\n3️⃣ Cuando el precio alcanza +2.0R (evento TP1 -> cierra 35% y ajusta SL al 65% restante):")
    tr.partial_done = True
    ex.on_event(tr, ("TP1", now, 1.1940), cfg)
    print("=" * 85)


def find_telegram_chats():
    import requests
    from cripto_alertas.alerts import load_env
    env = load_env()
    token = env.get("CRIPTO_TELEGRAM_TOKEN") or env.get("TELEGRAM_TOKEN")
    if not token:
        print("⚠️ No se encontró TELEGRAM_TOKEN en .env")
        return
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10).json()
    chats = {}
    for upd in r.get("result", []):
        for k in ("message", "channel_post", "my_chat_member"):
            if k in upd and "chat" in upd[k]:
                c = upd[k]["chat"]
                cid = str(c.get("id"))
                title = c.get("title") or c.get("username") or f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
                ctype = c.get("type", "")
                chats[cid] = (ctype, title)
    print(f"CHAT_ID actual en .env -> CHAT_ID={env.get('CHAT_ID')} | CRIPTO_CHAT_ID={env.get('CRIPTO_CHAT_ID')}")
    if not chats:
        print("\nNo hay mensajes recientes en getUpdates. Mandá un mensaje (ej. 'hola') en el nuevo grupo/canal donde metiste al bot y volvé a correr `--find-chats`.")
    else:
        print("\nChats detectados por el bot:")
        for cid, (ctype, title) in chats.items():
            print(f"  · chat_id: {cid:<16s} | tipo: {ctype:<10s} | nombre: {title}")


def main():
    a = parse()
    if a.find_chats:
        find_telegram_chats()
        return
    if a.test_telegram:
        from cripto_alertas.alerts import Notifier, load_env
        env = load_env()
        using_cripto_tok = bool(env.get("CRIPTO_TELEGRAM_TOKEN"))
        n = Notifier(telegram=True, console=True, chat_id=a.chat_id)
        print(f"[telegram] Usando {'CRIPTO_TELEGRAM_TOKEN' if using_cripto_tok else 'TELEGRAM_TOKEN'} -> chat_id={n.chat_id}")
        n.send("🟢 Prueba de conexión exitosa — Bot de Alertas Cripto (Barrido de Liquidez + Volume Profile) activo en su propio chat.")
        return
    cfg = DEFAULT.override(min_score=a.min_score, btc_filter=a.btc_filter, breakeven_r=a.breakeven_r)
    kinds = {"pre": ("PRE",), "confirmed": ("CONFIRMADA",), "both": ("PRE", "CONFIRMADA")}[a.mode]

    if a.symbols:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    elif a.multi or a.test_bingx:
        symbols = list(cfg.universe)
    else:
        symbols = [a.symbol.upper()]

    if a.test_bingx:
        run_bingx_test(cfg, symbols, dry_run=a.bingx_dry_run)
        return

    executor = None
    if a.bingx_demo or a.bingx_live or a.bingx_dry_run:
        executor = BingXExecutor(demo=not a.bingx_live, dry_run=a.bingx_dry_run)

    if len(symbols) == 1:
        runner = LiveRunner(cfg.override(symbol=symbols[0]), telegram=a.telegram,
                            kinds=kinds, intrabar_pre=not a.no_intrabar_pre, executor=executor, chat_id=a.chat_id)
        runner.run()
        return

    runners = []
    for sym in symbols:
        r = LiveRunner(cfg.override(symbol=sym), telegram=a.telegram,
                       kinds=kinds, intrabar_pre=not a.no_intrabar_pre, executor=executor, chat_id=a.chat_id)
        r.bootstrap()
        threading.Thread(target=r._worker, daemon=True).start()
        threading.Thread(target=r._ws_loop, daemon=True).start()
        runners.append(r)

    print(f"[live] Monitoreando {len(runners)} pares en paralelo ({', '.join(symbols)}). Ctrl+C para salir.")
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n[live] deteniendo todos los monitores...")
        for r in runners:
            r.stop.set()
            r.save_state()


if __name__ == "__main__":
    main()
