"""
Cliente HTTP hacia la API de Telegram con formato HTML, prueba de conexión
y control anti-spam persistente en `sent_alerts.json`.
"""
import json
import os
import requests

from config import Config, DEFAULT_CONFIG
from strategy_engine import EvaluationResult


def _p(val: float) -> str:
    return f"{val:,.4f}" if abs(val) < 10 else f"{val:,.2f}"


class TelegramNotifier:
    def __init__(self, cfg: Config = DEFAULT_CONFIG):
        self.cfg = cfg
        self.token = cfg.telegram_bot_token
        self.chat_id = cfg.telegram_chat_id
        self.sent_file = cfg.sent_alerts_file
        self.sent = self._load_sent()

    def _load_sent(self) -> set[str]:
        if os.path.exists(self.sent_file):
            try:
                with open(self.sent_file, "r", encoding="utf-8") as f:
                    return set(json.load(f))
            except Exception:
                return set()
        return set()

    def _save_sent(self):
        try:
            with open(self.sent_file, "w", encoding="utf-8") as f:
                json.dump(sorted(self.sent), f, indent=2)
        except Exception as e:
            print(f"[telegram] error guardando sent_alerts.json: {e}")

    def alert_key(self, res: EvaluationResult) -> str:
        return f"{res.symbol}_{res.timeframe}_{pd.Timestamp(res.timestamp).isoformat()}"

    def already_sent(self, key: str) -> bool:
        return key in self.sent

    def mark_sent(self, key: str):
        self.sent.add(key)
        self._save_sent()

    def format_html(self, res: EvaluationResult) -> str:
        if res.trigger_signal and res.in_buy_zone:
            semaforo = "🟢 ENTRAR LONG EN ZONA POC (REBOTE OBLIGADO CONFIRMADO)"
        elif res.trigger_signal:
            semaforo = "🟠 ORDEN LIMIT LISTA EN POC + PICOTAZO CONFIRMADO"
        else:
            semaforo = "🟢 ENTRAR LONG EN ZONA POC"

        return (
            f"<b>{semaforo}</b>\n"
            f"<b>Par:</b> {res.symbol} ({res.timeframe.upper()}) | <b>Vela:</b> {res.timestamp:%Y-%m-%d %H:%M UTC}\n"
            f"<b>Precio Actual:</b> ${_p(res.close)} USD\n\n"
            f"📊 <b>Zonas de Volumen ({self.cfg.vp_lookback} velas):</b>\n"
            f"• <b>Zona Golden POC:</b> ${_p(res.poc_low)} – ${_p(res.poc_high)} (Centro: ${_p(res.poc_mid)})\n"
            f"• <b>Zona de Resistencia:</b> ${_p(res.res_low)} – ${_p(res.res_high)}\n\n"
            f"🎯 <b>Niveles Operativos:</b>\n"
            f"• <b>Estado:</b> {res.action_state}\n"
            f"• <b>Entrada:</b> ${_p(res.entry_price)} USD\n"
            f"• <b>Take Profit Seguro (100%):</b> ${_p(res.tp_price)} USD (+{res.reward_pct:.2f}%)\n"
            f"• <b>Stop Loss Ajustado:</b> ${_p(res.sl_price)} USD (-{res.risk_pct:.2f}%)\n"
            f"• <b>Ratio Riesgo/Beneficio:</b> <b>{res.rr_ratio:.2f} : 1</b> (APROBADO &gt;= {self.cfg.min_rr:.1f}:1)\n\n"
            f"💰 <b>Gestión de Capital ({res.alloc_pct:.1f}% a {self.cfg.leverage}x):</b>\n"
            f"• <b>Margen a colocar:</b> ${res.margin_usd:.2f} USD (Posición total: ${res.position_usd:.2f} USD)\n"
            f"• <b>Ganancia estimada:</b> +${res.est_win_usd:.2f} USD | <b>Pérdida máx:</b> -${res.est_loss_usd:.2f} USD"
        )

    def send_message(self, html_text: str) -> bool:
        if not self.token or not self.chat_id:
            print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_chat_ID no configurados en .env (modo consola).")
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": html_text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
            if not r.ok:
                print(f"[telegram] HTTP {r.status_code}: {r.text}")
                return False
            return True
        except Exception as e:
            print(f"[telegram] Error de red: {e}")
            return False

    def send_test(self) -> bool:
        msg = (
            "<b>✅ Prueba de Conexión Exitosa — Bot Golden POC &amp; R:R 2:1</b>\n"
            "El escáner en vivo está conectado correctamente a Telegram."
        )
        return self.send_message(msg)


import pandas as pd  # noqa: E402
