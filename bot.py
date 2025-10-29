import asyncio
import aiohttp
import ccxt.async_support as ccxt
import json
import time
import logging
import traceback
from datetime import datetime
from flask import Flask, render_template_string, jsonify

CONFIG_FILE = "config.json"
POLL_INTERVAL = 3
CONCURRENCY = 10
LOG_FILE = "bot.log"

prices_cache = {}
AUTO_LIMIT = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {
            "TELEGRAM_TOKEN": "8471152392:AAHHYjhIcaGV1DVherO5sVYKO8OZubgY4r0",
            "CHAT_ID": "721323324",
            "SPREAD_THRESHOLD": 1.0,
            "AUTO_MODE": True,
            "AUTO_LIMIT": 60
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    return cfg

CONFIG = load_config()
AUTO_MODE = CONFIG.get("AUTO_MODE", True)
AUTO_LIMIT = CONFIG.get("AUTO_LIMIT", 60)
SPREAD_THRESHOLD = CONFIG.get("SPREAD_THRESHOLD", 1.0)

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
    except Exception:
        pass
    return None

async def try_quanto_mappings(session, base_symbol):
    candidates = [
        f"{base_symbol}-USDT-SWAP-LIN",
        f"{base_symbol}-USD-SWAP-LIN",
        f"{base_symbol}-USDT-SWAP",
        f"{base_symbol}-USD-SWAP",
        f"{base_symbol}-USDT",
        f"{base_symbol}-USD",
    ]
    for c in candidates:
        p = await fetch_quanto_price(session, c)
        if p is not None:
            return c, p
    return None, None

async def get_latest_pairs(exchange, limit=AUTO_LIMIT):
    logging.info("🔍 Recupero lista mercati da MEXC...")
    markets = await exchange.load_markets()
    swap_pairs = [
        (s, m)
        for s, m in markets.items()
        if m.get("type") in ("swap", "future") and ("USDT" in s or "USD" in s)
    ]
    if not swap_pairs:
        logging.warning("⚠️ Nessuna coppia swap trovata su MEXC!")
        return []
    # Ordina prima per listing_date, poi per nome (fallback)
    swap_pairs.sort(key=lambda x: x[1].get("info", {}).get("listing_date", x[0]), reverse=True)
    pairs = [s for s, _ in swap_pairs[:limit]]
    logging.info(f"✅ Trovate {len(pairs)} coppie (esempio: {pairs[:5]})")
    return pairs

async def poll_loop():
    global prices_cache
    ccxt_mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    session = aiohttp.ClientSession()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    pairs = await get_latest_pairs(ccxt_mexc, AUTO_LIMIT)
    if not pairs:
        logging.warning("⚠️ Nessuna coppia trovata! Controlla la connessione o MEXC API.")
        await asyncio.sleep(10)
        return

    logging.info(f"Monitoraggio automatico di {len(pairs)} coppie MEXC.")
    try:
        while True:
            tasks = []
            for symbol in pairs:
                async with semaphore:
                    tasks.append(process_pair(symbol, ccxt_mexc, session))
            await asyncio.gather(*tasks)
            await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:
        logging.error(f"Errore loop: {e}\n{traceback.format_exc()}")
    finally:
        await ccxt_mexc.close()
        await session.close()

async def process_pair(symbol, exchange, session):
    global prices_cache
    try:
        mexc_p = await fetch_mexc_price(exchange, symbol)
        if not mexc_p:
            return
        base = symbol.split("/")[0]
        quanto_sym, quanto_p = await try_quanto_mappings(session, base)
        if not quanto_p:
            return
        spread = (quanto_p - mexc_p) / mexc_p * 100
        prices_cache[symbol] = {
            "mexc": mexc_p,
            "quanto": quanto_p,
            "spread": spread,
        }
    except Exception as e:
        logging.debug(f"Errore {symbol}: {e}")

# ---------------- WEB DASHBOARD ----------------
from flask import Flask, jsonify, render_template_string
app = Flask(__name__)

@app.route("/")
def dashboard():
    html = """
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <meta charset="UTF-8">
        <title>MEXC Auto Dashboard</title>
        <style>
            body{font-family:'Segoe UI';background:#0b0e11;color:#e0e0e0;margin:0;padding:20px;}
            h2{color:#00ff99;}
            #status{margin-bottom:10px;color:#888;}
            table{width:100%;border-collapse:collapse;margin-top:10px;}
            th,td{border:1px solid #222;padding:6px;text-align:center;}
            th{background:#12181f;}
            tr:nth-child(even){background:#161b22;}
            .positive{color:#00ff99;}
            .negative{color:#ff5555;}
        </style>
    </head>
    <body>
        <h2>📈 MEXC Quanto Auto Dashboard</h2>
        <p>🧠 Modalità: <b style="color:#00ff99;">Automatica</b> — Ultime {{limit}} coppie listate su MEXC</p>
        <div id="status">Caricamento dati...</div>
        <table>
            <thead>
                <tr>
                    <th>Coppia</th><th>MEXC</th><th>Quanto</th><th>Spread %</th>
                </tr>
            </thead>
            <tbody id="pairsTable"></tbody>
        </table>
        <script>
            async function loadPairs(){
                const res = await fetch("/api/prices");
                const data = await res.json();
                const table=document.getElementById("pairsTable");
                const status=document.getElementById("status");
                table.innerHTML="";
                const now=new Date().toLocaleTimeString();
                status.innerText=`Ultimo aggiornamento: ${now} | Coppie: ${Object.keys(data).length}`;
                for(const [pair,info] of Object.entries(data)){
                    const spreadClass=info.spread>0?'positive':'negative';
                    table.innerHTML+=`
                        <tr>
                            <td>${pair}</td>
                            <td>${info.mexc?.toFixed(4)??'-'}</td>
                            <td>${info.quanto?.toFixed(4)??'-'}</td>
                            <td class="${spreadClass}">${info.spread?.toFixed(2)??'-'}%</td>
                        </tr>`;
                }
            }
            setInterval(loadPairs,3000);
            loadPairs();
        </script>
    </body>
    </html>
    """
    return render_template_string(html, limit=AUTO_LIMIT)

@app.route("/api/prices")
def api_prices():
    return jsonify(prices_cache)

if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(poll_loop()), daemon=True).start()
    app.run(host="0.0.0.0", port=5000)






