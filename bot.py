import asyncio
import aiohttp
import ccxt.async_support as ccxt
import json
import time
import logging
import traceback
from datetime import datetime
import threading
from flask import Flask, request, jsonify, render_template_string

CONFIG_FILE = "config.json"
POLL_INTERVAL = 3
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
            "TELEGRAM_TOKEN": "INSERISCI_IL_TUO_TOKEN",
            "CHAT_ID": "INSERISCI_CHAT_ID",
            "SPREAD_THRESHOLD": 1.0,
            "PAIRS": ["BTC/USDT", "ETH/USDT"]
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(example, f, indent=4)
        print("Creato config.json — inserisci i tuoi dati Telegram.")
        raise SystemExit(1)

CONFIG = load_config()
TELEGRAM_TOKEN = CONFIG.get("TELEGRAM_TOKEN")
CHAT_ID = CONFIG.get("CHAT_ID")
SPREAD_THRESHOLD = float(CONFIG.get("SPREAD_THRESHOLD", 1.0))
PAIRS = CONFIG.get("PAIRS", [])

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
    prices = {}
    last_spread = {}

    logging.info(f"Avvio bot con coppie: {PAIRS}")
    await send_telegram_message(f"🤖 Bot avviato.\nMonitoraggio di {len(PAIRS)} coppie su MEXC.", session)

    try:
        while True:
            tasks = []
            for symbol in PAIRS:
                async with semaphore:
                    tasks.append(process_pair(symbol, ccxt_mexc, session, prices, last_spread))
            await asyncio.gather(*tasks)
            await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:
        logging.error(f"Errore nel loop: {e}\n{traceback.format_exc()}")
        await send_telegram_message(f"❌ Errore: {e}", session)
    finally:
        await session.close()
        await ccxt_mexc.close()

async def process_pair(symbol, exchange, session, prices, last_spread):
    try:
        mexc_p = await fetch_mexc_price(exchange, symbol)
        if not mexc_p:
            return
        base = symbol.split("/")[0]
        quanto_sym, quanto_p = await try_quanto_mappings(session, base)
        if not quanto_p:
            return
        spread = (quanto_p - mexc_p) / mexc_p * 100
        prices[symbol] = spread

        prev = last_spread.get(symbol, 0)
        if abs(spread) >= SPREAD_THRESHOLD and abs(spread) > abs(prev):
            direction = "Compra su MEXC / Vendi su Quanto" if spread > 0 else "Vendi su MEXC / Compra su Quanto"
            msg = f"{'🟢' if spread>0 else '🔴'} {symbol}\nSpread: {spread:.2f}%\n{direction}"
            logging.info(msg)
            await send_telegram_message(msg, session)
        last_spread[symbol] = spread
    except Exception as e:
        logging.debug(f"Errore {symbol}: {e}")

# ---------------- FLASK DASHBOARD ----------------
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Mexc Quanto Bot is running! Vai su /dashboard per controllare."

@app.route('/dashboard')
def dashboard():
    html = """
    <h2>MEXC Quanto Bot Dashboard</h2>
    <p>Coppie monitorate:</p>
    <ul>
    {% for pair in pairs %}
        <li>{{ pair }}
        <form action="/removepair" method="post" style="display:inline;">
            <input type="hidden" name="pair" value="{{ pair }}">
            <button type="submit">❌ Rimuovi</button>
        </form>
        </li>
    {% endfor %}
    </ul>
    <form action="/addpair" method="post">
        <input name="pair" placeholder="es. BTC/USDT" required>
        <button type="submit">➕ Aggiungi coppia</button>
    </form>
    """
    return render_template_string(html, pairs=PAIRS)

@app.route('/addpair', methods=['POST'])
def addpair():
    pair = request.form.get('pair')
    if not pair:
        return "Manca la coppia", 400
    if pair not in PAIRS:
        PAIRS.append(pair)
        CONFIG["PAIRS"] = PAIRS
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=4)
        return f"Aggiunta {pair} con successo! <a href='/dashboard'>Torna</a>"
    else:
        return f"{pair} è già presente! <a href='/dashboard'>Torna</a>"

@app.route('/removepair', methods=['POST'])
def removepair():
    pair = request.form.get('pair')
    if pair in PAIRS:
        PAIRS.remove(pair)
        CONFIG["PAIRS"] = PAIRS
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=4)
        return f"Rimossa {pair} con successo! <a href='/dashboard'>Torna</a>"
    return "Coppia non trovata! <a href='/dashboard'>Torna</a>"

def start_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ---------------- ENTRYPOINT ----------------
if __name__ == "__main__":
    threading.Thread(target=lambda: asyncio.run(poll_loop()), daemon=True).start()
    start_flask()

