# ============================================================
# ESTRATEGIA SOL/USDT
# Backtesting: KuCoin · 1min · 6-14 Apr 2026
# Resultado: 20/22 (90.91%) | Profit Factor: 90
# ============================================================

SYMBOL = "SOL-USDT"

# Detección de señal
VOLUME_MULTIPLIER   = 4.5   # Pico de volumen vs media
MA_PERIOD           = 26    # Velas para calcular la media
DOMINANCE_THRESHOLD = 0.72  # 72% dominancia orderbook

# RSI
RSI_PERIOD    = 6
RSI_LONG_MAX  = 66   # LONG solo si RSI < 66
RSI_SHORT_MIN = 42   # SHORT solo si RSI > 42

# Gestión de la operación
TAKE_PROFIT_PCT = 0.25  # +0.25% cierre con beneficio
STOP_LOSS_PCT   = 0.25  # -0.25% cierre con pérdida
MAX_MINUTES     = 4     # cierre máximo si no toca TP ni SL

# Horario (UTC)
TRADING_HOUR_START = 8   # 10:00 Madrid
TRADING_HOUR_END   = 19  # 21:00 Madrid
