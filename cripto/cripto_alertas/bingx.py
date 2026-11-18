"""
Ejecutor automático para BingX Perpetual Futures (Swap v2) con soporte nativo para Modo Demo (VST).

- Modo Demo (VST - Virtual Simulated Token): https://open-api-vst.bingx.com
- Modo Real (USDT):                          https://open-api.bingx.com

Variables en `.env`:
  BINGX_API_KEY=...
  BINGX_SECRET=...   (o BINGX_SECRET_KEY=...)

Funciones que automatiza en cada señal y evento:
  1. Configura Margen AISLADO (`ISOLATED`) y Apalancamiento (`1x`–`3x`) del par en BingX.
  2. Abre la orden principal (`MARKET` en CONFIRMADA, `LIMIT` en PRE).
  3. Carga orden `STOP_MARKET` para el Stop Loss (100% de la posición).
  4. Divide el Take Profit en 2 órdenes `TAKE_PROFIT_MARKET`:
     - `TP1` por el 35% de la posición en `+2.0R`.
     - `TP2` por el 65% restante en el `TP` final.
  5. Al dispararse el evento `BE` (+1.5R) o `TP1` (+2.0R), cancela el Stop Loss anterior en BingX
     y coloca el nuevo Stop Loss en Breakeven (`entrada + 0.15R`) por la cantidad remanente.
  6. Al dispararse `TIEMPO` (time-stop 7 días), cierra a mercado la posición remanente y limpia órdenes abiertas.
"""
import hashlib
import hmac
import json
import math
import time
from urllib.parse import urlencode

import requests

from .alerts import load_env

DEMO_URL = "https://open-api-vst.bingx.com"
LIVE_URL = "https://open-api.bingx.com"


def to_bingx_symbol(symbol: str) -> str:
    """Convierte 'SOLUSDT' -> 'SOL-USDT'."""
    s = symbol.upper().strip().replace("/", "-")
    if "-" in s:
        return s
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    if s.endswith("VST"):
        return f"{s[:-3]}-VST"
    return s


