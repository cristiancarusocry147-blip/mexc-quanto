import os
import asyncio
import aiohttp
import ccxt.async_support as ccxt
import json
import time
import logging
import traceback
import threading
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string

# =============== CONFIGURAZIONE BASE ===============
CONFIG_FILE = "config.json"
POLL_INTERVAL = 5
MAX_PAIRS = 50
CONCURRENCY = 10
LOG_FILE = "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            data.setdefault("SPREAD_THRESHOLD", 1.0)
            return data
    except FileNotFoundError:
        example = {
            "TELEGRAM_TOKEN": "INSERISCI_IL_TUO_TOKEN",
            "CHAT_ID": "INSERISCI_CHAT_ID",
            "SPREAD_THRESHOLD": 1.0
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(example, f, indent=4)
        print("Creato config.json — inserisci il tuo TOKEN Telegram e CHAT_ID.")
        raise SystemExit(1)

CONFIG = load_config()
TELEGRAM_TOKEN = CONFIG.get("TELEGRAM_TOKEN")
CHAT_ID = CONFIG.get("CHAT_ID")
SPREAD_THRESHOLD = float(CONFIG.get("SPREAD_THRESHOLD", 1.0))

# Stato globale del bot
bot_status = {
    "running": False,
    "pairs": [],
    "spreads": {},
    "start_time": time.time(),
}

# =============== TELEGRAM ===============
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

# =============== FUNZIONI DI MERCATO ===============
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

async def build_pairs(exchange, limit=MAX_PAIRS):
    markets = await exchange.load_markets()
    swap_pairs = [
        s for s, m in markets.items()
        if m.get("type") == "swap" and ("USDT" in s or "USD" in s)
    ]
    return swap_pairs[:limit]

# =============== LOGICA PRINCIPALE ===============
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
        bot_status["spreads"][symbol] = spread

        prev = last_spread.get(symbol, 0)
        if abs(spread) >= SPREAD_THRESHOLD and abs(spread) > abs(prev):
            direction = "Compra su MEXC / Vendi su Quanto" if spread > 0 else "Vendi su MEXC / Compra su Quanto"
            msg = f"{'🟢' if spread>0 else '🔴'} {symbol}\nSpread: {spread:.2f}%\n{direction}"
            logging.info(msg)
            await send_telegram_message(msg, session)
        last_spread[symbol] = spread

    except Exception as e:
        logging.debug(f"Errore {symbol}: {e}")

async def poll_loop():
    ccxt_mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    session = aiohttp.ClientSession()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    prices = {}
    last_spread = {}

    pairs = await build_pairs(ccxt_mexc)
    bot_status["pairs"] = pairs
    bot_status["running"] = True
    logging.info(f"Trovate {len(pairs)} coppie futures su MEXC.")
    await send_telegram_message(f"🤖 Bot avviato. Monitoraggio di {len(pairs)} coppie su MEXC.", session)

    try:
        while True:
            tasks = []
            for symbol in pairs:
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

# =============== THREAD PER IL BOT ===============
def start_async_loop():
    asyncio.run(poll_loop())

threading.Thread(target=start_async_loop, daemon=True).start()

# =============== FLASK SERVER ===============
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Mexc Quanto Bot is running!"

@app.route('/status')
def status():
    uptime = int(time.time() - bot_status["start_time"])
    return jsonify({
        "running": bot_status["running"],
        "pairs": len(bot_status["pairs"]),
        "uptime": f"{uptime//3600}h {uptime%3600//60}m",
        "spreads": bot_status["spreads"]
    })

@app.route('/spreads')
def get_spreads():
    spreads = bot_status.get("spreads", {})
    sorted_spreads = sorted(spreads.items(), key=lambda x: x[1], reverse=True)
    result = [{"pair": pair, "spread": round(spread, 3)} for pair, spread in sorted_spreads]
    return jsonify(result)

@app.route('/addpair', methods=['POST'])
def add_pair():
    data = request.get_json()
    pair = data.get("pair")
    if not pair:
        return jsonify({"error": "Specifica una coppia es. BTC/USDT"}), 400
    if pair not in bot_status["pairs"]:
        bot_status["pairs"].append(pair)
        return jsonify({"message": f"Coppia {pair} aggiunta con successo."})
    return jsonify({"message": f"La coppia {pair} è già monitorata."})

# =============== DASHBOARD HTML ===============
HTML_PAGE = """
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <title>MEXC Quanto Bot Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #121212; color: #e0e0e0; margin: 0; padding: 20px; }
        h1 { color: #00ffc3; }
        table { border-collapse: collapse; width: 100%; margin-top: 20px; }
        th, td { border-bottom: 1px solid #333; padding: 10px; text-align: left; }
        th { background: #222; color: #00ffc3; }
        tr:hover { background: #1e1e1e; }
        .positive { color: #00ff6a; }
        .negative { color: #ff5252; }
        input[type="text"] { padding: 8px; border: none; border-radius: 4px; width: 200px; }
        button { padding: 8px 15px; border: none; border-radius: 4px; background: #00ffc3; color: #000; cursor: pointer; }
        button:hover { background: #00c89a; }
    </style>
    <script>
        async function loadSpreads() {
            const res = await fetch('/spreads');
            const data = await res.json();
            const tbody = document.getElementById('spread-table');
            tbody.innerHTML = '';
            data.forEach(row => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${row.pair}</td>
                    <td class="${row.spread >= 0 ? 'positive' : 'negative'}">${row.spread.toFixed(3)}%</td>
                `;
                tbody.appendChild(tr);
            });
        }

        async function addPair() {
            const pair = document.getElementById('pair-input').value;
            if (!pair) return alert("Inserisci una coppia es. BTC/USDT");
            await fetch('/addpair', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ pair })
            });
            document.getElementById('pair-input').value = '';
            loadSpreads();
        }

        setInterval(loadSpreads, 5000);
        window.onload = loadSpreads;
    </script>
</head>
<body>
    <h1>📊 Mexc Quanto Bot Dashboard</h1>
    <p>Monitoraggio in tempo reale delle coppie MEXC ↔ Quanto.trade</p>
    <div>
        <input id="pair-input" type="text" placeholder="Aggiungi coppia es. BTC/USDT" />
        <button onclick="addPair()">Aggiungi Coppia</button>
    </div>
    <table>
        <thead>
            <tr>
                <th>Coppia</th>
                <th>Spread (%)</th>
            </tr>
        </thead>
        <tbody id="spread-table"></tbody>
    </table>
</body>
</html>
"""

@app.route('/dashboard')
def dashboard():
    return render_template_string(HTML_PAGE)

# Porta per Render
port = int(os.environ.get("PORT", 10000))
app.run(host="0.0.0.0", port=port)
# ---------------- ENTRYPOINT ----------------
if __name__ == "__main__":

    asyncio.run(poll_loop())
