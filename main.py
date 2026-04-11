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
DOMINANCE_THRESHOLD = float(os.environ.get("DOMINANCE_THRESHOLD", "0.75"))
COOLDOWN_MINUTES    = int(os.environ.get("COOLDOWN_MINUTES", "30"))
MA_PERIOD           = int(os.environ.get("MA_PERIOD", "20"))
CLOSE_MINUTES       = int(os.environ.get("CLOSE_MINUTES", "5"))
POLL_INTERVAL       = int(os.environ.get("POLL_INTERVAL", "10"))
DAILY_REPORT_HOUR   = int(os.environ.get("DAILY_REPORT_HOUR", "23"))

# Pares de Quantfury en KuCoin (formato BASE-USDT)
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

daily_stats = {
    "total": 0, "win": 0, "loss": 0, "pnl": 0.0,
    "best_signal": None, "worst_signal": None,
}
last_daily_report = None

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
    """
    Obtiene las últimas velas de 1 minuto para un símbolo.
    KuCoin kline endpoint: /api/v1/market/candles
    interval: "1min"
    Respuesta: list de [time, open, close, high, low, volume, turnover]
    Orden: más reciente primero
    """
    try:
        url = f"{KUCOIN_BASE}/api/v1/market/candles"
        params = {"symbol": symbol, "type": interval}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "200000":
                return data["data"][:limit]  # ya viene ordenado desc
            else:
                log.error(f"KuCoin error {symbol}: {data.get('msg')}")
                return []
        else:
            log.error(f"KuCoin HTTP error {symbol}: {r.status_code}")
            return []
    except Exception as e:
        log.error(f"KuCoin exception {symbol}: {e}")
        return []

