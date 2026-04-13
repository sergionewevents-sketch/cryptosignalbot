import os
import time
import requests
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta

# ============================================================
# CONFIGURACIÓN
# ============================================================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

VOLUME_MULTIPLIER   = float(os.environ.get("VOLUME_MULTIPLIER", "3.0"))
DOMINANCE_THRESHOLD = float(os.environ.get("DOMINANCE_THRESHOLD", "0.68"))
COOLDOWN_MINUTES    = int(os.environ.get("COOLDOWN_MINUTES", "30"))
MA_PERIOD           = int(os.environ.get("MA_PERIOD", "20"))
POLL_INTERVAL       = int(os.environ.get("POLL_INTERVAL", "10"))
DAILY_REPORT_HOUR   = int(os.environ.get("DAILY_REPORT_HOUR", "23"))

# Tiempos de cierre múltiples
CLOSE_TIMES = [5, 7, 9]

# Filtro de horario (hora Madrid CEST = UTC+2)
TRADING_HOUR_START = int(os.environ.get("TRADING_HOUR_START", "9"))
TRADING_HOUR_END   = int(os.environ.get("TRADING_HOUR_END", "20"))

# Filtro RSI
RSI_PERIOD    = int(os.environ.get("RSI_PERIOD", "14"))
RSI_LONG_MAX  = int(os.environ.get("RSI_LONG_MAX", "70"))   # LONG solo si RSI < 70
RSI_SHORT_MIN = int(os.environ.get("RSI_SHORT_MIN", "35"))  # SHORT solo si RSI > 35
RSI_OVERBOUGHT= int(os.environ.get("RSI_OVERBOUGHT", "72")) # zona sobrecompra

# Pares de Quantfury en KuCoin
SYMBOLS = [
    "BTC-USDT", "SOL-USDT", "AAVE-USDT", "LINK-USDT", "DOT-USDT",
    "ETH-USDT", "ARB-USDT", "AVAX-USDT", "NEO-USDT", "OP-USDT",
    "POL-USDT", "RENDER-USDT", "RUNE-USDT", "S-USDT", "SUI-USDT",
    "TAO-USDT", "THETA-USDT", "TON-USDT", "APT-USDT", "HBAR-USDT",
    "INJ-USDT", "DOGE-USDT", "LTC-USDT", "NEAR-USDT", "BCH-USDT",
    "ATOM-USDT", "UNI-USDT", "SAND-USDT", "ADA-USDT", "MANA-USDT",
    "FIL-USDT", "XRP-USDT", "ONDO-USDT", "VIRTUAL-USDT", "XLM-USDT",
    "ZEC-USDT",
]

KUCOIN_BASE = "https://api.kucoin.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# ESTADO
# ============================================================
last_signal_time = {}
pending_signals  = []

def make_stats():
    return {t: {"total": 0, "win": 0, "loss": 0, "pnl": 0.0} for t in CLOSE_TIMES}

daily_stats        = make_stats()
weekly_stats       = make_stats()
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
def get_klines(symbol: str, interval: str = "1min", limit: int = 22):
    try:
        url = f"{KUCOIN_BASE}/api/v1/market/candles"
        params = {"symbol": symbol, "type": interval}
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
        elif r.status_code == 400:
            log.warning(f"KuCoin par no disponible: {symbol}")
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
    result = []
    for k in reversed(klines):
        result.append({
            "close":  float(k[2]),
            "volume": float(k[5]),
        })
    return result

def get_orderbook_pressure(symbol: str):
    try:
        url = f"{KUCOIN_BASE}/api/v1/market/orderbook/level2_20"
        r = requests.get(url, params={"symbol": symbol}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "200000":
                bids = data["data"]["bids"]
                asks = data["data"]["asks"]
                bid_vol = sum(float(b[1]) for b in bids)
                ask_vol = sum(float(a[1]) for a in asks)
                total = bid_vol + ask_vol
                if total == 0:
                    return 0.5, 0.5
                return bid_vol / total, ask_vol / total
        return 0.5, 0.5
    except Exception as e:
        log.error(f"KuCoin orderbook exception {symbol}: {e}")
        return 0.5, 0.5

# ============================================================
# FILTRO DE HORARIO
# ============================================================
def is_trading_hours() -> bool:
    now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)
    return TRADING_HOUR_START <= now_madrid.hour < TRADING_HOUR_END

