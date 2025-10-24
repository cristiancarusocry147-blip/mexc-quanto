
import asyncio
import aiohttp
import ccxt.async_support as ccxt
import json
import time
import logging
import traceback
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
import threading
import os

CONFIG_FILE = "config.json"
POLL_INTERVAL = 5
CONCURRENCY = 10
LOG_FILE = "bot.log"

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ---------------- CONFIG ----------------
def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            data.setdefault("SPREAD_THRESHOLD", 1.0)
            data.setdefault("PAIRS", ["BTC/USDT", "ETH/USDT"])
            return data
    except FileNotFoundError:
        example = {
            "TELEGRAM_TOKEN": "",
            "CHAT_ID": "",
            "SPREAD_THRESHOLD": 1.0,
            "PAIRS": ["BTC/USDT", "ETH/USDT"]
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(example, f, indent=4)
        print("Creato config.json — inserisci il tuo TOKEN Telegram e CHAT_ID.")
        raise SystemExit(1)

CONFIG = load_config()
TELEGRAM_TOKEN = CONFIG.get("TELEGRAM_TOKEN")
CHAT_ID = CONFIG.get("CHAT_ID")
SPREAD_THRESHOLD = float(CONFIG.get("SPREAD_THRESHOLD", 1.0))
PAIRS = CONFIG.get("PAIRS", [])

prices = {}  # spread live per dashboard

# ---------------- TELEGRAM ----------------
async def send_telegram_message(text, session=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    try:
        async with session.post(url, data=payload) as resp:
            if resp.status != 200:
                logging.warning(f"Telegram HTTP {resp.status}")
    except Exception as e:
        logging.error(f"Errore Telegram: {e}")

# ---------------- FETCH HELPERS ----------------
async def fetch_mexc_price(exchange, symbol):
    try:
        ticker = await exchange.fetch_ticker(symbol)
        return float(ticker.get("last")) if ticker.get("last") else None
    except Exception:
        return None

async def fetch_quanto_price(session, market_code):
    try:
        url = f"https://api.quanto.trade/v3/depth?marketCode={market_code}&level=1"
        async with session.get(url, timeout=8) as resp:
            data = await resp.json()
            if data.get("success") and "data" in data:
                bids = data["data"].get("bids", [])
                asks = data["data"].get("asks", [])
                if bids and asks:
                    bid = float(bids[0][0])
                    ask = float(asks[0][0])
                    return (bid + ask) / 2
        return None
    except Exception:
        return None

async def try_quanto_mappings(session, base_symbol):
    candidates = [
        f"{base_symbol}-USD-SWAP-LIN",
        f"{base_symbol}-USDT-SWAP-LIN",
        f"{base_symbol}-USD-SWAP",
        f"{base_symbol}-USDT-SWAP",
        f"{base_symbol}-USD",
        f"{base_symbol}-USDT",
    ]
    for c in candidates:
        p = await fetch_quanto_price(session, c)
        if p is not None:
            return c, p
    return None, None

# ---------------- MAIN LOOP ----------------
async def poll_loop():
    ccxt_mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    session = aiohttp.ClientSession()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    last_spread = {}

    logging.info(f"Monitoraggio di {len(PAIRS)} coppie su MEXC.")
    await send_telegram_message(f"🤖 Bot avviato.\nMonitoraggio di {len(PAIRS)} coppie su MEXC.", session)

    try:
        while True:
            for symbol in CONFIG["PAIRS"]:
                async with semaphore:
                    asyncio.create_task(process_pair(symbol, ccxt_mexc, session, last_spread))
            await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:
        logging.error(f"Errore nel loop: {e}\n{traceback.format_exc()}")
        await send_telegram_message(f"❌ Errore: {e}", session)
    finally:
        await session.close()
        await ccxt_mexc.close()

async def process_pair(symbol, exchange, session, last_spread):
    try:
        mexc_p = await fetch_mexc_price(exchange, symbol)
        if not mexc_p:
            return
        base = symbol.split("/")[0]
        quanto_sym, quanto_p = await try_quanto_mappings(session, base)
        if not quanto_p:
            return
        spread = (quanto_p - mexc_p) / mexc_p * 100
        prices[symbol] = round(spread, 2)

        prev = last_spread.get(symbol, 0)
        if abs(spread) >= SPREAD_THRESHOLD and abs(spread) > abs(prev):
            direction = "Compra su MEXC / Vendi su Quanto" if spread > 0 else "Vendi su MEXC / Compra su Quanto"
            msg = f"{'🟢' if spread>0 else '🔴'} {symbol}\nSpread: {spread:.2f}%\n{direction}"
            logging.info(msg)
            await send_telegram_message(msg, session)
        last_spread[symbol] = spread
    except Exception as e:
        logging.debug(f"Errore {symbol}: {e}")

# ---------------- FLASK WEB DASHBOARD ----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ MEXC Quanto Bot è attivo!"

@app.route('/dashboard')
def dashboard():
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
            current_pairs = cfg.get("PAIRS", [])
    except Exception:
        current_pairs = PAIRS

    msg = request.args.get("msg", "")
    html = """
    <h2>MEXC Quanto Bot Dashboard</h2>
    {% if msg %}
        <p style="color: green;">{{ msg }}</p>
    {% endif %}
    <p>Coppie monitorate:</p>
    <table border="1" cellpadding="6" cellspacing="0">
        <tr><th>Coppia</th><th>Spread (%)</th><th>Azioni</th></tr>
        {% for pair in pairs %}
        <tr>
            <td>{{ pair }}</td>
            <td>{{ prices.get(pair, '---') }}</td>
            <td>
                <form action="/removepair" method="post" style="display:inline;">
                    <input type="hidden" name="pair" value="{{ pair }}">
                    <button type="submit">❌</button>
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
    <br>
    <form action="/addpair" method="post">
        <input name="pair" placeholder="es. BTC/USDT" required>
        <button type="submit">➕ Aggiungi coppia</button>
    </form>
    <script>
        setTimeout(() => { window.location.reload(); }, 10000);
    </script>
    """
    return render_template_string(html, pairs=current_pairs, prices=prices, msg=msg)

@app.route('/addpair', methods=['POST'])
def add_pair():
    pair = request.form.get("pair", "").strip().upper()
    if not pair:
        return "Nessuna coppia specificata.", 400
    CONFIG["PAIRS"].append(pair)
    with open(CONFIG_FILE, "w") as f:
        json.dump(CONFIG, f, indent=4)
    return f"""<script>window.location.href='/dashboard?msg=✅ Coppia {pair} aggiunta con successo';</script>"""

@app.route('/removepair', methods=['POST'])
def remove_pair():
    pair = request.form.get("pair", "").strip().upper()
    if pair in CONFIG["PAIRS"]:
        CONFIG["PAIRS"].remove(pair)
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=4)
        return f"""<script>window.location.href='/dashboard?msg=❌ Coppia {pair} rimossa';</script>"""
    return f"""<script>window.location.href='/dashboard?msg=⚠️ Coppia non trovata';</script>"""

@app.route('/status')
def status():
    return jsonify({
        "running": True,
        "pairs": CONFIG["PAIRS"],
        "prices": prices,
        "spread_threshold": SPREAD_THRESHOLD
    })

# ---------------- AVVIO ----------------
def start_async_loop():
    asyncio.run(poll_loop())

if __name__ == "__main__":
    t = threading.Thread(target=start_async_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


