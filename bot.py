import asyncio
import aiohttp
import ccxt.async_support as ccxt
import json
import time
import logging
import traceback
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, jsonify

CONFIG_FILE = "config.json"
POLL_INTERVAL = 3
REFRESH_MARKETS = 600   # ogni 10 minuti
CONCURRENCY = 10
LOG_FILE = "bot.log"
AUTO_LIMIT = 60

prices_cache = {}
pairs_cache = []
last_refresh = 0
notified_new_pairs = set()

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
            data.setdefault("SPREAD_THRESHOLD", 2.0)
            data.setdefault("PAIRS", [])
            return data
    except FileNotFoundError:
        example = {
            "TELEGRAM_TOKEN": "8471152392:AAHHYjhIcaGV1DVherO5sVYKO8OZubgY4r0",
            "CHAT_ID": "721323324",
            "SPREAD_THRESHOLD": 2.0,
            "PAIRS": []
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(example, f, indent=4)
        print("Creato config.json — inserisci il tuo TOKEN Telegram e CHAT_ID.")
        raise SystemExit(1)

CONFIG = load_config()
TELEGRAM_TOKEN = CONFIG.get("TELEGRAM_TOKEN")
CHAT_ID = CONFIG.get("CHAT_ID")
SPREAD_THRESHOLD = float(CONFIG.get("SPREAD_THRESHOLD", 2.0))

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
        logging.warning("⚠️ Nessuna coppia trovata su MEXC!")
        return []
    swap_pairs.sort(key=lambda x: x[1].get("info", {}).get("listing_date", x[0]), reverse=True)
    pairs = [s for s, _ in swap_pairs[:limit]]
    logging.info(f"✅ Trovate {len(pairs)} coppie (es: {pairs[:5]})")
    return pairs

# ---------------- MAIN LOOP ----------------
async def poll_loop():
    global prices_cache, pairs_cache, last_refresh, notified_new_pairs
    ccxt_mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    session = aiohttp.ClientSession()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    pairs_cache = CONFIG.get("PAIRS", [])
    if not pairs_cache:
        pairs_cache = await get_latest_pairs(ccxt_mexc, AUTO_LIMIT)
    last_refresh = time.time()

    await send_telegram_message(f"🤖 Bot avviato. Monitoraggio di {len(pairs_cache)} coppie.", session)

    try:
        while True:
            # 🔁 Aggiorna lista coppie ogni 10 minuti
            if time.time() - last_refresh > REFRESH_MARKETS:
                new_pairs = await get_latest_pairs(ccxt_mexc, AUTO_LIMIT)
                added = [p for p in new_pairs if p not in pairs_cache]
                if added:
                    msg = f"🆕 Nuove coppie listate su MEXC:\n" + "\n".join(added[:10])
                    logging.info(msg)
                    await send_telegram_message(msg, session)
                    notified_new_pairs.update(added)
                pairs_cache = list(set(pairs_cache + new_pairs))
                last_refresh = time.time()

            # 🧮 Calcolo spread
            tasks = []
            for symbol in pairs_cache:
                async with semaphore:
                    tasks.append(process_pair(symbol, ccxt_mexc, session))
            await asyncio.gather(*tasks)
            await asyncio.sleep(POLL_INTERVAL)
    except Exception as e:
        logging.error(f"Errore loop: {e}\n{traceback.format_exc()}")
        await send_telegram_message(f"❌ Errore: {e}", session)
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
        prices_cache[symbol] = {"mexc": mexc_p, "quanto": quanto_p, "spread": spread}
        if abs(spread) >= SPREAD_THRESHOLD:
            msg = f"{'🟢' if spread>0 else '🔴'} {symbol}\nSpread: {spread:.2f}%"
            await send_telegram_message(msg, session)
    except Exception as e:
        logging.debug(f"Errore {symbol}: {e}")

# ---------------- WEB DASHBOARD ----------------
app = Flask(__name__)

@app.route("/")
def dashboard():
    html = """
    <html><head><title>Dashboard</title>
    <style>
    body{font-family:Segoe UI;background:#0b0e11;color:#e0e0e0;padding:20px;}
    h2{color:#00ff99;}
    table{width:100%;border-collapse:collapse;margin-top:10px;}
    th,td{border:1px solid #222;padding:6px;text-align:center;}
    th{background:#12181f;}
    tr:nth-child(even){background:#161b22;}
    .positive{color:#00ff99;} .negative{color:#ff5555;}
    input,button{padding:5px;border-radius:5px;}
    </style>
    </head><body>
    <h2>📊 MEXC Quanto Dashboard</h2>
    <p>Monitoraggio automatico + coppie manuali</p>
    <form method="POST" action="/addpair">
      <input name="pair" placeholder="es. BTC/USDT" required>
      <button>➕ Aggiungi coppia</button>
    </form>
    <br>
    <table><thead><tr><th>Coppia</th><th>MEXC</th><th>Quanto</th><th>Spread %</th><th>Rimuovi</th></tr></thead>
    <tbody id="pairsTable"></tbody></table>
    <script>
    async function loadPairs(){
      const res = await fetch("/api/prices");
      const data = await res.json();
      const table=document.getElementById("pairsTable");
      table.innerHTML="";
      for(const [pair,info] of Object.entries(data)){
        const cls=info.spread>0?'positive':'negative';
        table.innerHTML+=`
        <tr>
          <td>${pair}</td>
          <td>${info.mexc?.toFixed(4)??'-'}</td>
          <td>${info.quanto?.toFixed(4)??'-'}</td>
          <td class="${cls}">${info.spread?.toFixed(2)??'-'}%</td>
          <td><form method='POST' action='/removepair'>
              <input type='hidden' name='pair' value='${pair}'/>
              <button>❌</button></form></td>
        </tr>`;
      }
    }
    setInterval(loadPairs,3000);loadPairs();
    </script>
    </body></html>
    """
    return render_template_string(html)

@app.route("/addpair", methods=["POST"])
def add_pair():
    pair = request.form.get("pair")
    if pair:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        if pair not in cfg["PAIRS"]:
            cfg["PAIRS"].append(pair)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
        pairs_cache.append(pair)
    return redirect("/")

@app.route("/removepair", methods=["POST"])
def remove_pair():
    pair = request.form.get("pair")
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    if pair in cfg["PAIRS"]:
        cfg["PAIRS"].remove(pair)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)
    if pair in pairs_cache:
        pairs_cache.remove(pair)
    return redirect("/")

@app.route("/api/prices")
def api_prices():
    return jsonify(prices_cache)

# ---------------- ENTRYPOINT ----------------
if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(poll_loop()), daemon=True).start()
    app.run(host="0.0.0.0", port=5000)