# ============================================================
# RSI
# ============================================================
def calculate_rsi(klines: list, period: int = 14) -> float:
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
# LÓGICA DE DETECCIÓN
# ============================================================
def check_symbol(symbol: str):
    if not is_trading_hours():
        return None

    limit = max(RSI_PERIOD + 2, MA_PERIOD + 2)
    klines_raw = get_klines(symbol, limit=limit)
    if len(klines_raw) < MA_PERIOD + 1:
        return None

    klines = parse_klines(klines_raw)
    current = klines[-1]
    history = klines[:-1]

    avg_volume = sum(k["volume"] for k in history[-MA_PERIOD:]) / MA_PERIOD
    if avg_volume == 0:
        return None

    vol_ratio = current["volume"] / avg_volume
    if vol_ratio < VOLUME_MULTIPLIER:
        return None

    buy_ratio, sell_ratio = get_orderbook_pressure(symbol)

    if buy_ratio >= DOMINANCE_THRESHOLD:
        direction = "LONG"
        dominance_pct = round(buy_ratio * 100, 1)
    elif sell_ratio >= DOMINANCE_THRESHOLD:
        direction = "SHORT"
        dominance_pct = round(sell_ratio * 100, 1)
    else:
        return None

    rsi = calculate_rsi(klines, period=RSI_PERIOD)

    if direction == "LONG" and rsi >= RSI_LONG_MAX:
        log.info(f"Señal {symbol} LONG descartada por RSI alto: {rsi}")
        return None
    if direction == "LONG" and rsi >= RSI_OVERBOUGHT:
        log.info(f"Señal {symbol} LONG descartada por sobrecompra: RSI {rsi}")
        return None
    if direction == "SHORT" and rsi <= RSI_SHORT_MIN:
        log.info(f"Señal {symbol} SHORT descartada por RSI bajo: {rsi}")
        return None

    return {
        "symbol":        symbol,
        "direction":     direction,
        "price":         current["close"],
        "vol_ratio":     round(vol_ratio, 2),
        "dominance_pct": dominance_pct,
        "rsi":           rsi,
    }

def is_in_cooldown(symbol: str) -> bool:
    if symbol not in last_signal_time:
        return False
    elapsed = (datetime.now(timezone.utc) - last_signal_time[symbol]).total_seconds()
    return elapsed < COOLDOWN_MINUTES * 60

