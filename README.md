# Groww Trading Project

A personal Python + Flask trading dashboard that connects to your Groww account
through the official [Groww Trade API](https://groww.in/trade-api/docs/python-sdk)
using the **TOTP** login flow. The first page shows your profile and your
funds / margins.

## Project layout

```
app/
  __init__.py       Flask app factory (loads .env)
  groww_client.py   TOTP login, token caching until 6:00 AM IST
  routes.py         Dashboard, /login, /logout, /health
  templates/        HTML pages
  static/           CSS
tests/              pytest tests (use a fake Groww client, no network)
run.py              Start the dev server
```

## Run it from your phone (GitHub Codespaces)

No computer needed. Codespaces runs the app in the cloud and opens it in your
phone's browser.

1. **Add your keys as secrets** (once): on github.com go to your profile picture,
   **Settings → Codespaces → New secret**. Add `GROWW_API_KEY`,
   `GROWW_TOTP_SECRET` and `FLASK_SECRET_KEY`, and give each one access to this
   repository.
2. **Start it**: open this repo on github.com, tap **Code → Codespaces →
   Create codespace on main**. The first start takes a few minutes.
3. The app starts by itself. Open the **Ports** tab, tap the globe icon next to
   port 5000, and tap **Connect to Groww**.

Keep port 5000 **Private** (the default) so only you can open it. Stop the
codespace when you're done (github.com/codespaces) to save free hours.

## 1. Get your Groww TOTP key

1. Go to <https://groww.in/trade-api/api-keys> and create a key with the
   **TOTP** method (you need an active Trade API subscription).
2. You get two values: the **API key** (a long token) and the **TOTP secret**
   (the base32 text behind the QR code). These are what you have in `keys.txt`.

## 2. Set up on your computer

Needs Python 3.10 or newer.

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
| `pytest` | Run tests |
| `ruff check .` | Lint |
| `black .` | Format |
| `pre-commit run --all-files` | Run lint + format hooks |

GitHub Actions runs lint, format check and tests on every push.

## Next ideas

Holdings and positions pages (`get_holdings_for_user`, `get_positions_for_user`),
live quotes with `GrowwFeed`, order placement, and a backtesting module.

> This app only reads account data. Keep it on your own machine and never
> expose it to the internet: anyone who can open the page can use your account.
