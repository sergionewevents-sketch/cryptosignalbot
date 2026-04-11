import os
import time
import requests
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ============================================================
# CONFIGURACIÓN
# ============================================================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")

# Umbrales de detección
VOLUME_MULTIPLIER    = float(os.environ.get("VOLUME_MULTIPLIER", "3.0"))    # Veces sobre la media
DOMINANCE_THRESHOLD  = float(os.environ.get("DOMINANCE_THRESHOLD", "0.75")) # % mínimo de un lado
COOLDOWN_MINUTES     = int(os.environ.get("COOLDOWN_MINUTES", "30"))         # Minutos entre señales del mismo par
MA_PERIOD            = int(os.environ.get("MA_PERIOD", "20"))                # Velas para calcular la media
CLOSE_MINUTES        = int(os.environ.get("CLOSE_MINUTES", "5"))             # Minutos para cerrar posición estimada
POLL_INTERVAL        = int(os.environ.get("POLL_INTERVAL", "10"))            # Segundos entre consultas
DAILY_REPORT_HOUR    = int(os.environ.get("DAILY_REPORT_HOUR", "23"))        # Hora del resumen diario (Madrid)

# Pares de Quantfury contra USDT en Binance
SYMBOLS = [
    "BTCUSDT", "SOLUSDT", "AAVEUSDT", "LINKUSDT", "DOTUSDT",
    "ETHUSDT", "ARBUSDT", "AVAXUSDT", "NEOUSDT", "OPUSDT",
    "POLUSDT", "RENDERUSDT", "RUNEUSDT", "SUSDT", "SUIUSDT",
    "TAOUSDT", "THETAUSDT", "TONUSDT", "APTUSDT", "HBARUSDT",
    "INJUSDT", "DOGEUSDT", "LTCUSDT", "NEARUSDT", "BCHUSDT",
    "ATOMUSDT", "UNIUSDT", "SANDUSDT", "ADAUSDT", "MANAUSDT",
    "FILUSDT", "XRPUSDT", "ONDOUSDT", "VIRTUALUSDT", "XLMUSDT",
    "ZECUSDT",
]

BINANCE_BASE = "https://api.binance.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# ESTADO
# ============================================================
# Cooldown: symbol -> datetime de última señal
last_signal_time = {}

# Señales pendientes de resolución: lista de dicts
# { symbol, direction, entry_price, entry_time, resolved }
pending_signals = []