# ============================================================
# FORMATEO DE MENSAJES
# ============================================================
def format_signal(signal: dict) -> str:
    symbol_name = signal["symbol"].replace("-USDT", "")
    emoji     = "🟢" if signal["direction"] == "LONG" else "🔴"
    action    = "LONG  📈" if signal["direction"] == "LONG" else "SHORT 📉"
    dom_label = "💚 Dominancia compradora" if signal["direction"] == "LONG" else "🔴 Dominancia vendedora"
    return (
        f"{emoji} <b>SEÑAL {action} — {symbol_name}/USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Precio: <b>${signal['price']:,.4f}</b>\n"
        f"📊 Volumen: <b>{signal['vol_ratio']}x</b> sobre la media\n"
        f"{dom_label}: <b>{signal['dominance_pct']}%</b>\n"
        f"📉 RSI: <b>{signal.get('rsi', '-')}</b>\n"
        f"⏱️ Cierres: 5, 7 y 9 min\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def format_resolution(signal: dict, close_price: float) -> str:
    symbol_name = signal["symbol"].replace("-USDT", "")
    entry = signal["entry_price"]
    t = signal["close_min"]
    pct_change = ((close_price - entry) / entry) * 100
    pnl_pct = pct_change if signal["direction"] == "LONG" else -pct_change
    pnl_eur = round(pnl_pct / 100, 4)
    result_emoji = "✅" if pnl_pct > 0 else "❌"
    result_label = "GANADA" if pnl_pct > 0 else "PERDIDA"
    sign = "+" if pnl_eur >= 0 else ""
    return (
        f"{result_emoji} <b>CIERRE {t}min — {symbol_name} {signal['direction']} — {result_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📥 Entrada: ${entry:,.4f}\n"
        f"📤 Cierre:  ${close_price:,.4f}\n"
        f"📊 Movimiento: {pct_change:+.2f}%\n"
        f"💶 P&L (1€): {sign}{pnl_eur:.4f}€\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_stats_block(stats: dict, title: str) -> str:
    lines = [f"<b>{title}</b>", "━━━━━━━━━━━━━━━━━━━"]
    total = stats[CLOSE_TIMES[0]]["total"]
    lines.append(f"📨 Señales: <b>{total}</b>")
    for t in CLOSE_TIMES:
        s = stats[t]
        if s["total"] == 0:
            lines.append(f"⏱️ {t}min → sin datos")
            continue
        wr = round((s["win"] / s["total"]) * 100, 1)
        pnl = round(s["pnl"], 4)
        pnl_str = f"+{pnl}€" if pnl >= 0 else f"{pnl}€"
        lines.append(f"⏱️ {t}min → {s['win']}/{s['total']} ({wr}%) | {pnl_str}")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

def format_daily_report() -> str:
    total = daily_stats[CLOSE_TIMES[0]]["total"]
    if total == 0:
        return "📊 <b>RESUMEN DIARIO</b>\n\nNo hubo señales hoy."
    date_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    return "📊 " + format_stats_block(daily_stats, f"RESUMEN DIARIO — {date_str}")

def format_weekly_report() -> str:
    total = weekly_stats[CLOSE_TIMES[0]]["total"]
    if total == 0:
        return "📊 <b>RESUMEN SEMANAL</b>\n\nNo hubo señales esta semana."
    date_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    return "📊 " + format_stats_block(weekly_stats, f"RESUMEN SEMANAL — {date_str}")

# ============================================================
# RESOLUCIÓN DE SEÑALES
# ============================================================
def resolve_pending_signals():
    now = datetime.now(timezone.utc)
    for signal in pending_signals:
        if signal["resolved"]:
            continue
        elapsed = (now - signal["entry_time"]).total_seconds()
        if elapsed < signal["close_min"] * 60:
            continue
        close_price = get_current_price(signal["symbol"])
        if close_price is None:
            continue
        signal["resolved"] = True
        entry = signal["entry_price"]
        pct_change = ((close_price - entry) / entry) * 100
        pnl_pct = pct_change if signal["direction"] == "LONG" else -pct_change
        pnl_eur = round(pnl_pct / 100, 4)
        t = signal["close_min"]
        daily_stats[t]["total"] += 1
        daily_stats[t]["pnl"] = round(daily_stats[t]["pnl"] + pnl_eur, 4)
        weekly_stats[t]["total"] += 1
        weekly_stats[t]["pnl"] = round(weekly_stats[t]["pnl"] + pnl_eur, 4)
        if pnl_eur > 0:
            daily_stats[t]["win"] += 1
            weekly_stats[t]["win"] += 1
        else:
            daily_stats[t]["loss"] += 1
            weekly_stats[t]["loss"] += 1
        send_telegram(format_resolution(signal, close_price))
        log.info(f"Cierre {t}min: {signal['symbol']} {signal['direction']} → {pnl_eur:+.4f}€")

# ============================================================
# RESUMEN DIARIO Y SEMANAL
# ============================================================
def check_daily_report():
    global last_daily_report, daily_stats
    now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)
    today = now_madrid.date()
    if last_daily_report == today:
        return
    if now_madrid.hour != DAILY_REPORT_HOUR:
        return
    last_daily_report = today
    send_telegram(format_daily_report())
    log.info("Resumen diario enviado")
    daily_stats = make_stats()

def check_weekly_report():
    global last_weekly_report, weekly_stats
    now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)
    if now_madrid.weekday() != 6:
        return
    if now_madrid.hour != DAILY_REPORT_HOUR:
        return
    today = now_madrid.date()
    if last_weekly_report == today:
        return
    last_weekly_report = today
    send_telegram(format_weekly_report())
    log.info("Resumen semanal enviado")
    weekly_stats = make_stats()

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
    log.info("🚀 CryptoSignalBot v4 arrancado!")
    send_telegram(
        "🚀 <b>CryptoSignalBot v4 activado</b>\n"
        f"Monitorizando {len(SYMBOLS)} pares cada {POLL_INTERVAL}s\n"
        f"Volumen: {VOLUME_MULTIPLIER}x | Dominancia: {int(DOMINANCE_THRESHOLD*100)}% | RSI: <{RSI_LONG_MAX}/>{ RSI_SHORT_MIN}\n"
        f"Horario: {TRADING_HOUR_START}:00-{TRADING_HOUR_END}:00 Madrid | Cooldown: {COOLDOWN_MINUTES}min"
    )

    cycle = 0
    while True:
        try:
            cycle += 1
            signals_this_cycle = 0
            signaled_this_cycle = set()

            if not is_trading_hours():
                if cycle % 60 == 0:
                    now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)
                    log.info(f"Fuera de horario ({now_madrid.strftime('%H:%M')} Madrid) — bot en pausa")
                time.sleep(POLL_INTERVAL)
                continue

            for symbol in SYMBOLS:
                if is_in_cooldown(symbol):
                    continue
                if symbol in signaled_this_cycle:
                    continue

                signal = check_symbol(symbol)
                if signal is None:
                    continue

                last_signal_time[symbol] = datetime.now(timezone.utc)
                signaled_this_cycle.add(symbol)
                signals_this_cycle += 1
                send_telegram(format_signal(signal))

                now = datetime.now(timezone.utc)
                for close_min in CLOSE_TIMES:
                    pending_signals.append({
                        "symbol":      symbol,
                        "direction":   signal["direction"],
                        "entry_price": signal["price"],
                        "entry_time":  now,
                        "close_min":   close_min,
                        "resolved":    False,
                    })

                log.info(f"Señal: {symbol} {signal['direction']} | {signal['vol_ratio']}x vol | {signal['dominance_pct']}% dom | RSI {signal['rsi']}")
                time.sleep(1.0)

            resolve_pending_signals()
            pending_signals[:] = [s for s in pending_signals if not s["resolved"]]
            check_daily_report()
            check_weekly_report()

            if signals_this_cycle > 0:
                log.info(f"Ciclo {cycle} — {signals_this_cycle} señal(es)")
            elif cycle % 60 == 0:
                log.info(f"Ciclo {cycle} — Sin señales | Pendientes: {len(pending_signals)}")

        except Exception as e:
            log.error(f"Error en bucle principal: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=start_health_server, daemon=True).start()
    main()
