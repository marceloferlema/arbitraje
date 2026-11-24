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
from cripto_alertas.ai_analyst import GeminiAnalyst
from cripto_alertas.backtest import trade_from_signal
from cripto_alertas.binance_exec import BinanceExecutor
from cripto_alertas.bingx import BingXExecutor
from cripto_alertas.live import LiveRunner
from cripto_alertas.radar import format_radar_report
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
    ap.add_argument("--binance-testnet", action="store_true", help="Ejecutar órdenes automáticamente en Binance Futures Testnet")
    ap.add_argument("--binance-live", action="store_true", help="Ejecutar órdenes automáticamente en Binance Futures Real (USDT)")
    ap.add_argument("--env", choices=["auto", "live", "testnet", "paper"], default="auto",
                    help="Subcarpeta aislada en alerts_log/ (default: auto según --binance-live/--binance-testnet)")
    ap.add_argument("--test-binance", action="store_true", help="Probar conexión a Binance Futures Testnet y contratos")
    ap.add_argument("--test-gemini", action="store_true", help="Probar análisis cuantitativo en vivo con Gemini")
    ap.add_argument("--weekly-audit", action="store_true", help="Ejecutar auditoría semanal de resultados con Gemini")
    ap.add_argument("--export-audit", action="store_true", help="Exportar paquete Markdown desde SQLite para auditoría profunda (costo $0)")
    ap.add_argument("--audit-days", type=int, default=30, help="Ventana de días para --export-audit (default: 30)")
    ap.add_argument("--radar-hours", type=float, default=4.0, help="Intervalo en horas para el reporte Radar de zonas a Telegram (default: 4.0, 0 para desactivar)")
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
    entry_px = 1.0000
    if not ex.dry_run and isinstance(ex, BinanceExecutor):
        try:
            tk = ex._request("GET", "/fapi/v1/ticker/price", {"symbol": "XRPUSDT"}, signed=False)
            entry_px = float(tk.get("price", 1.0))
            print(f"[binance] Precio actual XRPUSDT en Testnet: {entry_px:.4f}")
        except Exception:
            pass

    sl_px = round(entry_px * 0.95, 4)
    tp1_px = round(entry_px * 1.05, 4)
    tp_px = round(entry_px * 1.10, 4)
    qty_est = round(100.0 / max(entry_px, 0.01), 1)

    dummy_sig = Signal(
        id="XRPUSDT-CON-L-TEST", kind="CONFIRMADA", direction="LONG", symbol="XRPUSDT",
        time=now, bar_idx=0, entry=entry_px, sl=sl_px, tp=tp_px, rr=2.0, score=78.0,
        entry_type="MERCADO", zone={"key": 1, "low": sl_px, "high": entry_px, "wall_score": 0.75},
        target_zone={"low": tp_px, "high": tp_px * 1.02}, level=entry_px, level_time=now.isoformat(),
        tp1_price=tp1_px,
        sizing={"equity": 1000.0, "margin_usd": 33.3, "margin_pct": 0.0333, "leverage": 3,
                "notional_usd": 100.0, "qty": qty_est, "risk_usd": 5.0, "risk_pct": 0.005,
                "reward_usd": 10.0, "sl_pct": 0.05, "tp_pct": 0.10},
    )
    tr = trade_from_signal(dummy_sig, cfg)
    print("\n1️⃣ Al dispararse la señal CONFIRMADA:")
    ex.on_signal(dummy_sig, tr, cfg)

    print("\n2️⃣ Cuando el precio alcanza +1.5R (evento BE -> mueve SL):")
    tr.sl = round(entry_px * 0.985, 4)
    tr.be_moved = True
    ex.on_event(tr, ("BE", now, tr.sl), cfg)

    print("\n3️⃣ Cuando el precio alcanza +2.0R (evento TP1 -> cierra 35% y ajusta SL al 65% restante):")
    tr.partial_done = True
    ex.on_event(tr, ("TP1", now, tp1_px), cfg)

    if not ex.dry_run:
        print("\n4️⃣ Limpiando órdenes y cerrando posición de prueba en Testnet:")
        ex.on_event(tr, ("TIEMPO", now, entry_px), cfg)
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


