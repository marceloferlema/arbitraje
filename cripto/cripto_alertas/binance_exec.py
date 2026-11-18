"""
Ejecutor automático para Binance USD-M Perpetual Futures con soporte nativo para Testnet.

- Modo Testnet (Demo USDT): https://testnet.binancefuture.com
- Modo Real (USDT):         https://fapi.binance.com

Variables en `.env`:
  BINANCE_API_KEY=...
  BINANCE_API_SECRET=...   (o BINANCE_SECRET=...)

Funciones que automatiza en cada señal y evento:
  1. Configura Margen AISLADO (`ISOLATED`) y Apalancamiento (`1x`–`3x`) del par en Binance.
  2. Abre la orden principal (`MARKET` en CONFIRMADA, `LIMIT` en PRE).
  3. Carga orden `STOP_MARKET` para el Stop Loss (100% de la posición).
  4. Divide el Take Profit en 2 órdenes `TAKE_PROFIT_MARKET`:
     - `TP1` por el 35% de la posición en `+2.0R`.
     - `TP2` por el 65% restante en el `TP` final.
  5. Al dispararse el evento `BE` (+1.5R) o `TP1` (+2.0R), cancela el Stop Loss anterior en Binance
     y coloca el nuevo Stop Loss en Breakeven (`entrada + 0.15R`) por la cantidad remanente.
  6. Al dispararse `TIEMPO` (time-stop 7 días), cierra a mercado la posición remanente y limpia órdenes abiertas.
"""
import hashlib
import hmac
import math
import time
from urllib.parse import urlencode

import requests

from .alerts import load_env

TESTNET_URL = "https://testnet.binancefuture.com"
LIVE_URL = "https://fapi.binance.com"


def _decimals_from_step(step_str: str) -> int:
    s = str(step_str).rstrip("0")
    if "." not in s:
        return 0
    return len(s.split(".")[1])


