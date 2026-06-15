> **Disclaimer:** Not Financial Advice, educational purposes only

# IBKR Setup Guide

How to set up Interactive Brokers (IBKR) **IB Gateway** and the Python environment
to run the live bots in this folder:

- [`orb_strategy.py`](orb_strategy.py) — connects to `127.0.0.1`, port selectable via
  `--port` (default `4002` = paper), client id `10`
- [`orb_vwap_filter/orb_vwap_filter.py`](orb_vwap_filter/orb_vwap_filter.py) — connects to
  the host/port set in its constants (`IBKR_HOST`, `IBKR_PORT = 4001` = live, client id `21`)

Both bots talk to IBKR through **`ib_insync`**. They do **not** use a broker REST key —
all access goes through a running Gateway (or TWS) session on your machine/LAN.

---

## 1. Prerequisites

- An **Interactive Brokers account** (a free **paper-trading** account is enabled from
  Client Portal → Settings → Paper Trading Account — use this first).
- **Market-data subscriptions** for what you trade. For QQQ you need US equity +
  **US options (OPRA)** real-time data, otherwise the bots get delayed/empty quotes.
  Add them in Client Portal → Settings → **Market Data Subscriptions**.
- **Python 3.9+** (the bots use `zoneinfo`, added in 3.9). Check with `python3 --version`.
- ~2 GB free disk for the Gateway + a Java runtime (bundled with the installer).

---

## 2. Install IB Gateway

IB Gateway is the lightweight, headless alternative to TWS — recommended for bots.

1. Download the **latest/stable IB Gateway** for your OS:
   <https://www.interactivebrokers.com/en/trading/ibgateway-stable.php>
2. Run the installer and launch **IB Gateway**.
3. At login, choose the **mode**:
   - **Paper Trading** — for testing (recommended first).
   - **Live Trading** — real money.
4. Log in with your IBKR username/password (paper login = your paper credentials).

> **Ports differ by app and mode** — this is the #1 source of "connection refused":
>
> | App | Live | Paper |
> |---|---|---|
> | **IB Gateway** | **4001** | **4002** |
> | TWS (desktop) | 7496 | 7497 |
>
> `orb_strategy.py` defaults to **4002 (paper)**; pass `--port 4001` for live.
> `orb_vwap_filter.py` is hard-set to **4001 (live)** in its constants — change it for paper.

---

## 3. Configure the API

In IB Gateway: **Configure → Settings → API → Settings** (in TWS: Edit → Global
Configuration → API → Settings):

- ☑ **Enable ActiveX and Socket Clients**
- ☐ **Read-Only API** — must be **unchecked** so the bots can place orders
- **Socket port** — confirm it matches the table above (4001 live / 4002 paper)
- **Trusted IPs** — add the IP that runs the bot:
  - same machine → add `127.0.0.1`
  - a different machine on your LAN (e.g. the bot connects to `172.31.9.221`) → add that
    bot machine's IP here, and make sure the Gateway host's firewall allows the port
- **Master API client ID** — leave blank, or set it and keep bot client ids distinct
  (this repo uses **10** for `orb_strategy.py`, **21** for `orb_vwap_filter.py`; never run
  two clients with the same id against one Gateway)
- ☑ **Allow connections from localhost only** — uncheck **only** if connecting over the LAN
- Optionally raise **API → Settings → "Logging Level"** to detail while debugging

**Auto-restart instead of daily logout:** IB Gateway force-logs-out once a day.
Configure **Configure → Settings → Lock and Exit → Auto restart** (and set the restart
time outside market hours) so your session survives overnight.

---

## 4. Python environment & libraries

Use a virtual environment so the bot deps stay isolated:

```bash
cd ibkr                      # this folder
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
```

**Live bot dependencies** (what the bots import):

```bash
pip install ib_insync numpy
```

| Package | Used for |
|---|---|
| `ib_insync` | all IBKR connectivity, contracts, orders (pulls in `eventkit`, `nest_asyncio`) |
| `numpy` | VWAP / indicator math in `orb_vwap_filter.py` |