def start_radar_scheduler(runners, interval_hours, stop_event=None):
    if interval_hours <= 0 or not runners:
        return

    analyst = GeminiAnalyst()

    def _emit_radar_and_ai(n):
        rep = format_radar_report(runners)
        if rep:
            n.send(rep)
        if analyst.enabled:
            ai_txt = analyst.analyze_radar_snapshot(runners)
            if ai_txt:
                n.send(ai_txt)
                if getattr(n, "storage", None) is not None:
                    try:
                        n.storage.log_ai_report("4H", analyst.model, ai_txt)
                    except Exception:
                        pass

    def _loop():
        # Espera inicial para estabilizar websockets y contextos
        time.sleep(8)
        n = runners[0].notifier
        try:
            _emit_radar_and_ai(n)
        except Exception as e:
            print(f"[radar] error en reporte inicial: {e}")

        last_sent = time.time()
        last_audit_week = None
        while stop_event is None or not stop_event.is_set():
            time.sleep(30)
            if time.time() - last_sent >= interval_hours * 3600:
                try:
                    _emit_radar_and_ai(n)
                    last_sent = time.time()
                except Exception as e:
                    print(f"[radar] error en ciclo periódico: {e}")
            # Auditoría semanal automática los domingos 21:00 ART (= lunes 00:00 UTC)
            if analyst.enabled:
                now_utc = pd.Timestamp.now(tz="UTC")
                iso_week = f"{now_utc.isocalendar().year}-W{now_utc.isocalendar().week}"
                if now_utc.weekday() == 0 and now_utc.hour == 0 and last_audit_week != iso_week:
                    try:
                        audit = analyst.run_weekly_audit(runners[0].cfg.alerts_dir, runners[0].cfg)
                        if audit:
                            n.send(audit)
                        last_audit_week = iso_week
                    except Exception as e:
                        print(f"[gemini] error en auditoría semanal: {e}")

    th = threading.Thread(target=_loop, daemon=True)
    th.start()
    return th


