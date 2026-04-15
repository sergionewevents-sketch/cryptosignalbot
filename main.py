import os
import time
import requests
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta

# Cargar estrategias
from estrategias.xrp import (
    SYMBOL as XRP_SYMBOL,
    VOLUME_MULTIPLIER as XRP_VOL, MA_PERIOD as XRP_MA,
    DOMINANCE_THRESHOLD as XRP_DOM,
    RSI_PERIOD as XRP_RSI_P, RSI_LONG_MAX as XRP_RSI_L,
    RSI_SHORT_MIN as XRP_RSI_S,
    TAKE_PROFIT_PCT as XRP_TP, STOP_LOSS_PCT as XRP_SL,
    MAX_MINUTES as XRP_MAX, TRADING_HOUR_START as XRP_HS,
    TRADING_HOUR_END as XRP_HE,
)
from estrategias.sol import (
    SYMBOL as SOL_SYMBOL,
    VOLUME_MULTIPLIER as SOL_VOL, MA_PERIOD as SOL_MA,
    DOMINANCE_THRESHOLD as SOL_DOM,
    RSI_PERIOD as SOL_RSI_P, RSI_LONG_MAX as SOL_RSI_L,
    RSI_SHORT_MIN as SOL_RSI_S,
    TAKE_PROFIT_PCT as SOL_TP, STOP_LOSS_PCT as SOL_SL,
    MAX_MINUTES as SOL_MAX, TRADING_HOUR_START as SOL_HS,
    TRADING_HOUR_END as SOL_HE,
)

# ============================================================
# CONFIGURACIÓN GLOBAL
# ============================================================
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")
POLL_INTERVAL     = int(os.environ.get("POLL_INTERVAL", "5"))
DAILY_REPORT_HOUR = int(os.environ.get("DAILY_REPORT_HOUR", "23"))
COOLDOWN_MINUTES  = int(os.environ.get("COOLDOWN_MINUTES", "30"))

STRATEGIES = [
    {
        "symbol":             XRP_SYMBOL,
        "volume_multiplier":  XRP_VOL,
        "ma_period":          XRP_MA,
        "dominance_threshold":XRP_DOM,
        "rsi_period":         XRP_RSI_P,
        "rsi_long_max":       XRP_RSI_L,
        "rsi_short_min":      XRP_RSI_S,
        "take_profit_pct":    XRP_TP,
        "stop_loss_pct":      XRP_SL,
        "max_minutes":        XRP_MAX,
        "hour_start":         XRP_HS,
        "hour_end":           XRP_HE,
    },
    {
        "symbol":             SOL_SYMBOL,
        "volume_multiplier":  SOL_VOL,
        "ma_period":          SOL_MA,
        "dominance_threshold":SOL_DOM,
        "rsi_period":         SOL_RSI_P,
        "rsi_long_max":       SOL_RSI_L,
        "rsi_short_min":      SOL_RSI_S,
        "take_profit_pct":    SOL_TP,
        "stop_loss_pct":      SOL_SL,
        "max_minutes":        SOL_MAX,
        "hour_start":         SOL_HS,
        "hour_end":           SOL_HE,
    },
]

KUCOIN_BASE = "https://api.kucoin.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# ESTADO
# ============================================================
last_signal_time = {}
pending_signals  = []

daily_stats  = {"total": 0, "win": 0, "loss": 0, "pnl": 0.0, "tp": 0, "sl": 0, "time": 0}
weekly_stats = {"total": 0, "win": 0, "loss": 0, "pnl": 0.0, "tp": 0, "sl": 0, "time": 0}
last_daily_report  = None
last_weekly_report = None

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Telegram exception: {e}")

# ============================================================
# KUCOIN API
# ============================================================
def get_klines(symbol: str, limit: int = 50):
    try:
        url = f"{KUCOIN_BASE}/api/v1/market/candles"
        params = {"symbol": symbol, "type": "1min"}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "200000":
                return data["data"][:limit]
            else:
                log.error(f"KuCoin error {symbol}: {data.get('msg')}")
                return []
        elif r.status_code == 429:
            log.warning(f"KuCoin rate limit {symbol}, esperando 5s...")
            time.sleep(5)
            return []
        else:
            log.error(f"KuCoin HTTP error {symbol}: {r.status_code}")
            return []
    except Exception as e:
        log.error(f"KuCoin exception {symbol}: {e}")
        return []

