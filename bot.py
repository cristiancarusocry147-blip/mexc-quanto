import asyncio
import aiohttp
import ccxt.async_support as ccxt
import json
import time
import logging
import traceback
from datetime import datetime
from flask import Flask, render_template_string, request, redirect

CONFIG_FILE = "config.json"
POLL_INTERVAL = 3
MAX_PAIRS = 50
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
            data.setdefault("PAIRS", [])
            return data
    except FileNotFoundError:
        example = {
            "TELEGRAM_TOKEN": "INSERISCI_IL_TUO_TOKEN",
            "CHAT_ID": "INSERISCI_CHAT_ID",
            "SPREAD_THRESHOLD": 1.0,
            "PAIRS": []
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

async def build_pairs(exchange, limit=MAX_PAIRS):
    markets = await exchange.load_markets()
    swap_pairs = [
        s for s, m in markets.items()
        if m.get("type") == "swap" and ("USDT" in s or "USD" in s)
    ]
    return swap_pairs[:limit]

# ---------------- MAIN LOOP ----------------
async def poll_loop():
    ccxt_mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    session = aiohttp.ClientSession()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    prices = {}
    last_spread = {}
    start_time = time.time()

    if PAIRS:
        pairs = PAIRS
        logging.info(f"Monitoraggio di {len(pairs)} coppie dal config.json")
    else:
        pairs = await build_pairs(ccxt_mexc)
        logging.info(f"Nessuna coppia specificata, caricate {len(pairs)} coppie da MEXC")

    await send_telegram_message(f"🤖 Bot avviato.\nMonitoraggio di {len(pairs)} coppie su MEXC.", session)

    asyncio.create_task(handle_status_commands(session, prices, start_time))

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

# ---------------- STATUS HANDLER ----------------
async def handle_status_commands(session, prices, start_time):
    offset = 0
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    while True:
        try:
            async with session.get(url, params={"offset": offset, "timeout": 10}) as resp:
                data = await resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {}).get("text", "")
                    chat_id = update.get("message", {}).get("chat", {}).get("id")
                    if msg == "/status" and str(chat_id) == str(CHAT_ID):
                        uptime = int(time.time() - start_time)
                        avg_spread = sum(prices.values()) / len(prices) if prices else 0
                        text = (
                            f"📊 *Status Bot*\n"
                            f"Coppie monitorate: {len(prices)}\n"
                            f"Spread medio: {avg_spread:.2f}%\n"
                            f"Uptime: {uptime//3600}h {uptime%3600//60}m\n"
                            f"Ultimo update: {datetime.utcnow().strftime('%H:%M:%S UTC')}"
                        )
                        await send_telegram_message(text, session)
        except Exception:
            pass
        await asyncio.sleep(5)

# ---------------- WEB DASHBOARD ----------------
from flask import Flask, render_template_string, request, redirect, jsonify

app = Flask(__name__)

prices_cache = {}  # aggiornato dal loop principale


@app.route("/")
def home():
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    html = """
    <!DOCTYPE html>
    <html lang="it">
    <head>
        <meta charset="UTF-8">
        <title>Trading Dashboard</title>
        <style>
            body {
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                background-color: #0b0e11;
                color: #e0e0e0;
                margin: 0;
                padding: 20px;
            }
            h2 { color: #00ff99; }
            table {
                width: 100%;
                border-collapse: collapse;
                margin-top: 15px;
            }
            th, td {
                border: 1px solid #222;
                padding: 8px;
                text-align: center;
            }
            th {
                background-color: #12181f;
            }
            tr:nth-child(even) {
                background-color: #161b22;
            }
            .positive { color: #00ff99; }
            .negative { color: #ff5555; }
            button {
                padding: 5px 10px;
                background-color: #222;
                border: 1px solid #333;
                border-radius: 5px;
                color: #eee;
                cursor: pointer;
            }
            button:hover {
                background-color: #00ff99;
                color: #000;
            }
            input {
                padding: 5px;
                background: #12181f;
                color: #eee;
                border: 1px solid #333;
                border-radius: 5px;
            }
        </style>
    </head>
    <body>
        <h2>📊 MEXC Quanto Trading Dashboard</h2>

        <form id="addForm" style="margin-bottom:10px;">
            <input name="pair" id="pairInput" placeholder="es. BTC/USDT" required>
            <button type="submit">➕ Aggiungi coppia</button>
        </form>

        <table>
            <thead>
                <tr>
                    <th>Coppia</th>
                    <th>Prezzo MEXC</th>
                    <th>Prezzo Quanto</th>
                    <th>Spread %</th>
                    <th>Azioni</th>
                </tr>
            </thead>
            <tbody id="pairsTable"></tbody>
        </table>

        <script>
            async function loadPairs() {
                const res = await fetch("/api/prices");
                const data = await res.json();
                const table = document.getElementById("pairsTable");
                table.innerHTML = "";
                for (const [pair, info] of Object.entries(data)) {
                    const tr = document.createElement("tr");
                    const spreadClass = info.spread > 0 ? 'positive' : 'negative';
                    tr.innerHTML = `
                        <td>${pair}</td>
                        <td>${info.mexc?.toFixed(4) ?? '-'}</td>
                        <td>${info.quanto?.toFixed(4) ?? '-'}</td>
                        <td class="${spreadClass}">${info.spread?.toFixed(2) ?? '-'}%</td>
                        <td>
                            <form action="/removepair" method="post">
                                <input type="hidden" name="pair" value="${pair}">
                                <button type="submit">❌ Rimuovi</button>
                            </form>
                        </td>
                    `;
                    table.appendChild(tr);
                }
            }

            document.getElementById("addForm").addEventListener("submit", async (e) => {
                e.preventDefault();
                const pair = document.getElementById("pairInput").value;
                await fetch("/addpair", {
                    method: "POST",
                    headers: { "Content-Type": "application/x-www-form-urlencoded" },
                    body: "pair=" + encodeURIComponent(pair)
                });
                document.getElementById("pairInput").value = "";
                loadPairs();
            });

            setInterval(loadPairs, 3000);
            loadPairs();
        </script>
    </body>
    </html>
    """
    return render_template_string(html)


@app.route("/api/prices")
def api_prices():
    return jsonify(prices_cache)


@app.route("/addpair", methods=["POST"])
def add_pair():
    pair = request.form.get("pair")
    if pair:
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        if "PAIRS" not in cfg:
            cfg["PAIRS"] = []
        if pair not in cfg["PAIRS"]:
            cfg["PAIRS"].append(pair)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    return redirect("/dashboard")


@app.route("/removepair", methods=["POST"])
def remove_pair():
    pair = request.form.get("pair")
    with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
    if "PAIRS" in cfg and pair in cfg["PAIRS"]:
        cfg["PAIRS"].remove(pair)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)
    return redirect("/dashboard")

# ---------------- ENTRYPOINT ----------------
if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(poll_loop()), daemon=True).start()
    app.run(host="0.0.0.0", port=5000)