def run_binance_test(cfg, symbols, dry_run=False):
    print("=" * 85)
    print("🧪 PRUEBA DE INTEGRACIÓN BINANCE USD-M FUTURES — MODO TESTNET (DEMO)")
    print("=" * 85)
    ex = BinanceExecutor(testnet=True, dry_run=dry_run)
    contracts = ex.load_contracts()
    print(f"[binance] Contratos cargados desde {ex.base_url}: {len(contracts)} pares disponibles.\n")

    print("Especificaciones de los pares de la cartera en Binance Futures:")
    for sym in symbols:
        info = ex.contract_info(sym)
        print(f"  · {sym:8s} -> Binance: {info['symbol']:10s} | minQty: {info['min_qty']:<8} | "
              f"qtyPrec: {info['qty_prec']} | pricePrec: {info['price_prec']} | minUSDT: {info['min_usdt']}")

    if ex.api_key and ex.api_secret and not dry_run:
        try:
            bal = ex.get_balance()
            print(f"\n💰 Balance Cuenta Testnet Binance ({bal['asset']}): "
                  f"Total={bal['balance']:,.2f} {bal['asset']} | Disponible={bal['availableMargin']:,.2f} {bal['asset']}")
        except Exception as e:
            print(f"\n⚠️ No se pudo consultar el balance Testnet ({e}). Pasando a simulación de flujo de órdenes.")
            ex.dry_run = True
    else:
        print("\nℹ️ Para que el bot opere contra tu cuenta Testnet o Real de Binance, agregá en tu `.env`:")
        print("   BINANCE_API_KEY=tu_api_key_de_binance")
        print("   BINANCE_API_SECRET=tu_secret_key_de_binance")
        print("\nSimulando ciclo completo de una señal CONFIRMADA (Entrada + SL + TP1 35% + TP2 65% + Breakeven):")
        ex.dry_run = True

    now = pd.Timestamp.now(tz="UTC")
    entry_px = 1.0000
    if not ex.dry_run and isinstance(ex, BinanceExecutor):
        try:
            tk = ex._request("GET", "/fapi/v1/ticker/price", {"symbol": "XRPUSDT"}, signed=False)
            entry_px = float(tk.get("price", 1.0))
            print(f"[binance] Precio actual XRPUSDT en Testnet: {entry_px:.4f}")
        except Exception:
            pass

    sl_px = round(entry_px * 0.95, 4)
    tp1_px = round(entry_px * 1.05, 4)
    tp_px = round(entry_px * 1.10, 4)
    qty_est = round(100.0 / max(entry_px, 0.01), 1)

    dummy_sig = Signal(
        id="XRPUSDT-CON-L-TEST", kind="CONFIRMADA", direction="LONG", symbol="XRPUSDT",
        time=now, bar_idx=0, entry=entry_px, sl=sl_px, tp=tp_px, rr=2.0, score=78.0,
        entry_type="MERCADO", zone={"key": 1, "low": sl_px, "high": entry_px, "wall_score": 0.75},
        target_zone={"low": tp_px, "high": tp_px * 1.02}, level=entry_px, level_time=now.isoformat(),
        tp1_price=tp1_px,
        sizing={"equity": 1000.0, "margin_usd": 33.3, "margin_pct": 0.0333, "leverage": 3,
                "notional_usd": 100.0, "qty": qty_est, "risk_usd": 5.0, "risk_pct": 0.005,
                "reward_usd": 10.0, "sl_pct": 0.05, "tp_pct": 0.10},
    )
    tr = trade_from_signal(dummy_sig, cfg)
    print("\n1️⃣ Al dispararse la señal CONFIRMADA:")
    ex.on_signal(dummy_sig, tr, cfg)

    print("\n2️⃣ Cuando el precio alcanza +1.5R (evento BE -> mueve SL):")
    tr.sl = round(entry_px * 0.985, 4)
    tr.be_moved = True
    ex.on_event(tr, ("BE", now, tr.sl), cfg)

    print("\n3️⃣ Cuando el precio alcanza +2.0R (evento TP1 -> cierra 35% y ajusta SL al 65% restante):")
    tr.partial_done = True
    ex.on_event(tr, ("TP1", now, tp1_px), cfg)

    if not ex.dry_run:
        print("\n4️⃣ Limpiando órdenes y cerrando posición de prueba en Testnet:")
        ex.on_event(tr, ("TIEMPO", now, entry_px), cfg)
    print("=" * 85)