def get_current_price(symbol: str):
    try:
        url = f"{KUCOIN_BASE}/api/v1/market/orderbook/level1"
        r = requests.get(url, params={"symbol": symbol}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "200000":
                return float(data["data"]["price"])
        return None
    except Exception as e:
        log.error(f"KuCoin price exception {symbol}: {e}")
        return None

def parse_klines(klines: list):
    """
    KuCoin formato: [time, open, close, high, low, volume, turnover]
    Viene en orden DESCENDENTE — invertimos para orden cronológico.
    """
    result = []
    for k in reversed(klines):
        result.append({
            "open":   float(k[1]),
            "close":  float(k[2]),
            "high":   float(k[3]),
            "low":    float(k[4]),
            "volume": float(k[5]),
        })
    return result

# ============================================================
# RSI — igual que TradingView ta.rsi()
# ============================================================
def calculate_rsi(klines: list, period: int) -> float:
    if len(klines) < period + 1:
        return 50.0
    closes = [k["close"] for k in klines[-(period + 1):]]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# ============================================================
# DOMINANCIA — igual que TradingView (cuerpo de vela)
# body_ratio = abs(close - open) / (high - low) * 100
# ============================================================
def calculate_dominance(candle: dict):
    """
    Misma lógica que Pine Script:
    candle_range = high - low
    body_ratio = abs(close - open) / candle_range * 100
    bull = close > open and body_ratio >= dominance_pct
    bear = close < open and body_ratio >= dominance_pct
    """
    candle_range = candle["high"] - candle["low"]
    if candle_range == 0:
        return None, 0.0
    body = abs(candle["close"] - candle["open"])
    body_ratio = body / candle_range * 100

    if candle["close"] > candle["open"]:
        direction = "LONG"
    else:
        direction = "SHORT"

    return direction, round(body_ratio, 1)

# ============================================================
# HORARIO
# ============================================================
def is_trading_hours(hour_start: int, hour_end: int) -> bool:
    now_utc = datetime.now(timezone.utc)
    return hour_start <= now_utc.hour < hour_end

# ============================================================
# COOLDOWN
# ============================================================
def is_in_cooldown(symbol: str) -> bool:
    if symbol not in last_signal_time:
        return False
    elapsed = (datetime.now(timezone.utc) - last_signal_time[symbol]).total_seconds()
    return elapsed < COOLDOWN_MINUTES * 60

# ============================================================
# LÓGICA DE DETECCIÓN — igual que TradingView
# ============================================================
def check_strategy(strat: dict):
    symbol = strat["symbol"]

    if not is_trading_hours(strat["hour_start"], strat["hour_end"]):
        return None

    if is_in_cooldown(symbol):
        return None

    limit = max(strat["rsi_period"] + 2, strat["ma_period"] + 2)
    klines_raw = get_klines(symbol, limit=limit)
    if len(klines_raw) < strat["ma_period"] + 1:
        return None

    klines = parse_klines(klines_raw)
    current = klines[-1]
    history = klines[:-1]

    # Volumen anómalo — igual que TradingView ta.sma(volume, ma_period)
    avg_volume = sum(k["volume"] for k in history[-strat["ma_period"]:]) / strat["ma_period"]
    if avg_volume == 0:
        return None

    vol_ratio = current["volume"] / avg_volume
    if vol_ratio < strat["volume_multiplier"]:
        return None

    # Dominancia por cuerpo de vela — igual que TradingView
    direction, body_ratio = calculate_dominance(current)
    if direction is None:
        return None

    if body_ratio < strat["dominance_threshold"]:
        return None

    # RSI — igual que TradingView ta.rsi()
    rsi = calculate_rsi(klines, period=strat["rsi_period"])

    if direction == "LONG" and rsi >= strat["rsi_long_max"]:
        log.info(f"{symbol} LONG descartada — RSI alto: {rsi}")
        return None
    if direction == "SHORT" and rsi <= strat["rsi_short_min"]:
        log.info(f"{symbol} SHORT descartada — RSI bajo: {rsi}")
        return None

    return {
        "symbol":        symbol,
        "direction":     direction,
        "price":         current["close"],
        "vol_ratio":     round(vol_ratio, 2),
        "dominance_pct": body_ratio,
        "rsi":           rsi,
        "strat":         strat,
    }

# ============================================================
# FORMATEO DE MENSAJES
# ============================================================
def format_signal(signal: dict) -> str:
    symbol_name = signal["symbol"].replace("-USDT", "")
    emoji     = "🟢" if signal["direction"] == "LONG" else "🔴"
    action    = "LONG  📈" if signal["direction"] == "LONG" else "SHORT 📉"
    dom_label = "💚 Dominancia compradora" if signal["direction"] == "LONG" else "🔴 Dominancia vendedora"
    s = signal["strat"]
    return (
        f"{emoji} <b>SEÑAL {action} — {symbol_name}/USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Precio entrada: <b>${signal['price']:,.4f}</b>\n"
        f"📊 Volumen: <b>{signal['vol_ratio']}x</b> sobre la media\n"
        f"{dom_label}: <b>{signal['dominance_pct']}%</b>\n"
        f"📉 RSI: <b>{signal['rsi']}</b>\n"
        f"🎯 TP: <b>+{s['take_profit_pct']}%</b> | 🛑 SL: <b>-{s['stop_loss_pct']}%</b>\n"
        f"⏱️ Cierre máx: <b>{s['max_minutes']} min</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def format_resolution(signal: dict, close_price: float, close_reason: str) -> str:
    symbol_name = signal["symbol"].replace("-USDT", "")
    entry = signal["entry_price"]
    pct_change = ((close_price - entry) / entry) * 100
    pnl_pct = pct_change if signal["direction"] == "LONG" else -pct_change
    pnl_eur = round(pnl_pct / 100, 4)
    result_emoji = "✅" if pnl_pct > 0 else "❌"
    result_label = "GANADA" if pnl_pct > 0 else "PERDIDA"
    sign = "+" if pnl_eur >= 0 else ""
    reason_labels = {"TP": "🎯 Take Profit", "SL": "🛑 Stop Loss", "TIME": "⏱️ Tiempo máximo"}
    reason_str = reason_labels.get(close_reason, close_reason)
    return (
        f"{result_emoji} <b>CIERRE — {symbol_name} {signal['direction']} — {result_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📥 Entrada: ${entry:,.4f}\n"
        f"📤 Cierre:  ${close_price:,.4f}\n"
        f"📊 Movimiento: {pct_change:+.2f}%\n"
        f"💶 P&L (1€): {sign}{pnl_eur:.4f}€\n"
        f"🔖 Motivo: {reason_str}\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_stats(stats: dict, title: str) -> str:
    if stats["total"] == 0:
        return f"📊 <b>{title}</b>\n\nNo hubo señales."
    wr = round((stats["win"] / stats["total"]) * 100, 1)
    pnl = round(stats["pnl"], 4)
    pnl_str = f"+{pnl}€" if pnl >= 0 else f"{pnl}€"
    date_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    return (
        f"📊 <b>{title} — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📨 Señales: <b>{stats['total']}</b>\n"
        f"✅ Ganadas: <b>{stats['win']}</b> ({wr}%)\n"
        f"❌ Perdidas: <b>{stats['loss']}</b>\n"
        f"🎯 Por TP: <b>{stats['tp']}</b>\n"
        f"🛑 Por SL: <b>{stats['sl']}</b>\n"
        f"⏱️ Por tiempo: <b>{stats['time']}</b>\n"
        f"💶 P&L total: <b>{pnl_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

# ============================================================
# RESOLUCIÓN DE SEÑALES (TP / SL / Tiempo)
# ============================================================
def resolve_pending_signals():
    now = datetime.now(timezone.utc)
    for signal in pending_signals:
        if signal["resolved"]:
            continue

        elapsed = (now - signal["entry_time"]).total_seconds()
        strat = signal["strat"]

        close_price = get_current_price(signal["symbol"])
        if close_price is None:
            continue

        entry = signal["entry_price"]
        pct_change = ((close_price - entry) / entry) * 100
        pnl_pct = pct_change if signal["direction"] == "LONG" else -pct_change

        close_reason = None
        if pnl_pct >= strat["take_profit_pct"]:
            close_reason = "TP"
        elif pnl_pct <= -strat["stop_loss_pct"]:
            close_reason = "SL"
        elif elapsed >= strat["max_minutes"] * 60:
            close_reason = "TIME"

        if close_reason is None:
            continue

        signal["resolved"] = True
        pnl_eur = round(pnl_pct / 100, 4)

        for stats in [daily_stats, weekly_stats]:
            stats["total"] += 1
            stats["pnl"] = round(stats["pnl"] + pnl_eur, 4)
            if pnl_eur > 0:
                stats["win"] += 1
            else:
                stats["loss"] += 1
            if close_reason == "TP":
                stats["tp"] += 1
            elif close_reason == "SL":
                stats["sl"] += 1
            else:
                stats["time"] += 1

        send_telegram(format_resolution(signal, close_price, close_reason))
        log.info(f"Cierre {close_reason}: {signal['symbol']} {signal['direction']} → {pnl_eur:+.4f}€")

# ============================================================
# RESUMEN DIARIO Y SEMANAL
# ============================================================
def check_daily_report():
    global last_daily_report, daily_stats
    now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)
    today = now_madrid.date()
    if last_daily_report == today or now_madrid.hour != DAILY_REPORT_HOUR:
        return
    last_daily_report = today
    send_telegram(format_stats(daily_stats, "RESUMEN DIARIO"))
    log.info("Resumen diario enviado")
    daily_stats = {"total": 0, "win": 0, "loss": 0, "pnl": 0.0, "tp": 0, "sl": 0, "time": 0}

def check_weekly_report():
    global last_weekly_report, weekly_stats
    now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)
    if now_madrid.weekday() != 6 or now_madrid.hour != DAILY_REPORT_HOUR:
        return
    today = now_madrid.date()
    if last_weekly_report == today:
        return
    last_weekly_report = today
    send_telegram(format_stats(weekly_stats, "RESUMEN SEMANAL"))
    log.info("Resumen semanal enviado")
    weekly_stats = {"total": 0, "win": 0, "loss": 0, "pnl": 0.0, "tp": 0, "sl": 0, "time": 0}

# ============================================================
# SERVIDOR HTTP (requerido por Fly.io)
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(("0.0.0.0", 8080), HealthHandler)
    server.serve_forever()

# ============================================================
# BUCLE PRINCIPAL
# ============================================================
def main():
    symbols = [s["symbol"].replace("-USDT", "") for s in STRATEGIES]
    log.info(f"🚀 CryptoSignalBot arrancado! Estrategias: {', '.join(symbols)}")
    send_telegram(
        f"🚀 <b>CryptoSignalBot activado</b>\n"
        f"📊 Estrategias activas: <b>{', '.join(symbols)}</b>\n"
        f"⏱️ Consulta cada {POLL_INTERVAL}s | Cooldown: {COOLDOWN_MINUTES}min\n"
        f"📐 Lógica: cuerpo de vela (= TradingView)\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

    cycle = 0
    while True:
        try:
            cycle += 1
            signals_this_cycle = 0

            for strat in STRATEGIES:
                signal = check_strategy(strat)
                if signal is None:
                    continue

                last_signal_time[strat["symbol"]] = datetime.now(timezone.utc)
                signals_this_cycle += 1
                send_telegram(format_signal(signal))

                pending_signals.append({
                    "symbol":      strat["symbol"],
                    "direction":   signal["direction"],
                    "entry_price": signal["price"],
                    "entry_time":  datetime.now(timezone.utc),
                    "strat":       strat,
                    "resolved":    False,
                })

                log.info(
                    f"Señal: {strat['symbol']} {signal['direction']} | "
                    f"{signal['vol_ratio']}x vol | {signal['dominance_pct']}% dom | RSI {signal['rsi']}"
                )
                time.sleep(1.0)

            resolve_pending_signals()
            pending_signals[:] = [s for s in pending_signals if not s["resolved"]]
            check_daily_report()
            check_weekly_report()

            if signals_this_cycle > 0:
                log.info(f"Ciclo {cycle} — {signals_this_cycle} señal(es)")
            elif cycle % 120 == 0:
                log.info(f"Ciclo {cycle} — Sin señales | Pendientes: {len(pending_signals)}")

        except Exception as e:
            log.error(f"Error en bucle principal: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    main()