class BinanceExecutor:
    def __init__(self, testnet: bool = True, api_key: str = None, api_secret: str = None,
                 dry_run: bool = False, notifier=None):
        env = load_env()
        self.testnet = testnet
        self.base_url = TESTNET_URL if testnet else LIVE_URL
        self.unit = "USDT (Testnet)" if testnet else "USDT"
        self.api_key = api_key or env.get("BINANCE_API_KEY") or ""
        self.api_secret = (api_secret or env.get("BINANCE_API_SECRET")
                           or env.get("BINANCE_SECRET") or "")
        self.dry_run = dry_run or not (self.api_key and self.api_secret)
        self.notifier = notifier
        self.session = requests.Session()
        self._contracts = {}
        self._dual_side = False  # Por defecto Binance usa One-Way Mode (BOTH), consultamos igual
        self._initialized_symbols = set()

        if not self.api_key or not self.api_secret:
            print(f"[binance] ⚠️ BINANCE_API_KEY / BINANCE_API_SECRET no encontrados en .env -> "
                  f"ejecutando en modo SIMULACIÓN LOCAL ({self.unit}).")
        else:
            mode_str = "TESTNET (DEMO)" if self.testnet else "REAL (LIVE)"
            print(f"[binance] ✅ Ejecutor Binance Futures activo en modo {mode_str} ({self.base_url})")
            try:
                self._dual_side = self.get_dual_side_mode()
                print(f"[binance] Modo de posición en cuenta: {'Hedge (LONG/SHORT)' if self._dual_side else 'One-Way (BOTH)'}")
            except Exception as e:
                print(f"[binance] Aviso al consultar modo de posición ({e}); asumiendo One-Way Mode.")

    # --------------------------------------------------------- Firma y HTTP
    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}"

    def _request(self, method: str, path: str, params: dict = None, signed: bool = True) -> dict:
        params = dict(params or {})
        headers = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", 5000)
            qs = self._sign(params)
            url = f"{self.base_url}{path}?{qs}"
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            qs = urlencode(params)
            url = f"{self.base_url}{path}?{qs}" if qs else f"{self.base_url}{path}"

        r = self.session.request(method, url, headers=headers, timeout=15)
        try:
            data = r.json()
        except Exception:
            r.raise_for_status()
            raise
        if r.status_code >= 400 or (isinstance(data, dict) and "code" in data and data["code"] < 0):
            code = data.get("code", r.status_code) if isinstance(data, dict) else r.status_code
            msg = data.get("msg", r.text) if isinstance(data, dict) else r.text
            raise RuntimeError(f"Binance API error {code}: {msg}")
        return data

    def _notify(self, text: str):
        if self.notifier is not None:
            self.notifier.send(text)
        else:
            print(text)

    # ------------------------------------------------ Especificaciones y Cuenta
    def load_contracts(self) -> dict:
        if self._contracts:
            return self._contracts
        try:
            data = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
            for sym_info in data.get("symbols", []):
                sym = sym_info.get("symbol")
                if not sym or sym_info.get("status") != "TRADING":
                    continue
                tick_size = "0.01"
                step_size = "0.001"
                min_qty = 0.001
                min_notional = 5.0
                for f in sym_info.get("filters", []):
                    ft = f.get("filterType")
                    if ft == "PRICE_FILTER":
                        tick_size = f.get("tickSize", tick_size)
                    elif ft == "LOT_SIZE":
                        step_size = f.get("stepSize", step_size)
                        min_qty = float(f.get("minQty", min_qty))
                    elif ft == "MIN_NOTIONAL":
                        min_notional = float(f.get("notional", min_notional))
                self._contracts[sym] = {
                    "symbol": sym,
                    "tick_size": float(tick_size),
                    "price_prec": _decimals_from_step(tick_size),
                    "step_size": float(step_size),
                    "qty_prec": _decimals_from_step(step_size),
                    "min_qty": min_qty,
                    "min_usdt": min_notional,
                }
        except Exception as e:
            print(f"[binance] Aviso: no se pudo cargar exchangeInfo ({e}). Usando defaults.")
        return self._contracts

    def contract_info(self, symbol: str) -> dict:
        sym = symbol.upper().strip()
        if not self._contracts:
            self.load_contracts()
        return self._contracts.get(sym, {
            "symbol": sym,
            "tick_size": 0.0001,
            "price_prec": 4,
            "step_size": 0.001,
            "qty_prec": 3,
            "min_qty": 0.001,
            "min_usdt": 5.0,
        })

    def round_qty(self, symbol: str, qty: float) -> float:
        info = self.contract_info(symbol)
        step = info["step_size"]
        prec = info["qty_prec"]
        val = math.floor(qty / step) * step if step > 0 else qty
        return round(max(val, 0.0), prec)

    def round_price(self, symbol: str, price: float) -> float:
        info = self.contract_info(symbol)
        tick = info["tick_size"]
        prec = info["price_prec"]
        val = round(price / tick) * tick if tick > 0 else price
        return round(val, prec)

    def get_dual_side_mode(self) -> bool:
        data = self._request("GET", "/fapi/v1/positionSide/dual")
        return bool(data.get("dualSidePosition", False))

    def get_balance(self) -> dict:
        if self.dry_run:
            return {"asset": "USDT", "balance": 1000.0, "availableMargin": 1000.0}
        data = self._request("GET", "/fapi/v2/balance")
        for item in data or []:
            if item.get("asset") == "USDT":
                return {
                    "asset": "USDT",
                    "balance": float(item.get("balance", 0)),
                    "availableMargin": float(item.get("availableBalance", 0)),
                }
        return {"asset": "USDT", "balance": 0.0, "availableMargin": 0.0}

    def ensure_symbol_setup(self, symbol: str, leverage: int):
        sym = symbol.upper().strip()
        if self.dry_run or sym in self._initialized_symbols:
            return
        try:
            self._request("POST", "/fapi/v1/marginType", {"symbol": sym, "marginType": "ISOLATED"})
        except Exception as e:
            # -4046: No need to change margin type (ya está en ISOLATED)
            if "-4046" not in str(e):
                print(f"[binance] Aviso marginType {sym}: {e}")
        try:
            self._request("POST", "/fapi/v1/leverage", {"symbol": sym, "leverage": int(leverage)})
        except Exception as e:
            print(f"[binance] Aviso leverage {sym}: {e}")
        self._initialized_symbols.add(sym)

    # ----------------------------------------------------- Ejecución de Órdenes
    def _place_order(self, params: dict) -> str:
        if self.dry_run:
            return f"SIM-BIN-{int(time.time() * 1000) % 1000000}"
        res = self._request("POST", "/fapi/v1/order", params)
        return str(res.get("orderId", ""))

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        if not order_id or self.dry_run:
            return True
        sym = symbol.upper().strip()
        try:
            self._request("DELETE", "/fapi/v1/order", {"symbol": sym, "orderId": order_id})
            return True
        except Exception as e:
            print(f"[binance] Aviso cancelando orden {order_id} en {sym}: {e}")
            return False

    def on_signal(self, s, tr, cfg) -> dict:
        """
        Ejecuta una señal nueva en Binance Futures:
        - Orden principal (MARKET o LIMIT)
        - Stop Loss (STOP_MARKET)
        - TP1 (35% en +2.0R) y TP2 (65% en TP final)
        """
        sym = s.symbol.upper().strip()
        lev = int(s.sizing.get("leverage", cfg.leverage_max))
        self.ensure_symbol_setup(sym, lev)

        info = self.contract_info(sym)
        total_qty = self.round_qty(sym, tr.qty)
        if total_qty < info["min_qty"]:
            total_qty = info["min_qty"]

        long = s.direction == "LONG"
        open_side = "BUY" if long else "SELL"
        close_side = "SELL" if long else "BUY"
        pos_side = ("LONG" if long else "SHORT") if self._dual_side else "BOTH"

        entry_px = self.round_price(sym, s.entry)
        sl_px = self.round_price(sym, s.sl)
        tp_px = self.round_price(sym, s.tp)
        tp1_px = self.round_price(sym, s.tp1_price) if s.tp1_price else None

        ptp_frac = getattr(cfg, "partial_tp_frac", 0.35) or 0.0
        tp1_qty = self.round_qty(sym, total_qty * ptp_frac) if (tp1_px and ptp_frac > 0) else 0.0
        tp2_qty = self.round_qty(sym, total_qty - tp1_qty)
        if tp1_qty < info["min_qty"] or tp2_qty < info["min_qty"]:
            tp1_qty = 0.0
            tp2_qty = total_qty

        orders = {
            "symbol": sym,
            "unit": self.unit,
            "testnet": self.testnet,
            "dry_run": self.dry_run,
            "total_qty": total_qty,
            "tp1_qty": tp1_qty,
            "tp2_qty": tp2_qty,
        }

        try:
            # 1. Orden de entrada
            entry_params = {
                "symbol": sym,
                "side": open_side,
                "positionSide": pos_side,
                "type": "MARKET" if s.entry_type == "MERCADO" else "LIMIT",
                "quantity": total_qty,
            }
            if s.entry_type != "MERCADO":
                entry_params["price"] = entry_px
                entry_params["timeInForce"] = "GTC"
            orders["entry_order_id"] = self._place_order(entry_params)

            # 2. Stop Loss (100% de la posición)
            sl_params = {
                "symbol": sym,
                "side": close_side,
                "positionSide": pos_side,
                "type": "STOP_MARKET",
                "stopPrice": sl_px,
                "quantity": total_qty,
                "workingType": "MARK_PRICE",
            }
            if not self._dual_side:
                sl_params["reduceOnly"] = "true"
            orders["sl_order_id"] = self._place_order(sl_params)

            # 3. TP1 parcial (35%)
            if tp1_qty > 0 and tp1_px:
                tp1_params = {
                    "symbol": sym,
                    "side": close_side,
                    "positionSide": pos_side,
                    "type": "TAKE_PROFIT_MARKET",
                    "stopPrice": tp1_px,
                    "quantity": tp1_qty,
                    "workingType": "MARK_PRICE",
                }
                if not self._dual_side:
                    tp1_params["reduceOnly"] = "true"
                orders["tp1_order_id"] = self._place_order(tp1_params)

            # 4. TP2 final (65%)
            tp2_params = {
                "symbol": sym,
                "side": close_side,
                "positionSide": pos_side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp_px,
                "quantity": tp2_qty,
                "workingType": "MARK_PRICE",
            }
            if not self._dual_side:
                tp2_params["reduceOnly"] = "true"
            orders["tp2_order_id"] = self._place_order(tp2_params)

            tr.exchange_orders = orders
            tag = f"BINANCE {'TESTNET-SIM' if self.dry_run else ('TESTNET' if self.testnet else 'LIVE')}"
            msg = (
                f"🤖 [{tag}] Orden ejecutada en {sym} ({s.direction} {lev}x Aislado)\n"
                f"  · Entrada ({entry_params['type']}): {entry_px} | Qty: {total_qty} (ID: {orders.get('entry_order_id')})\n"
                f"  · SL cargado: {sl_px} por {total_qty} (ID: {orders.get('sl_order_id')})\n"
                + (f"  · TP1 parcial (35%): {tp1_px} por {tp1_qty} (ID: {orders.get('tp1_order_id')})\n" if tp1_qty > 0 else "")
                + f"  · TP final: {tp_px} por {tp2_qty} (ID: {orders.get('tp2_order_id')})"
            )
            self._notify(msg)
        except Exception as e:
            self._notify(f"⚠️ [Binance] Error al enviar órdenes para {sym}: {e}")
        return orders

    def on_event(self, tr, event, cfg):
        """
        Sincroniza eventos de seguimiento con Binance Futures:
        - `BE` / `TP1`: mueve el SL a Breakeven (+0.15R) por la cantidad remanente.
        - `TIEMPO`: cierra a mercado el remanente y limpia órdenes pendientes.
        - `TP` / `SL` / `CANCELADA` / `EXPIRADA`: cancela órdenes huérfanas.
        """
        name, ts, price = event
        orders = getattr(tr, "exchange_orders", None) or {}
        if not orders:
            return
        sym = (orders.get("symbol") or cfg.symbol).upper().strip()
        long = tr.direction == "LONG"
        close_side = "SELL" if long else "BUY"
        pos_side = ("LONG" if long else "SHORT") if self._dual_side else "BOTH"
        tag = f"BINANCE {'TESTNET-SIM' if self.dry_run else ('TESTNET' if self.testnet else 'LIVE')}"

        try:
            if name in ("BE", "TP1"):
                old_sl_id = orders.get("sl_order_id")
                if old_sl_id:
                    self.cancel_order(sym, old_sl_id)
                rem_qty = orders.get("tp2_qty", orders.get("total_qty", tr.qty)) if tr.partial_done else orders.get("total_qty", tr.qty)
                rem_qty = self.round_qty(sym, rem_qty)
                new_sl_px = self.round_price(sym, tr.sl)
                sl_params = {
                    "symbol": sym,
                    "side": close_side,
                    "positionSide": pos_side,
                    "type": "STOP_MARKET",
                    "stopPrice": new_sl_px,
                    "quantity": rem_qty,
                    "workingType": "MARK_PRICE",
                }
                if not self._dual_side:
                    sl_params["reduceOnly"] = "true"
                new_id = self._place_order(sl_params)
                orders["sl_order_id"] = new_id
                tr.exchange_orders = orders
                self._notify(f"🤖 [{tag}] SL actualizado en {sym} ({name}) -> {new_sl_px} por qty {rem_qty} (ID: {new_id})")

            elif name == "TIEMPO":
                for k in ("sl_order_id", "tp1_order_id", "tp2_order_id"):
                    if orders.get(k):
                        self.cancel_order(sym, orders[k])
                rem_qty = orders.get("tp2_qty", orders.get("total_qty", tr.qty)) if tr.partial_done else orders.get("total_qty", tr.qty)
                rem_qty = self.round_qty(sym, rem_qty)
                close_params = {
                    "symbol": sym,
                    "side": close_side,
                    "positionSide": pos_side,
                    "type": "MARKET",
                    "quantity": rem_qty,
                }
                if not self._dual_side:
                    close_params["reduceOnly"] = "true"
                cid = self._place_order(close_params)
                self._notify(f"🤖 [{tag}] Time-stop ejecutado a mercado en {sym} por qty {rem_qty} (ID: {cid})")

            elif name in ("TP", "SL", "CANCELADA", "EXPIRADA"):
                for k in ("entry_order_id", "sl_order_id", "tp1_order_id", "tp2_order_id"):
                    if orders.get(k):
                        self.cancel_order(sym, orders[k])
        except Exception as e:
            self._notify(f"⚠️ [Binance] Error procesando evento {name} en {sym}: {e}")