def executor_status_line(executor) -> str:
    if executor is None:
        return "📢 Modo ejecución: Solo alertas (sin envío automático de órdenes)"
    if isinstance(executor, BinanceExecutor):
        modo = "SIMULACIÓN" if executor.dry_run else ("TESTNET (DEMO)" if executor.testnet else "REAL (LIVE)")
        ex_name = f"Binance Futures {modo}"
    else:
        modo = "SIMULACIÓN" if executor.dry_run else ("DEMO (VST)" if executor.demo else "REAL (LIVE)")
        ex_name = f"BingX Futures {modo}"
    try:
        bal = executor.get_balance()
        return f"🤖 Ejecución automática: {ex_name} · Saldo disponible: {bal['availableMargin']:,.2f} {bal['asset']}"
    except Exception as e:
        return f"🤖 Ejecución automática: {ex_name} (aviso balance: {e})"


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
    if a.env == "auto":
        if a.binance_live or a.bingx_live:
            run_env = "live"
        elif a.binance_testnet or a.bingx_demo or a.bingx_dry_run:
            run_env = "testnet"
        else:
            run_env = "paper"
    else:
        run_env = a.env
    env_alerts_dir = os.path.join(cfg.alerts_dir, run_env)
    os.makedirs(env_alerts_dir, exist_ok=True)
    cfg = cfg.override(alerts_dir=env_alerts_dir)
    print(f"[env] Entorno aislado: {run_env.upper()} -> {cfg.alerts_dir}")
    if a.export_audit:
        from cripto_alertas.storage import BotStorage
        st = BotStorage(cfg.alerts_dir)
        md = st.export_audit_markdown(days=a.audit_days, cfg=cfg)
        print(md)
        print(f"\n✅ Guardado también en: {os.path.join(cfg.alerts_dir, 'audit_bundle.md')}")
        return
    kinds = {"pre": ("PRE",), "confirmed": ("CONFIRMADA",), "both": ("PRE", "CONFIRMADA")}[a.mode]

    if a.symbols:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    elif a.multi or a.test_bingx or a.test_binance or a.test_gemini:
        symbols = list(cfg.universe)
    else:
        symbols = [a.symbol.upper()]

    if a.test_bingx:
        run_bingx_test(cfg, symbols, dry_run=a.bingx_dry_run)
        return
    if a.test_binance:
        run_binance_test(cfg, symbols)
        return
    if a.test_gemini or a.weekly_audit:
        from cripto_alertas.alerts import Notifier
        analyst = GeminiAnalyst()
        if not analyst.enabled:
            print("⚠️ GEMINI_API_KEY no encontrada en .env. Agregá GEMINI_API_KEY=tu_clave en .env")
            return
        n = Notifier(telegram=a.telegram, console=True, chat_id=a.chat_id)
        if a.weekly_audit:
            txt = analyst.run_weekly_audit(cfg.alerts_dir, cfg)
            if txt:
                n.send(txt)
            return
        runners = []
        for sym in symbols:
            r = LiveRunner(cfg.override(symbol=sym), telegram=False)
            r.bootstrap()
            runners.append(r)
        txt = analyst.analyze_radar_snapshot(runners)
        if txt:
            n.send(txt)
        return

    executor = None
    if a.binance_testnet or a.binance_live:
        executor = BinanceExecutor(testnet=not a.binance_live)
    elif a.bingx_demo or a.bingx_live or a.bingx_dry_run:
        executor = BingXExecutor(demo=not a.bingx_live, dry_run=a.bingx_dry_run)

    if len(symbols) == 1:
        runner = LiveRunner(cfg.override(symbol=symbols[0]), telegram=a.telegram,
                            kinds=kinds, intrabar_pre=not a.no_intrabar_pre, executor=executor, chat_id=a.chat_id)
        if a.telegram:
            runner.notifier.send(
                f"🚀 Monitor Cripto iniciado en vivo ({symbols[0]} · modo {'+'.join(kinds)})\n"
                f"{executor_status_line(executor)}"
            )
        start_radar_scheduler([runner], a.radar_hours, runner.stop)
        runner.run()
        return

    runners = []
    def _portfolio_open():
        return sum(1 for rn in runners for x in rn.trades if not x.is_closed and x.status == "ABIERTA")

    for sym in symbols:
        r = LiveRunner(cfg.override(symbol=sym), telegram=a.telegram,
                       kinds=kinds, intrabar_pre=not a.no_intrabar_pre, executor=executor,
                       chat_id=a.chat_id, portfolio_open_fn=_portfolio_open)
        r.bootstrap()
        threading.Thread(target=r._worker, daemon=True).start()
        threading.Thread(target=r._ws_loop, daemon=True).start()
        runners.append(r)

    if a.telegram and runners:
        resumen = [
            f"🚀 Monitor de Alertas Cripto ACTIVO ({len(runners)} pares · modo {'+'.join(kinds)})",
            executor_status_line(executor),
        ]
        for r in runners:
            px = float(r.h1["close"].iloc[-1]) if len(r.h1) else 0.0
            resumen.append(f"  · {r.cfg.symbol}: {px:,.4f} USDT | 1D: {r.ctx.structure.summary()}")
        runners[0].notifier.send("\n".join(resumen))

    stop_event = threading.Event()
    start_radar_scheduler(runners, a.radar_hours, stop_event)

    print(f"[live] Monitoreando {len(runners)} pares en paralelo ({', '.join(symbols)}). Ctrl+C para salir.")
    try:
        while True:
            time.sleep(30)
            for r in runners:
                r.check_hourly_failsafe()
    except KeyboardInterrupt:
        print("\n[live] deteniendo todos los monitores...")
        stop_event.set()
        for r in runners:
            r.stop.set()
            r.save_state()


if __name__ == "__main__":
    main()
