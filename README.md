# FALARON — Telegram SMM storefront

A Telegram bot **and** Mini App for reselling SMM-panel services, with a ledger-based wallet, verified
online payments (Razorpay) and safe provider synchronisation.

* **Bot** (aiogram 3): onboarding + consent, catalog, orders, wallet, referrals, reseller/partner programmes, admin panel.
* **Mini App** (`app/webapp/`, plain HTML/JS, no build step) served by the same process at `/app/`.
* **API** (FastAPI): Telegram `initData`-authenticated JSON API, payment webhooks, `/health`, `/health/ready`.
* **Workers**: order status sync, provider health, catalog sync, payment reconciliation, stuck-order detection.
* **Operator CLI**: `python -m app.cli --help` (providers, sync, activate services, resolve held orders, settings).

**Deploy it → [DEPLOY.md](DEPLOY.md).** What changed and what is / isn't verified → [HANDOVER.md](HANDOVER.md).

## Layout

```
app/
  __main__.py        entrypoint (production config gate, API + bot + workers)
  config.py          settings + production validation
  models.py db.py    SQLAlchemy models; additive schema upgrade (SQLite + PostgreSQL)
  wallet.py          immutable ledger, per-user row locking, idempotent credit/debit/reserve/capture
  orders.py          order engine (reserve → provider → capture; review hold; refunds capped)
  pricing.py         markup layers, reseller discount, coupons that cannot breach the margin floor
  payments/          gateway interface, Razorpay, deposit service (webhook/reconcile/manual)
  sync.py            provider catalog sync (validated, inactive-by-default, never trusts bad data)
  adapters/          PerfectPanel-compatible client (+ demo adapter, blocked in production)
  api/               FastAPI app, Mini App routes, webhooks, rate limits
  bot/               Telegram handlers and keyboards
  webapp/            Mini App front-end
  cli.py             operator commands
tests/               unit tests (no deps) + money-path integration tests (pytest)
deploy/Caddyfile     HTTPS reverse proxy
docker-compose.yml   PostgreSQL + app + Caddy
scripts/             backup.sh, restore.sh
```

## Quick local run (demo mode, SQLite, fake provider and fake payments)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env      # set BOT_TOKEN and OWNER_TELEGRAM_ID, uncomment the dev block at the bottom
python -m app
```

Demo mode exists only when `APP_ENV=development`. With `APP_ENV=production` the process refuses to start
if demo payments are enabled, the database is SQLite, `SECRET_KEY` is missing, or gateway credentials are absent.

## Tests

```bash
python -m unittest tests.test_pure -v        # 28 dependency-free tests (signatures, webhooks, pricing, SSRF, sync validation)
pytest -q                                   # + money-path integration tests (needs requirements-dev.txt)
```