# Estadísticas del día
daily_stats = {
    "total": 0,
    "win": 0,
    "loss": 0,
    "pnl": 0.0,
    "best_signal": None,   # { symbol, direction, pnl }
    "worst_signal": None,
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
# BINANCE API
# ============================================================
def get_klines(symbol: str, interval: str = "1m", limit: int = 22):
    """Obtiene las últimas `limit` velas de 1 minuto para un símbolo."""
    try:
        url = f"{BINANCE_BASE}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        else:
            log.error(f"Binance klines error {symbol}: {r.status_code}")
            return []
    except Exception as e:
        log.error(f"Binance klines exception {symbol}: {e}")
        return []

def get_current_price(symbol: str):
    """Obtiene el precio actual de un símbolo."""
    try:
        url = f"{BINANCE_BASE}/api/v3/ticker/price"
        r = requests.get(url, params={"symbol": symbol}, timeout=10)
        if r.status_code == 200:
            return float(r.json()["price"])
        return None
    except Exception as e:
        log.error(f"Binance price exception {symbol}: {e}")
        return None

def parse_klines(klines: list):
    """
    Extrae de cada vela:
      - volume: volumen total
      - taker_buy_volume: volumen iniciado por compradores
      - close: precio de cierre
    Binance kline formato: [open_time, open, high, low, close, volume,
                             close_time, quote_vol, trades,
                             taker_buy_base_vol, taker_buy_quote_vol, ignore]
    """
    result = []
    for k in klines:
        result.append({
            "close":            float(k[4]),
            "volume":           float(k[5]),
            "taker_buy_volume": float(k[9]),
        })
    return result

# ============================================================
# LÓGICA DE DETECCIÓN
# ============================================================
def check_symbol(symbol: str):
    """
    Analiza un símbolo y devuelve una señal si se cumplen las condiciones,
    o None si no hay señal.
    """
    klines_raw = get_klines(symbol, limit=MA_PERIOD + 2)
    if len(klines_raw) < MA_PERIOD + 1:
        return None

    klines = parse_klines(klines_raw)

    # La vela actual es la última (puede estar incompleta), usamos la penúltima
    # como vela "cerrada" más reciente para el análisis
    current = klines[-1]        # vela en formación (la más fresca)
    history = klines[:-1]       # velas anteriores completas

    # Media de volumen de las últimas MA_PERIOD velas completas
    avg_volume = sum(k["volume"] for k in history[-MA_PERIOD:]) / MA_PERIOD

    if avg_volume == 0:
        return None

    # Volumen actual vs media
    vol_ratio = current["volume"] / avg_volume
    if vol_ratio < VOLUME_MULTIPLIER:
        return None

    # Dominancia compradora/vendedora
    total_vol = current["volume"]
    if total_vol == 0:
        return None

    buy_vol  = current["taker_buy_volume"]
    sell_vol = total_vol - buy_vol

    buy_ratio  = buy_vol / total_vol
    sell_ratio = sell_vol / total_vol

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
    symbol_name = signal["symbol"].replace("USDT", "")
    emoji = "🟢" if signal["direction"] == "LONG" else "🔴"
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
    symbol_name = signal["symbol"].replace("USDT", "")
    entry  = signal["entry_price"]
    pct_change = ((close_price - entry) / entry) * 100

    if signal["direction"] == "LONG":
        pnl_pct = pct_change
    else:
        pnl_pct = -pct_change

    pnl_eur = round(pnl_pct / 100, 4)  # sobre 1€ invertido
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

    win_rate = round((s["win"] / total) * 100, 1) if total > 0 else 0
    pnl = round(s["pnl"], 4)
    pnl_str = f"+{pnl}€" if pnl >= 0 else f"{pnl}€"

    best  = s["best_signal"]
    worst = s["worst_signal"]

    best_str  = f"\n🏆 Mejor señal: {best['symbol'].replace('USDT','')} {best['direction']} ({'+' if best['pnl']>=0 else ''}{best['pnl']:.4f}€)" if best else ""
    worst_str = f"\n💀 Peor señal:  {worst['symbol'].replace('USDT','')} {worst['direction']} ({'+' if worst['pnl']>=0 else ''}{worst['pnl']:.4f}€)" if worst else ""

    date_str = datetime.now(timezone.utc).strftime("%d %b %Y")

    return (
        f"📊 <b>RESUMEN DIARIO — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📨 Señales enviadas: <b>{total}</b>\n"
        f"✅ Acertadas: <b>{s['win']}</b> ({win_rate}%)\n"
        f"❌ Falladas:  <b>{s['loss']}</b>\n"
        f"💶 P&L total: <b>{pnl_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━"
        f"{best_str}"
        f"{worst_str}"
    )

# ============================================================
# RESOLUCIÓN DE SEÑALES (cierre estimado a 5 min)
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
        if signal["direction"] == "LONG":
            pnl_pct = pct_change
        else:
            pnl_pct = -pct_change

        pnl_eur = round(pnl_pct / 100, 4)

        # Actualizar estadísticas
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

    now_madrid = datetime.now(timezone.utc) + timedelta(hours=2)  # CEST (verano España)
    today = now_madrid.date()

    if last_daily_report == today:
        return
    if now_madrid.hour != DAILY_REPORT_HOUR:
        return

    last_daily_report = today
    send_telegram(format_daily_report())
    log.info("Resumen diario enviado")

    # Reset estadísticas para el día siguiente
    daily_stats["total"]        = 0
    daily_stats["win"]          = 0
    daily_stats["loss"]         = 0
    daily_stats["pnl"]          = 0.0
    daily_stats["best_signal"]  = None
    daily_stats["worst_signal"] = None

# ============================================================
# BUCLE PRINCIPAL
# ============================================================
def main():
    log.info("🚀 CryptoSignalBot arrancado!")
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

                # Registrar señal
                last_signal_time[symbol] = datetime.now(timezone.utc)
                signals_this_cycle += 1

                # Enviar alerta Telegram
                send_telegram(format_signal(signal))

                # Guardar para resolución posterior
                pending_signals.append({
                    "symbol":      signal["symbol"],
                    "direction":   signal["direction"],
                    "entry_price": signal["price"],
                    "entry_time":  datetime.now(timezone.utc),
                    "resolved":    False,
                })

                log.info(f"Señal: {symbol} {signal['direction']} | {signal['vol_ratio']}x vol | {signal['dominance_pct']}% dom")

                # Pequeña pausa entre símbolos para no saturar la API
                time.sleep(0.3)

            # Resolver señales pendientes
            resolve_pending_signals()

            # Limpiar señales ya resueltas (mantener lista limpia)
            pending_signals[:] = [s for s in pending_signals if not s["resolved"]]

            # Resumen diario
            check_daily_report()

            if signals_this_cycle > 0:
                log.info(f"Ciclo {cycle} — {signals_this_cycle} señal(es) disparada(s)")
            elif cycle % 60 == 0:
                log.info(f"Ciclo {cycle} — Sin señales | Pendientes: {len(pending_signals)}")

        except Exception as e:
            log.error(f"Error en bucle principal: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