def get_current_price(symbol: str):
    """Obtiene el precio actual de un símbolo."""
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
    Usamos close como precio y volume como volumen.
    """
    result = []
    for k in reversed(klines):
        result.append({
            "close":  float(k[2]),
            "volume": float(k[5]),
        })
    return result

def get_orderbook_pressure(symbol: str):
    """
    Obtiene presión compradora/vendedora del orderbook de KuCoin.
    Suma volumen de los primeros 20 niveles de bid y ask.
    """
    try:
        url = f"{KUCOIN_BASE}/api/v1/market/orderbook/level2_20"
        r = requests.get(url, params={"symbol": symbol}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == "200000":
                bids = data["data"]["bids"]  # [[price, qty], ...]
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
# LÓGICA DE DETECCIÓN
# ============================================================
def check_symbol(symbol: str):
    """
    1. Detecta pico de volumen vs media histórica
    2. Si hay pico, consulta orderbook para dirección
    """
    klines_raw = get_klines(symbol, limit=MA_PERIOD + 2)
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

    # Pico detectado — consultar orderbook para dirección
    buy_ratio, sell_ratio = get_orderbook_pressure(symbol)

    if buy_ratio >= DOMINANCE_THRESHOLD:
        direction = "LONG"
        dominance_pct = round(buy_ratio * 100, 1)
    elif sell_ratio >= DOMINANCE_THRESHOLD:
        direction = "SHORT"
        dominance_pct = round(sell_ratio * 100, 1)
    else:
        return None

    return {
        "symbol":        symbol,
        "direction":     direction,
        "price":         current["close"],
        "vol_ratio":     round(vol_ratio, 2),
        "dominance_pct": dominance_pct,
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
    emoji  = "🟢" if signal["direction"] == "LONG" else "🔴"
    action = "LONG  📈" if signal["direction"] == "LONG" else "SHORT 📉"
    dom_label = "💚 Dominancia compradora" if signal["direction"] == "LONG" else "🔴 Dominancia vendedora"

    return (
        f"{emoji} <b>SEÑAL {action} — {symbol_name}/USDT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Precio: <b>${signal['price']:,.4f}</b>\n"
        f"📊 Volumen: <b>{signal['vol_ratio']}x</b> sobre la media\n"
        f"{dom_label}: <b>{signal['dominance_pct']}%</b>\n"
        f"⏱️ Cierre estimado en {CLOSE_MINUTES} min\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC"
    )

def format_resolution(signal: dict, close_price: float) -> str:
    symbol_name = signal["symbol"].replace("-USDT", "")
    entry = signal["entry_price"]
    pct_change = ((close_price - entry) / entry) * 100
    pnl_pct = pct_change if signal["direction"] == "LONG" else -pct_change
    pnl_eur = round(pnl_pct / 100, 4)
    result_emoji = "✅" if pnl_pct > 0 else "❌"
    result_label = "GANADA" if pnl_pct > 0 else "PERDIDA"

    return (
        f"{result_emoji} <b>CIERRE — {symbol_name} {signal['direction']} — {result_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📥 Entrada: ${entry:,.4f}\n"
        f"📤 Cierre:  ${close_price:,.4f}\n"
        f"📊 Movimiento: {pct_change:+.2f}%\n"
        f"💶 P&L (1€): {'+' if pnl_eur > 0 else ''}{pnl_eur:.4f}€\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

def format_daily_report() -> str:
    s = daily_stats
    total = s["total"]
    if total == 0:
        return "📊 <b>RESUMEN DIARIO</b>\n\nNo hubo señales hoy."

    win_rate = round((s["win"] / total) * 100, 1)
    pnl = round(s["pnl"], 4)
    pnl_str = f"+{pnl}€" if pnl >= 0 else f"{pnl}€"
    best  = s["best_signal"]
    worst = s["worst_signal"]
    best_str  = f"\n🏆 Mejor: {best['symbol'].replace('-USDT','')} {best['direction']} ({'+' if best['pnl']>=0 else ''}{best['pnl']:.4f}€)" if best else ""
    worst_str = f"\n💀 Peor:  {worst['symbol'].replace('-USDT','')} {worst['direction']} ({'+' if worst['pnl']>=0 else ''}{worst['pnl']:.4f}€)" if worst else ""

    return (
        f"📊 <b>RESUMEN DIARIO — {datetime.now(timezone.utc).strftime('%d %b %Y')}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📨 Señales: <b>{total}</b>\n"
        f"✅ Acertadas: <b>{s['win']}</b> ({win_rate}%)\n"
        f"❌ Falladas:  <b>{s['loss']}</b>\n"
        f"💶 P&L total: <b>{pnl_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━"
        f"{best_str}{worst_str}"
    )

# ============================================================
# RESOLUCIÓN DE SEÑALES
# ============================================================
def resolve_pending_signals():
    now = datetime.now(timezone.utc)
    for signal in pending_signals:
        if signal["resolved"]:
            continue
        elapsed = (now - signal["entry_time"]).total_seconds()
        if elapsed < CLOSE_MINUTES * 60:
            continue

        close_price = get_current_price(signal["symbol"])
        if close_price is None:
            continue

        signal["resolved"] = True
        entry = signal["entry_price"]
        pct_change = ((close_price - entry) / entry) * 100
        pnl_pct = pct_change if signal["direction"] == "LONG" else -pct_change
        pnl_eur = round(pnl_pct / 100, 4)

        daily_stats["total"] += 1
        daily_stats["pnl"] = round(daily_stats["pnl"] + pnl_eur, 4)
        if pnl_eur > 0:
            daily_stats["win"] += 1
        else:
            daily_stats["loss"] += 1

        sig_result = {"symbol": signal["symbol"], "direction": signal["direction"], "pnl": pnl_eur}
        if daily_stats["best_signal"] is None or pnl_eur > daily_stats["best_signal"]["pnl"]:
            daily_stats["best_signal"] = sig_result
        if daily_stats["worst_signal"] is None or pnl_eur < daily_stats["worst_signal"]["pnl"]:
            daily_stats["worst_signal"] = sig_result

        send_telegram(format_resolution(signal, close_price))
        log.info(f"Señal resuelta: {signal['symbol']} {signal['direction']} → {pnl_eur:+.4f}€")

# ============================================================
# RESUMEN DIARIO
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
    daily_stats.update({"total": 0, "win": 0, "loss": 0, "pnl": 0.0,
                        "best_signal": None, "worst_signal": None})

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
    log.info("🚀 CryptoSignalBot v3 arrancado!")
    send_telegram(
        "🚀 <b>CryptoSignalBot activado</b>\n"
        f"Monitorizando {len(SYMBOLS)} pares cada {POLL_INTERVAL}s\n"
        f"Umbral volumen: {VOLUME_MULTIPLIER}x | Dominancia: {int(DOMINANCE_THRESHOLD*100)}% | Cooldown: {COOLDOWN_MINUTES}min"
    )

    cycle = 0
    while True:
        try:
            cycle += 1
            signals_this_cycle = 0

            for symbol in SYMBOLS:
                if is_in_cooldown(symbol):
                    continue

                signal = check_symbol(symbol)
                if signal is None:
                    continue

                last_signal_time[symbol] = datetime.now(timezone.utc)
                signals_this_cycle += 1
                send_telegram(format_signal(signal))

                pending_signals.append({
                    "symbol":      symbol,
                    "direction":   signal["direction"],
                    "entry_price": signal["price"],
                    "entry_time":  datetime.now(timezone.utc),
                    "resolved":    False,
                })

                log.info(f"Señal: {symbol} {signal['direction']} | {signal['vol_ratio']}x vol | {signal['dominance_pct']}% dom")
                time.sleep(0.3)

            resolve_pending_signals()
            pending_signals[:] = [s for s in pending_signals if not s["resolved"]]
            check_daily_report()

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