class BingXExecutor:
    def __init__(self, demo: bool = True, api_key: str = None, api_secret: str = None,
                 dry_run: bool = False, notifier=None):
        env = load_env()
        self.demo = demo
        self.base_url = DEMO_URL if demo else LIVE_URL
        self.unit = "VST" if demo else "USDT"
        self.api_key = api_key or env.get("BINGX_API_KEY") or env.get("BINGX_KEY") or ""
        self.api_secret = (api_secret or env.get("BINGX_SECRET")
                           or env.get("BINGX_SECRET_KEY") or env.get("BINGX_API_SECRET") or "")
        self.dry_run = dry_run or not (self.api_key and self.api_secret)
        self.notifier = notifier
        self.session = requests.Session()
        self._contracts = {}
        self._dual_side = True  # BingX por defecto usa Hedge Mode (LONG / SHORT)
        self._initialized_symbols = set()

        if not self.api_key or not self.api_secret:
            print(f"[bingx] ⚠️ BINGX_API_KEY / BINGX_SECRET no encontrados en .env -> "
                  f"ejecutando en modo SIMULACIÓN LOCAL ({self.unit}).")
        else:
            mode_str = "DEMO (VST)" if self.demo else "REAL (USDT)"
            print(f"[bingx] ✅ Ejecutor BingX activo en modo {mode_str} ({self.base_url})")
            try:
                self._dual_side = self.get_dual_side_mode()
                print(f"[bingx] Modo de posición en cuenta: {'Hedge (LONG/SHORT)' if self._dual_side else 'One-Way (BOTH)'}")
            except Exception as e:
                print(f"[bingx] Aviso al consultar modo de posición ({e}); asumiendo Hedge Mode.")

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
            headers["X-BX-APIKEY"] = self.api_key
        else:
            qs = urlencode(params)
            url = f"{self.base_url}{path}?{qs}" if qs else f"{self.base_url}{path}"

        r = self.session.request(method, url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("code", 0) not in (0, "0"):
            raise RuntimeError(f"BingX API error {data.get('code')}: {data.get('msg')}")
        return data.get("data", data) if isinstance(data, dict) else data

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
            data = self._request("GET", "/openApi/swap/v2/quote/contracts", signed=False)
            for c in data or []:
                sym = c.get("symbol")
                if sym:
                    self._contracts[sym] = {
                        "symbol": sym,
                        "qty_prec": int(c.get("quantityPrecision", 4)),
                        "price_prec": int(c.get("pricePrecision", 4)),
                        "min_qty": float(c.get("tradeMinQuantity", 0.001)),
                        "min_usdt": float(c.get("tradeMinUSDT", 2.0)),
                    }
        except Exception as e:
            print(f"[bingx] Aviso al cargar contratos ({e})")
        return self._contracts

    def contract_info(self, symbol: str) -> dict:
        bx_sym = to_bingx_symbol(symbol)
        self.load_contracts()
        return self._contracts.get(bx_sym, {
            "symbol": bx_sym, "qty_prec": 2, "price_prec": 4, "min_qty": 0.01, "min_usdt": 2.0,
        })

    def round_qty(self, symbol: str, qty: float) -> float:
        info = self.contract_info(symbol)
        prec = info["qty_prec"]
        factor = 10 ** prec
        val = math.floor(qty * factor) / factor
        return round(max(val, 0.0), prec)

    def round_price(self, symbol: str, price: float) -> float:
        info = self.contract_info(symbol)
        return round(float(price), info["price_prec"])

    def get_balance(self) -> dict:
        """Devuelve el balance de Futuros Perpetuos (en VST si demo=True, USDT si demo=False)."""
        data = self._request("GET", "/openApi/swap/v2/user/balance", signed=True)
        bal = data.get("balance", data) if isinstance(data, dict) else data
        return {
            "asset": bal.get("asset", self.unit),
            "balance": float(bal.get("balance", 0.0)),
            "equity": float(bal.get("equity", 0.0)),
            "availableMargin": float(bal.get("availableMargin", 0.0)),
            "unrealizedProfit": float(bal.get("unrealizedProfit", 0.0)),
        }

    def get_dual_side_mode(self) -> bool:
        data = self._request("GET", "/openApi/swap/v1/positionSide/dual", signed=True)
        val = str(data.get("dualSidePosition", "true")).lower()
        return val == "true"

    def ensure_symbol_setup(self, symbol: str, leverage: int):
        bx_sym = to_bingx_symbol(symbol)
        key = f"{bx_sym}:{leverage}"
        if key in self._initialized_symbols or self.dry_run:
            return
        try:
            self._request("POST", "/openApi/swap/v2/trade/marginType",
                          {"symbol": bx_sym, "marginType": "ISOLATED"}, signed=True)
        except Exception:
            pass
        sides = ("LONG", "SHORT") if self._dual_side else ("BOTH",)
        for side in sides:
            try:
                self._request("POST", "/openApi/swap/v2/trade/leverage",
                              {"symbol": bx_sym, "side": side, "leverage": int(leverage)}, signed=True)
            except Exception:
                pass
        self._initialized_symbols.add(key)

    # --------------------------------------------------------- Órdenes
    def _place_order(self, params: dict) -> str:
        if self.dry_run:
            fake_id = f"SIM-{int(time.time() * 1000) % 1_000_000_000}"
            print(f"[bingx-sim] POST /openApi/swap/v2/trade/order -> {json.dumps(params)} (id={fake_id})")
            return fake_id
        data = self._request("POST", "/openApi/swap/v2/trade/order", params, signed=True)
        order = data.get("order", data) if isinstance(data, dict) else {}
        return str(order.get("orderId", ""))

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        if not order_id:
            return False
        bx_sym = to_bingx_symbol(symbol)
        if self.dry_run or str(order_id).startswith("SIM-"):
            print(f"[bingx-sim] DELETE order {bx_sym} orderId={order_id}")
            return True
        try:
            self._request("DELETE", "/openApi/swap/v2/trade/order",
                          {"symbol": bx_sym, "orderId": int(order_id)}, signed=True)
            return True
        except Exception as e:
            print(f"[bingx] Aviso al cancelar orden {order_id} en {bx_sym}: {e}")
            return False

    # ------------------------------------------------ Ciclo de Vida de Señales
    def on_signal(self, s, tr, cfg) -> dict:
        """
        Abre la operación en BingX Demo (VST) cuando llega una señal y deja cargados SL, TP1 (35%) y TP2 (65%).
        Guarda los orderIds en `tr.exchange_orders`.
        """
        bx_sym = to_bingx_symbol(s.symbol)
        info = self.contract_info(s.symbol)
        lev = int(s.sizing.get("leverage", cfg.leverage_max))
        self.ensure_symbol_setup(s.symbol, lev)

        total_qty = self.round_qty(s.symbol, tr.qty)
        if total_qty < info["min_qty"]:
            total_qty = info["min_qty"]

        long = s.direction == "LONG"
        open_side = "BUY" if long else "SELL"
        close_side = "SELL" if long else "BUY"
        pos_side = ("LONG" if long else "SHORT") if self._dual_side else "BOTH"

        entry_px = self.round_price(s.symbol, s.entry)
        sl_px = self.round_price(s.symbol, s.sl)
        tp_px = self.round_price(s.symbol, s.tp)
        tp1_px = self.round_price(s.symbol, s.tp1_price) if s.tp1_price else None

        # Calcular partición TP1 (35%) y TP2 (65%)
        ptp_frac = getattr(cfg, "partial_tp_frac", 0.35) or 0.0
        tp1_qty = self.round_qty(s.symbol, total_qty * ptp_frac) if (tp1_px and ptp_frac > 0) else 0.0
        tp2_qty = self.round_qty(s.symbol, total_qty - tp1_qty)
        if tp1_qty < info["min_qty"] or tp2_qty < info["min_qty"]:
            tp1_qty = 0.0
            tp2_qty = total_qty

        orders = {
            "symbol": bx_sym,
            "unit": self.unit,
            "demo": self.demo,
            "dry_run": self.dry_run,
            "total_qty": total_qty,
            "tp1_qty": tp1_qty,
            "tp2_qty": tp2_qty,
        }

        try:
            # 1. Orden de entrada (MARKET en CONFIRMADA, LIMIT en PRE)
            entry_params = {
                "symbol": bx_sym,
                "side": open_side,
                "positionSide": pos_side,
                "type": "MARKET" if s.entry_type == "MERCADO" else "LIMIT",
                "quantity": total_qty,
            }
            if s.entry_type != "MERCADO":
                entry_params["price"] = entry_px
                entry_params["timeInForce"] = "GTC"
            orders["entry_order_id"] = self._place_order(entry_params)

            # 2. Orden de Stop Loss por el 100% de la cantidad
            sl_params = {
                "symbol": bx_sym,
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

            # 3. Orden TP1 parcial (35% en +2.0R)
            if tp1_qty > 0 and tp1_px:
                tp1_params = {
                    "symbol": bx_sym,
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

            # 4. Orden TP2 final (65% restante)
            tp2_params = {
                "symbol": bx_sym,
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
            tag = f"BINGX {'DEMO-SIM' if self.dry_run else ('DEMO-VST' if self.demo else 'LIVE')}"
            msg = (
                f"🤖 [{tag}] Orden ejecutada en {bx_sym} ({s.direction} {lev}x Aislado)\n"
                f"  · Entrada ({entry_params['type']}): {entry_px} | Qty: {total_qty} (ID: {orders.get('entry_order_id')})\n"
                f"  · SL cargado: {sl_px} por {total_qty} (ID: {orders.get('sl_order_id')})\n"
                + (f"  · TP1 parcial (35%): {tp1_px} por {tp1_qty} (ID: {orders.get('tp1_order_id')})\n" if tp1_qty > 0 else "")
                + f"  · TP final: {tp_px} por {tp2_qty} (ID: {orders.get('tp2_order_id')})"
            )
            self._notify(msg)
        except Exception as e:
            self._notify(f"⚠️ [BingX] Error al enviar órdenes para {bx_sym}: {e}")
        return orders

    def on_event(self, tr, event, cfg):
        """
        Sincroniza con BingX los eventos de seguimiento:
        - `BE` / `TP1`: actualiza el Stop Loss en BingX al precio de Breakeven y ajusta la cantidad remanente.
        - `TIEMPO`: cierra a mercado el remanente y cancela SL/TP pendientes.
        - `TP` / `SL` / `CANCELADA` / `EXPIRADA`: limpia órdenes huérfanas.
        """
        name, ts, price = event
        orders = getattr(tr, "exchange_orders", None) or {}
        if not orders:
            return
        bx_sym = orders.get("symbol") or to_bingx_symbol(cfg.symbol)
        long = tr.direction == "LONG"
        close_side = "SELL" if long else "BUY"
        pos_side = ("LONG" if long else "SHORT") if self._dual_side else "BOTH"
        tag = f"BINGX {'DEMO-SIM' if self.dry_run else ('DEMO-VST' if self.demo else 'LIVE')}"

        try:
            if name in ("BE", "TP1"):
                # Cancelar SL anterior y colocar nuevo SL con tr.sl actual y cantidad remanente
                old_sl_id = orders.get("sl_order_id")
                if old_sl_id:
                    self.cancel_order(bx_sym, old_sl_id)
                rem_qty = orders.get("tp2_qty", orders.get("total_qty", tr.qty)) if tr.partial_done else orders.get("total_qty", tr.qty)
                rem_qty = self.round_qty(bx_sym, rem_qty)
                new_sl_px = self.round_price(bx_sym, tr.sl)
                sl_params = {
                    "symbol": bx_sym,
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
                self._notify(f"🤖 [{tag}] SL actualizado en {bx_sym} ({name}) -> {new_sl_px} por qty {rem_qty} (ID: {new_id})")

            elif name == "TIEMPO":
                for k in ("sl_order_id", "tp1_order_id", "tp2_order_id"):
                    if orders.get(k):
                        self.cancel_order(bx_sym, orders[k])
                rem_qty = orders.get("tp2_qty", orders.get("total_qty", tr.qty)) if tr.partial_done else orders.get("total_qty", tr.qty)
                rem_qty = self.round_qty(bx_sym, rem_qty)
                close_params = {
                    "symbol": bx_sym,
                    "side": close_side,
                    "positionSide": pos_side,
                    "type": "MARKET",
                    "quantity": rem_qty,
                }
                if not self._dual_side:
                    close_params["reduceOnly"] = "true"
                cid = self._place_order(close_params)
                self._notify(f"🤖 [{tag}] Time-stop ejecutado a mercado en {bx_sym} por qty {rem_qty} (ID: {cid})")

            elif name in ("TP", "SL", "CANCELADA", "EXPIRADA"):
                for k in ("entry_order_id", "sl_order_id", "tp1_order_id", "tp2_order_id"):
                    if orders.get(k):
                        self.cancel_order(bx_sym, orders[k])
        except Exception as e:
            self._notify(f"⚠️ [BingX] Error procesando evento {name} en {bx_sym}: {e}")
