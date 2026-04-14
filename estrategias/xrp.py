# ============================================================
# ESTRATEGIA XRP/USDT
# Backtesting: KuCoin · 1min · 6-14 Apr 2026
# Resultado: 9/10 (90%) | Profit Factor: 222
# ============================================================

SYMBOL = "XRP-USDT"

# Detección de señal
VOLUME_MULTIPLIER   = 4.0   # Pico de volumen vs media
MA_PERIOD           = 31    # Velas para calcular la media
DOMINANCE_THRESHOLD = 0.88  # 88% dominancia orderbook

# RSI
RSI_PERIOD    = 14
RSI_LONG_MAX  = 70   # LONG solo si RSI < 70
RSI_SHORT_MIN = 46   # SHORT solo si RSI > 46

# Gestión de la operación
TAKE_PROFIT_PCT = 0.26  # +0.26% cierre con beneficio
STOP_LOSS_PCT   = 0.09  # -0.09% cierre con pérdida
MAX_MINUTES     = 6     # cierre máximo si no toca TP ni SL

# Horario (UTC — España CEST = UTC+2)
TRADING_HOUR_START = 12  # 14:00 Madrid
TRADING_HOUR_END   = 18  # 20:00 Madrid
