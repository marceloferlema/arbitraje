"""
Configuración global de la estrategia Golden POC + Clímax de Volumen + Filtro R:R 2:1.
Carga credenciales de Telegram desde .env (raíz del repositorio o carpeta local).
"""
from dataclasses import dataclass, field, replace
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CRIPTO_DIR = os.path.dirname(HERE)
ROOT_DIR = os.path.dirname(CRIPTO_DIR)


def _load_env():
    env = {}
    for base in (ROOT_DIR, CRIPTO_DIR, HERE):
        p = os.path.join(base, ".env")
        if os.path.exists(p):
            try:
                from dotenv import dotenv_values
                env.update({k: v for k, v in dotenv_values(p).items() if v is not None})
            except Exception:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_chat_ID", "CHAT_ID"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    return env


_ENV = _load_env()


@dataclass
class Config:
    # Universo y temporalidades por defecto
    symbols: list = field(default_factory=lambda: ["SOL/USDT", "BTC/USDT", "ETH/USDT"])
    timeframes: list = field(default_factory=lambda: ["1h", "1d"])

    # 1. Perfil de Volumen (Golden POC y Resistencia)
    vp_lookback: int = 200
    vp_rows: int = 36
    poc_zone_width: int = 1
    floor_pct: float = 0.25          # low <= lowest_low + range_span * 0.25

    # 2. Filtro de Señales (Clímax de volumen + Mecha + Squeeze + Cooldown)
    vol_len: int = 20
    vol_mult: float = 1.5
    vol_lookback_bars: int = 3       # vela actual o dentro de las últimas 3 velas
    wick_min_ratio: float = 0.45     # (close - low) / (high - low) >= 0.45 o close > open
    sqz_len: int = 20
    signal_cooldown: int = 25

    # 3. Niveles Operativos y R:R
    min_rr: float = 2.0
    sl_buffer_pct: float = 0.8       # 0.8% debajo de poc_low
    tp_safety_pct: float = 94.0      # 94% del recorrido hacia res_low
    tight_sl_thr: float = 3.5        # umbral de SL ajustado (%)

    # 4. Gestión de Capital y Apalancamiento
    capital_total: float = 1000.0
    base_alloc_pct: float = 3.0      # 3% si risk_pct > 3.5%
    boost_alloc_pct: float = 5.0     # 5% si risk_pct <= 3.5%
    leverage: int = 3

    # 5. Backtest
    fee_pct: float = 0.04            # 0.04% por lado (entrada y salida)
    max_hold_bars: int = 120         # 120 velas (5 días en 1H)
    limit_valid_bars: int = 48       # validez de la orden limit en modo limit_poc

    # Rutas y Telegram
    data_dir: str = os.path.join(HERE, "data")
    shared_binance_dir: str = os.path.join(CRIPTO_DIR, "data", "binance")
    output_dir: str = os.path.join(HERE, "output")
    sent_alerts_file: str = os.path.join(HERE, "sent_alerts.json")
    telegram_bot_token: str = (_ENV.get("CRIPTO_TELEGRAM_TOKEN") or _ENV.get("TELEGRAM_BOT_TOKEN")
                               or _ENV.get("TELEGRAM_TOKEN", ""))
    telegram_chat_id: str = (_ENV.get("CRIPTO_CHAT_ID") or _ENV.get("TELEGRAM_CHAT_ID")
                             or _ENV.get("TELEGRAM_chat_ID") or _ENV.get("CHAT_ID", ""))

    def override(self, **kwargs):
        clean = {k: v for k, v in kwargs.items() if v is not None and hasattr(self, k)}
        return replace(self, **clean)


DEFAULT_CONFIG = Config()
