# Groww Trading Project

A personal Python + Flask app on the official
[Groww Trade API](https://groww.in/trade-api/docs/python-sdk) (TOTP login):

- **Backtest** a 9/21 EMA crossover strategy on NIFTY weekly options with
  1-minute Groww data stored in Postgres, and audit every simulated trade on a
  candlestick chart.
- **Paper trade** the same rules live during market hours.
- **Account**: your profile and funds.

**No real orders are ever sent.** The app wraps the Groww client in a read-only
wrapper that only allows historical-data calls; there is no live-trading code
or switch.

## Project layout

```
app/
  strategy/         Pure strategy rules (bars, EMA/crossovers, strikes, costs,
                    trade state machine, metrics). Shared by backtest + paper.
  backtest.py       Downloader (Groww -> Postgres) and the full backtest grid
  paper.py          Paper trading engine
  groww_data.py     Read-only Groww data client (rate limit + retries)
  db.py             Postgres tables (SQLAlchemy)
  dashboard.py      Backtest / Data / Paper pages and JSON APIs
  groww_client.py   TOTP login, token caching until 6:00 AM IST
  routes.py         Account page, /login, /logout, /health
tests/              pytest (fake Groww API; SQLite and real Postgres)
.devcontainer/      Codespaces: app container + Postgres 16
```

## Strategy rules (as coded)

- **Bars**: NIFTY 1m candles resampled to 5/15/30m, aligned to 09:15. EMAs (9,
  21; SMA-seeded) run continuously across days.
- **Cross**: EMA9 - EMA21 (rounded to 0.01) changes sign versus the last
  non-zero sign. A spread of exactly 0 is a tie and never a cross by itself.
- **Signal time** = close time of the signal bar. **Fill** = open of the
  option's 1m candle starting at that minute. Missing candle: last close within
  5 minutes, otherwise the entry is skipped and shown in the day log.
- **Window**: start <= signal time <= cutoff (inclusive). Cross number counts
  crosses inside the window.
- **Exit**: opposite cross (filled the same way) or 15:00 square-off (open of
  the 15:00 candle). Opposite cross inside the window also opens the reverse
  trade with a fresh strike; after the cutoff it only closes.
- **Modes**: ITM5 buy (CE ATM-250 / PE ATM+250), ATM buy, ATM sell (bullish ->
  sell PE, bearish -> sell CE). ATM = spot close rounded to nearest 50 (half
  rounds up). Nearest weekly expiry, including the expiry day itself (0 DTE).
- **Size**: 65 x 1 lot (editable on the Data page).
- **Slippage**: buy = price x 1.005 + 0.5 rounded up to 0.05; sell = price x
  0.995 - 0.5 rounded down, floored at 0.05.
- **Charges** (`app/strategy/costs.py`, dated): Rs 20/order brokerage, STT on
  sell premium (0.0625% before Oct 2024, 0.1% to Mar 2026, 0.15% from Apr
  2026), NSE 0.03503%, SEBI Rs 10/crore, stamp 0.003% on buys, GST 18%.
- **Whipsaw**: closed by the opposite cross within 2 signal bars.
- Gross PnL uses raw prices; friction = slippage + charges; net = gross - friction.

## Run it from your phone (GitHub Codespaces)

No computer needed. Codespaces runs the app in the cloud and opens it in your
phone's browser.

1. **Add your keys as secrets** (once): on github.com go to your profile picture,
   **Settings → Codespaces → New secret**. Add `GROWW_API_KEY`,
   `GROWW_TOTP_SECRET` and `FLASK_SECRET_KEY`, and give each one access to this
   repository.
2. **Start it**: open this repo on github.com, tap **Code → Codespaces →
   Create codespace on main**. The first start takes a few minutes.
3. The app and its Postgres database start by themselves. Open the **Ports**
   tab, tap the globe icon next to port 5000, and tap **Connect to Groww**.
4. Go to **Data**, pick dates, tap **Download & run**. When it finishes, tap
   **Open the dashboard**.

**Already have a codespace from before?** It needs one rebuild to get
Postgres: on github.com/codespaces tap **⋯** next to it → **Delete**, then
create a new one as in step 2 (or, in the codespace, open the command palette
and run **Codespaces: Rebuild Container**). Data lives inside the codespace:
stopping keeps it, deleting it removes it (use the CSV export on the Data page).

For paper trading, keep the codespace tab open during market hours. Codespaces
stop after 30 idle minutes by default; you can raise this up to 4 hours in
GitHub **Settings → Codespaces → Default idle timeout**.

Keep port 5000 **Private** (the default) so only you can open it. Stop the
codespace when you're done (github.com/codespaces) to save free hours.

## 1. Get your Groww TOTP key

1. Go to <https://groww.in/trade-api/api-keys> and create a key with the
   **TOTP** method (you need an active Trade API subscription).
2. You get two values: the **API key** (a long token) and the **TOTP secret**
   (the base32 text behind the QR code). These are what you have in `keys.txt`.

## 2. Set up on your computer

Needs Python 3.10 or newer and a Postgres database (set `DATABASE_URL`, e.g.
`postgresql+psycopg://postgres:postgres@localhost:5432/trading`).

```bash
git clone https://github.com/rahulkapoor1926-ai/Groww-Trading-Project.git
cd Groww-Trading-Project

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pre-commit install                 # optional: auto-format on commit

cp .env.example .env               # Windows: copy .env.example .env
```

Open `.env` and paste your values from `keys.txt`:

```
GROWW_API_KEY=<your API key>
GROWW_TOTP_SECRET=<your TOTP secret>
FLASK_SECRET_KEY=<any random string>
```

`.env` and `keys.txt` are in `.gitignore`, so they are never committed.

## 3. Run it

```bash
python run.py
```

Open <http://127.0.0.1:5000> and tap **Connect to Groww**. The app generates
the current TOTP code, exchanges it for an access token, and shows your
profile and funds. The token is kept in memory until 6:00 AM IST, when Groww
expires it; after that the app logs in again automatically.

## Dev tools

| Command | What it does |
| --- | --- |
| `pytest` | Run tests (set `TEST_DATABASE_URL` to also test on Postgres) |
| `ruff check .` | Lint |
| `black .` | Format |
| `pre-commit run --all-files` | Run lint + format hooks |

GitHub Actions runs lint, format check and tests on every push.

## Next ideas

Holdings and positions pages. Live order placement is deliberately out of
scope.

> This app only reads account data. Keep it on your own machine and never
> expose it to the internet: anyone who can open the page can use your account.
