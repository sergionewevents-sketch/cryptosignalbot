#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
TRADES GRANDES EN VIVO (Bybit)  ->  Telegram
================================================================================
Avisa cuando se EJECUTA un trade grande en el mercado (por defecto, perpetuos de
Bybit), por encima de un importe en dólares. Es un "whale alert" de operaciones
reales, no de posiciones.

POR QUÉ BYBIT Y NO BINANCE:
  Binance bloquea la IP de los servidores de Railway (error 451). Bybit no, y por
  eso ya lo usamos en el bot de cripto. Los datos de Bybit son públicos y gratis.

QUÉ MIRA:
  El feed público de operaciones recientes de Bybit (/v5/market/recent-trade).
  Cada operación trae precio, tamaño, lado (compra/venta agresiva) y hora.
  Importe en USD = precio x tamaño. Las operaciones de una misma orden (misma hora
  y lado) se agrupan y se suman, para captar también órdenes grandes "troceadas".

LIMITACIÓN HONESTA:
  Detecta operaciones grandes según se ejecutan. Una orden enorme repartida en
  muchísimos trocitos a lo largo de segundos podría no llegar al umbral de golpe.
  Para órdenes normales y bloques OTC (isBlockTrade) funciona de sobra.

Requisitos: Python 3.8+ y solo librería estándar.
Despliegue: igual que los otros bots -> GitHub + Railway (región Europa).
================================================================================
"""

import os
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# ==============================================================================
# CONFIGURACIÓN (variables de Railway)
# ==============================================================================

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "PEGA_AQUI_TU_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PEGA_AQUI_TU_CHAT_ID")

def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)

def _envi(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)

# Importe mínimo del trade en dólares para avisar
MIN_USD = _envf("MIN_USD", 250000)

# Qué mercados vigilar (separados por comas). Símbolos de Bybit.
SYMBOLS = [s.strip().upper() for s in
           os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()]

# "linear" = perpetuos USDT (recomendado, más volumen de ballenas y hasta 1000 trades
# por consulta). "spot" = contado (máximo 60 por consulta).
CATEGORY = os.environ.get("CATEGORY", "linear").lower()

POLL_SECONDS = _envf("POLL_SECONDS", 4)   # cada cuánto se consultan las operaciones

# Endpoint público de Bybit
BYBIT_URL = "https://api.bybit.com/v5/market/recent-trade"
LIMIT = 60 if CATEGORY == "spot" else 1000   # máximos que permite Bybit

# ==============================================================================
# HTTP / TELEGRAM
# ==============================================================================

def http_get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "bybit-whale/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def telegram_send(text):
    if "PEGA_AQUI" in TELEGRAM_TOKEN or "PEGA_AQUI" in TELEGRAM_CHAT_ID:
        print("[AVISO] Telegram no configurado. Mensaje:\n" + text + "\n")
        return
    url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_TOKEN
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=30).read()
    except Exception as e:
        print("[ERROR Telegram] %s" % e)
    time.sleep(0.05)

# ==============================================================================
# BYBIT
# ==============================================================================

def fetch_trades(symbol):
    url = "%s?category=%s&symbol=%s&limit=%d" % (BYBIT_URL, CATEGORY, symbol, LIMIT)
    data = http_get_json(url)
    if data.get("retCode") != 0:
        print("[ERROR Bybit %s] %s" % (symbol, data.get("retMsg")))
        return []
    return (data.get("result", {}) or {}).get("list", []) or []

def base_coin(symbol):
    for q in ("USDT", "USDC", "USD"):
        if symbol.endswith(q):
            return symbol[:-len(q)]
    return symbol

def fmt_usd(x):
    return "$" + format(int(round(x)), ",")

def fmt_size(x):
    # tamaño legible (sin decimales raros para números grandes)
    return ("%.4f" % x).rstrip("0").rstrip(".") if x < 1000 else format(int(round(x)), ",")

# ==============================================================================
# PROCESAR UN SÍMBOLO
# ==============================================================================

def process_symbol(symbol, seen, baseline):
    try:
        trades = fetch_trades(symbol)
    except Exception as e:
        print("[ERROR al leer %s] %s" % (symbol, e))
        return 0

    fresh = []
    for t in trades:
        eid = t.get("execId")
        if eid and eid not in seen:
            seen.add(eid)
            fresh.append(t)

    if baseline:
        return 0  # primera vuelta: solo memorizamos, sin avisar

    # agrupar por (hora, lado): trocitos de una misma orden -> un solo aviso
    groups = {}
    for t in fresh:
        groups.setdefault((t.get("time"), t.get("side")), []).append(t)

    alerts = 0
    for (tm, side), items in groups.items():
        try:
            usd = sum(float(x["price"]) * float(x["size"]) for x in items)
        except (TypeError, ValueError, KeyError):
            continue
        if usd < MIN_USD:
            continue
        size  = sum(float(x["size"]) for x in items)
        price = float(items[0]["price"])
        block = any(x.get("isBlockTrade") for x in items)
        emoji = "🟢" if side == "Buy" else "🔴"
        accion = "COMPRA" if side == "Buy" else "VENTA"
        try:
            hora = datetime.fromtimestamp(int(tm) / 1000, timezone.utc).strftime("%H:%M:%S UTC")
        except (TypeError, ValueError):
            hora = "?"
        lines = [
            "🐋 <b>TRADE GRANDE</b> (%s)" % CATEGORY,
            "%s <b>%s %s</b>" % (emoji, accion, symbol),
            "Valor: <b>%s</b>" % fmt_usd(usd),
            "Tamaño: %s %s @ %s" % (fmt_size(size), base_coin(symbol), fmt_usd(price)),
        ]
        if block:
            lines.append("📦 Block trade (OTC)")
        if len(items) > 1:
            lines.append("(orden en %d ejecuciones)" % len(items))
        lines.append("🕒 %s" % hora)
        telegram_send("\n".join(lines))
        print("  ALERTA: %s %s %s" % (accion, symbol, fmt_usd(usd)))
        alerts += 1
    return alerts

# ==============================================================================
# BUCLE PRINCIPAL
# ==============================================================================

def main():
    print("Vigilando trades grandes en Bybit (%s): %s. Umbral: %s." % (
        CATEGORY, ", ".join(SYMBOLS), fmt_usd(MIN_USD)))
    telegram_send(
        "✅ Bot de trades grandes iniciado (Bybit %s).\n"
        "Mercados: %s\nAvisaré de operaciones de %s o más.\n"
        "Haciendo foto inicial..." % (CATEGORY, ", ".join(SYMBOLS), fmt_usd(MIN_USD)))

    seen = set()
    baseline = True

    while True:
        cycle_start = time.time()
        total = 0
        for sym in SYMBOLS:
            total += process_symbol(sym, seen, baseline)
            time.sleep(0.1)

        # limitar tamaño del set
        if len(seen) > 60000:
            seen = set(list(seen)[-30000:])

        if baseline:
            baseline = False
            print("Foto inicial completada. Vigilancia activa.")
            telegram_send("📸 Foto inicial lista. Vigilancia activa.")

        time.sleep(max(0, POLL_SECONDS - (time.time() - cycle_start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