**Optional — only for the offline backtests/studies** in `orb_vwap_filter/`
(`backtest_vwap_filter.py`, `grid_search.py`, etc., which read local parquet data, **not**
IBKR):

```bash
pip install pandas pyarrow
```

Pin them for reproducibility if you like:

```bash
pip freeze > requirements.txt
```

> **Python 3.13 note:** older `ib_insync` releases can warn under 3.13's asyncio. If you
> hit event-loop errors, use Python 3.11/3.12, or `pip install ib_async` (the maintained
> fork) which is API-compatible — change the import to `from ib_async import ...`.

---

## 5. Point the bots at your Gateway

Make sure each bot's connection settings match your running Gateway:

**`orb_vwap_filter/orb_vwap_filter.py`** (top of file):

```python
IBKR_HOST = '172.31.9.221'   # 127.0.0.1 if Gateway runs on the same machine
IBKR_PORT = 4001             # 4001 live / 4002 paper
IBKR_CLIENT_ID = 21          # unique per client
```

**`orb_strategy.py`** — host/client id are constants near the top
(`HOST = "127.0.0.1"`, `CLIENT_ID = 10`); the port is a CLI flag:

```bash
python3 orb_strategy.py                 # paper (4002)
python3 orb_strategy.py --port 4001     # live
```

**Live vs delayed data:** both call `ib.reqMarketDataType(1)` (real-time). If your account
lacks a live subscription, switch to `3` (delayed) for testing — orders still work, quotes
lag ~15 min.

---

## 6. Verify the connection

With IB Gateway **logged in and running**, run the bundled check (from
`orb_vwap_filter/`):

```bash
cd orb_vwap_filter
./test_orb_vwap_filter.sh        # checks files, perms, and that ib_insync imports
```

Then confirm an actual socket handshake with a one-liner (adjust host/port/id):

```bash
python3 - <<'PY'
from ib_insync import IB, Stock
ib = IB()
ib.connect('127.0.0.1', 4002, clientId=99)   # paper Gateway on localhost
print('connected:', ib.isConnected())
ib.qualifyContracts(Stock('QQQ', 'SMART', 'USD'))
print('server time:', ib.reqCurrentTime())
ib.disconnect()
PY
```

A printed `connected: True` and a server time means you're ready.

---

## 7. Run the bots

Manual:

```bash
python3 orb_vwap_filter/orb_vwap_filter.py     # one poll cycle (designed to run each minute)
```

Scheduled (the VWAP bot is built to be invoked every minute by cron):

```bash
crontab -e
# run every minute during the session; the bot self-checks trading hours
* * * * * /path/to/ibkr/orb_vwap_filter/orb_vwap_filter.sh
```

Edit `orb_vwap_filter.sh` so it activates your venv and `cd`s to the bot dir before
calling `python3 orb_vwap_filter.py`.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `ConnectionRefusedError` / can't connect | Gateway not running, **wrong port** (4001 vs 4002), or API not enabled |
| `peer closed connection` right after connect | Bot's IP not in **Trusted IPs**, or "localhost only" is checked for a LAN connection |
| `clientId N already in use` | Another client/bot is using that id — give each a unique `clientId` |
| Empty/`nan` prices, "market data farm" only | Missing/duplicate **market-data subscription**, or use `reqMarketDataType(3)` for delayed |
| `Error 200: No security definition` | Contract not qualified / wrong exchange — QQQ uses `SMART`; 0DTE options need today's expiry |
| Orders rejected | Account permissions (options trading not enabled), insufficient buying power, or **Read-Only API** still checked |
| Session dies overnight | Enable **Auto restart** (Section 3) instead of daily auto-logout |

---

### Safety

Start in **paper trading** for at least a week, watch it during market hours, and use
small size before going live. 0DTE options are extremely risky — see
[`orb_vwap_filter/README.md`](orb_vwap_filter/README.md) and the backtests in that folder
for the strategy's (limited) historical edge.
