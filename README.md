# 🐋 Trades Grandes en Vivo (Bybit) → Telegram

Avisa por Telegram cuando se **ejecuta** un trade grande en el mercado (por defecto,
perpetuos USDT de Bybit) por encima de un importe en dólares. Es un "whale alert" de
**operaciones reales**, no de posiciones.

> Usamos **Bybit** porque Binance bloquea la IP de Railway (el error 451 que ya sufrimos).
> Los datos de Bybit son públicos y gratis, sin claves.

---

## 📦 Archivos
- `bybit_whale_trades.py` — el bot (solo librería estándar)
- `Procfile` — proceso continuo para Railway
- `requirements.txt` — vacío (para que Railway detecte Python)

## 🚀 Desplegar en Railway (igual que los otros bots)
1. Sube los 3 archivos a un repo NUEVO de GitHub (ej. `bigtrades`).
2. Railway → New Project → Deploy from GitHub repo → región **Europa (Ámsterdam)**.
3. Variables: `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.
4. Logs → verás `✅ Bot de trades grandes iniciado`.

## 🎚️ Variables (opcionales salvo token y chat)
| Variable           | Por defecto              | Para qué |
|--------------------|--------------------------|----------|
| `MIN_USD`          | `250000`                 | importe mínimo del trade en dólares |
| `SYMBOLS`          | `BTCUSDT,ETHUSDT,SOLUSDT`| mercados a vigilar (separados por comas) |
| `CATEGORY`         | `linear`                 | `linear` = perpetuos · `spot` = contado |
| `POLL_SECONDS`     | `4`                      | cada cuánto se consultan las operaciones |

Ejemplos: para ver solo pelotazos muy gordos, `MIN_USD=1000000`. Para vigilar más
monedas, `SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT`.

## 🧠 Cómo lee las operaciones
- 🟢 COMPRA = alguien ejecutó una compra agresiva (taker). 🔴 VENTA = venta agresiva.
- Las ejecuciones de una misma orden (misma hora y lado) se **agrupan** y se suman,
  para captar también órdenes grandes troceadas.
- Los **block trades** (OTC, suelen ser institucionales) se marcan con 📦.

## ⚠️ Notas honestas
- Detecta operaciones grandes según se ejecutan. Una orden enorme repartida en
  muchísimos trocitos a lo largo de varios segundos podría no llegar al umbral de golpe.
- En `spot`, Bybit solo deja leer 60 operaciones por consulta (en `linear`, hasta 1000),
  así que en mercados muy movidos `linear` se pierde menos.
- Un trade grande es información de flujo, no una orden de compra: úsalo como contexto.
