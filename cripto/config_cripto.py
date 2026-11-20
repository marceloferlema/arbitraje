"""
Configuración de la estrategia de alertas cripto
("Barrido de liquidez sobre zonas de Volume Profile").

Todos los parámetros se pueden sobreescribir desde la CLI de run_live.py / run_backtest.py.
"""
from dataclasses import dataclass, field, asdict, replace
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Settings:
    # ---------------- Universo ----------------
    symbol: str = "SOLUSDT"
    btc_symbol: str = "BTCUSDT"
    universe: tuple = ("SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "BTCUSDT")

    # ---------------- Volume Profile ----------------
    profile_lookback_days: int = 540      # ~18 meses de velas 1H para construir el perfil
    profile_min_days: int = 120           # mínimo de historia para considerar el perfil válido
    profile_bin_pct: float = 0.004        # ancho de cada bin (0.4 % del precio, bins logarítmicos)
    profile_smooth_bins: int = 5          # suavizado gaussiano del histograma
    hvn_percentile: float = 70.0          # un pico es HVN si supera este percentil del histograma
    zone_expand: float = 0.60             # la franja se extiende mientras el volumen >= 60 % del pico
    zone_max_width_pct: float = 0.08      # ancho máximo de una franja (8 %)
    zone_min_share: float = 0.01          # volumen mínimo de la franja (1 % del total)
    value_area_pct: float = 0.70

    # ---------------- Estructura diaria ----------------
    pivot_len_1d: int = 3
    range_lookback_days: int = 30
    range_max_atr: float = 7.0            # si el box de 30 días mide <= 7 ATR diarios -> RANGO
    accdist_threshold: float = 0.10

    # ---------------- Estructura 1H / barrido ----------------
    pivot_len_1h: int = 6                 # fractal de 6 velas a cada lado (captura swings intradía)
    impulse_min_pct: float = 0.04         # el mínimo debe haber originado un impulso >= 4 %
    pivot_search_days: int = 45           # antigüedad máxima del último mín/máx relevante
    sweep_bars: int = 4                   # ventana (velas 1H) para barrido + reclaim
    level_zone_tol_pct: float = 0.03      # el nivel barrido puede estar hasta 3 % fuera de la franja

    # ---------------- Volumen 1H y momentum ----------------
    vol_window_bars: int = 720            # 30 días de velas 1H para comparar volumen
    vol_percentile_min: float = 0.90      # percentil mínimo del volumen en la vela de barrido
    squeeze_confirm: bool = False         # exigir Squeeze Momentum girando desde extremo
    wick_reject_min: float = 0.55         # cierre en el >=55% superior/inferior de la vela (o vela a favor)
    delta_confirm: bool = True            # exigir absorción de taker_buy_base en la ventana de barrido
    btc_filter: str = "strict"            # strict / soft / off
    btc_recent_bars: int = 6
    btc_ref_bars: int = 168
    no_counter_bias: bool = True          # descartar señales EN CONTRA del sesgo 1D

    # ---------------- Entradas / salidas ----------------
    approach_pct: float = 0.03            # pre-alerta si el precio está a <= 3 % de la franja
    entry_offset_pct: float = 0.0015      # limit apenas arriba del piso (long) / abajo del techo (short)
    sl_buffer_atr_d: float = 0.35         # SL = borde de la franja/mecha -/+ 0.35 ATR diario
    tp_buffer_pct: float = 0.003          # TP un poquito antes de la zona objetivo
    min_tp_pct: float = 0.01
    rr_min: float = 2.0
    min_score: float = 65.0
    breakeven_r: float = 0.0              # Trailing Stop activo desde +2.5R (evita cortes prematuros en BE)
    breakeven_offset_r: float = 0.15      # colchón sobre entrada si se activa BE manualmente
    partial_tp_r: float = 0.0             # desactivado: deja correr el 100% de los ganadores
    partial_tp_frac: float = 0.0
    trail_start_r: float = 2.5            # activar trailing stop al alcanzar +2.5R
    trail_distance_r: float = 1.50        # distancia del trailing stop (1.5R)
    pre_valid_hours: int = 72             # validez de la orden limit de una pre-alerta
    max_hold_days: int = 7                # time-stop
    zone_cooldown_hours: int = 24

    # ---------------- Capitulación Extrema (Sniper Contra-Tendencia) ----------------
    capitulation_enabled: bool = True         # detectar pisos de pánico extremo (LONG táctico 2:1)
    capitulation_ema_dist_pct: float = -0.10  # precio <= -10 % por debajo de EMA 200 (1H)
    capitulation_rsi_max: float = 25.0        # RSI 1H <= 25 en las últimas 4 velas
    capitulation_vol_pct: float = 0.95        # volumen en percentil >= 95 %
    capitulation_wick_min: float = 0.75       # cierre en el >= 75 % superior de la vela
    capitulation_tb_min: float = 0.53         # compras agresivas (taker_buy_base) >= 53 %
    capitulation_rr: float = 2.0              # objetivo táctico de rebote (R:R 2:1)
    capitulation_cooldown_hours: int = 48     # enfriamiento entre señales de capitulación

    # ---------------- Riesgo / sizing (Perfil Conservador B: Max DD < 10%) ----------------
    capital_usd: float = 1000.0
    margin_pct_min: float = 0.08
    margin_pct_max: float = 0.20
    leverage_max: int = 5
    max_risk_pct: float = 0.025           # pérdida máxima por trade (2.5 % del capital)
    max_portfolio_positions: int = 3      # escudo global: máx 3 posiciones simultáneas entre los 5 pares
    sl_tight_pct: float = 0.02            # SL <= 2 % -> margen máximo
    sl_wide_pct: float = 0.08             # SL >= 8 % -> margen mínimo

    # ---------------- Backtest ----------------
    fee_maker: float = 0.0002
    fee_taker: float = 0.0005
    slippage_pct: float = 0.0005          # en stops y entradas a mercado
    compound: bool = True
    max_open_positions: int = 1
    intrabar_resolution: bool = True      # resolver ambigüedades con velas 1m (on-demand)

    # ---------------- Rutas ----------------
    data_dir: str = os.path.join(BASE_DIR, "data")
    reports_dir: str = os.path.join(BASE_DIR, "reports")
    alerts_dir: str = os.path.join(BASE_DIR, "alerts_log")

    def to_dict(self):
        return asdict(self)

    def override(self, **kwargs):
        clean = {k: v for k, v in kwargs.items() if v is not None and hasattr(self, k)}
        return replace(self, **clean)


DEFAULT = Settings()
