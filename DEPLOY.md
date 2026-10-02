# FALARON — Deployment Guide

Target: one small Linux VPS (1 vCPU / 1–2 GB RAM is enough to start), Docker, a domain name.
Stack: PostgreSQL 16 · the app (Telegram bot + Mini App API + workers, one process) · Caddy (automatic HTTPS).

> **Read §7 (Before you take real money) before going live.** The code enforces the dangerous parts
> (it refuses to boot in production with demo payments, SQLite, or a missing encryption key), but a
> payment integration must be proven in the gateway's *test mode* first. That step is yours.

---------------------------------------------------------------------------------------------------

## 1. What you need

| Item | Where |
|---|---|
| Bot token | Telegram → **@BotFather** → `/newbot` |
| Your numeric Telegram id | Telegram → **@userinfobot** |
| A domain pointing at the server | DNS **A/AAAA** record → server IP (must resolve *before* first start, Caddy needs it for HTTPS) |
| Razorpay account (test keys first) | https://dashboard.razorpay.com — or use `PAYMENT_GATEWAY=manual` |
| An SMM panel account with API access | Any PerfectPanel-compatible panel (`/api/v2`) |
| Public Terms + Privacy URLs | Required in production |

Server basics: install Docker + the compose plugin, allow only ports **22, 80, 443** in the firewall,
use SSH keys, keep the OS patched.

## 2. Install

```bash
unzip falaron-deploy.zip && cd falaron
cp .env.example .env
nano .env            # fill every section; comments explain each value
```

Generate the two secrets you must set:

```bash
openssl rand -hex 24                                    # -> POSTGRES_PASSWORD
docker run --rm python:3.12-slim sh -c "pip -q install cryptography && python -c \
 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"   # -> SECRET_KEY
```

**Back up `SECRET_KEY` somewhere safe (password manager).** It encrypts provider API keys in the
database. Lose it and those keys cannot be decrypted (you would re-enter them; nothing else breaks).
Never change it casually.

## 3. Start

```bash
docker compose up -d --build
docker compose logs -f app          # wait for: "bot polling started" and uvicorn "Application startup complete"
curl -s https://YOUR_DOMAIN/health/ready      # {"status":"ready","database":true}
```

If the app exits immediately it prints exactly what is wrong (e.g. `SECRET_KEY is not set`). Fix `.env`
and run `docker compose up -d` again. You can also check without starting:

```bash
docker compose run --rm app python -m app.cli check-config
```

## 4. Connect Telegram

1. @BotFather → your bot → **Bot Settings → Menu Button** → set URL to `https://YOUR_DOMAIN/app/`
   (optional; the bot's own "Open Mini App" button also uses `PUBLIC_BASE_URL`).
2. Open your bot, send `/start`, accept the terms. You are the owner (`OWNER_TELEGRAM_ID`); `/admin` opens
   the control panel.

## 5. Connect your SMM provider and publish services

```bash
# key is typed hidden, never printed or logged. FX: 1 if the panel prices in INR, ~83 if in USD.
docker compose exec app python -m app.cli add-provider --id prov_main --name "Main panel" \
    --url https://panel.example.com/api/v2 --fx 1

docker compose exec app python -m app.cli sync --provider prov_main
docker compose exec app python -m app.cli services --provider prov_main --search instagram
docker compose exec app python -m app.cli activate --provider prov_main --category "Instagram"
```

Rules the sync follows (so a provider can't surprise you):

* New services arrive **inactive** — nothing is sellable until *you* activate it.
* A broken/empty/collapsed provider response **never** replaces your last good catalog.
* Services removed upstream are deactivated, never deleted (order history stays intact).
* Price moves, limit changes and removals are summarised and sent to the owner in Telegram.
* Types the Mini App cannot render (packages, polls, subscriptions) are imported but cannot be activated.
* Selling price = provider cost × your markup, never below `MIN_MARGIN_PERCENT`; coupons can't push below it either.

Hourly sync is automatic (`PROVIDER_SYNC_INTERVAL_SECONDS`). `/sync` in the bot runs it now.

## 6. Razorpay (test mode first)

1. Dashboard → **Test Mode** → Settings → API Keys → put `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in `.env`.
2. Settings → **Webhooks → Add**:
   * URL: `https://YOUR_DOMAIN/webhooks/razorpay`
   * Secret: any long random string → the same value in `RAZORPAY_WEBHOOK_SECRET`
   * Events: `payment_link.paid`, `payment_link.expired`, `payment_link.cancelled`
     (optionally refund/dispute events — they raise an alert for manual handling; nothing is auto-refunded).
3. `docker compose up -d` (reload env), then in the bot: **Deposit → ₹100 → Pay securely**, pay with a
   Razorpay test card/UPI, and confirm the wallet is credited.
4. Redeliver the same webhook from the Razorpay dashboard → balance must **not** change a second time.

How money safety works: the wallet is credited only from a signature-verified webhook (HMAC over the raw
body) or from a direct status lookup against Razorpay — never from a user's say-so. Amount, currency,
reference and gateway must match the invoice; anything else is parked for manual review and you are alerted.
Every event id is stored once (replay-proof). A reconcile job re-checks pending invoices every 5 minutes, so a
lost webhook cannot lose a payment.

`PAYMENT_GATEWAY=manual` instead: users get an invoice id + your payment instructions; you verify the money
yourself, then `/confirmpay PAY-XXXXXXXX` in the bot (audited). Set the instructions text with:

```bash
docker compose exec app python -m app.cli set-setting payment_instructions "UPI: yourname@bank — quote the invoice id"
```

## 7. Before you take real money (checklist)

- [ ] `docker compose run --rm app python -m app.cli check-config` prints **OK** with no warnings you don't accept.
- [ ] A full test-mode cycle worked: deposit → webhook → balance → order → provider accepted → status updates.
- [ ] Webhook replay did not double-credit (step 6.4).
- [ ] An order with a *deliberately wrong* link/quantity was rejected cleanly; a provider outage gave a refund or a "review" hold, never a silent loss.
- [ ] Provider balance is funded and you set a low-balance habit (the health check shows it in `/admin → Providers`).
- [ ] Terms/Privacy pages exist and say what you actually do (refunds, delivery guarantees, data stored).
- [ ] Switched Razorpay to **Live** keys + a *live* webhook with its own secret; repeated a ₹100 real test.
- [ ] `WELCOME_BONUS_PAISE=0` (default in production) unless you deliberately give free credit.
- [ ] A backup ran and you restored it once on a scratch machine (§9).
- [ ] You can reach the server and know how to read logs (§8).

Legal/tax/compliance (GST, KYC, platform rules of the networks you sell for, Telegram's Terms for bots) is
your responsibility; the software enforces consent capture and keeps an audit trail, nothing more.

## 8. Operating it

```bash
docker compose logs -f app                 # live logs (secrets are never logged)
docker compose ps                          # health
docker compose exec app python -m app.cli stats
docker compose exec app python -m app.cli services --inactive --provider prov_main
docker compose exec app python -m app.cli deactivate --ids prov_main-123 prov_main-456
```

**Orders under review** — if the provider times out *after* we submitted, the outcome is unknown, so funds
stay held and you get a Telegram alert. Check the provider's panel, then:

```bash
docker compose exec app python -m app.cli resolve-order FL-XXXXXX --release          # provider never got it -> refund
docker compose exec app python -m app.cli resolve-order FL-XXXXXX --attach 1234567   # provider has it -> charge & track
```

**Payments under review** (amount/currency mismatch, refund/dispute) are never auto-resolved: inspect in the
Razorpay dashboard, then credit/adjust via `/admin → Credit/Debit` (audited).

Kill switches in `/admin` (new orders, deposits, refills, read-only, maintenance) take effect immediately.

Update to a new version: replace the project files, keep `.env`, then `docker compose up -d --build`.
Tables are created and new columns are added automatically (additive only; nothing is dropped).

## 9. Backups

```bash
./scripts/backup.sh          # writes backups/falaron-<timestamp>.sql.gz, keeps 14 days
crontab -e                   # 15 3 * * *  cd /opt/falaron && ./scripts/backup.sh
./scripts/restore.sh backups/falaron-<timestamp>.sql.gz     # DESTRUCTIVE, asks for confirmation
```

Copy backups **off the server** (object storage / another machine) — a backup on the same disk dies with it.
Keep `.env` (esp. `SECRET_KEY`) backed up separately from the database dump.

## 10. Local development (no Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env     # set BOT_TOKEN, OWNER_TELEGRAM_ID; uncomment the dev block at the bottom
python -m app            # SQLite + demo provider + demo payments; Mini App at http://localhost:8080/app/
pytest -q                # integration tests;  python -m unittest tests.test_pure   # dependency-free unit tests
```

Telegram only opens Mini Apps over HTTPS, so for the Mini App itself use a tunnel (e.g. `cloudflared tunnel`)
and set `PUBLIC_BASE_URL` to its URL. The bot works without it.

## 11. Known limits (by design, for a single-server launch)

* One app process. Rate limits and bot conversation state live in memory, so **do not run multiple replicas**
  (move them to Redis first). A restart loses in-flight wizard steps only, never money state.
* The bot uses long polling (no public bot webhook to secure). Payment webhooks are public and signature-checked.
* Razorpay is the only automatic gateway; `manual` is the fallback. Telegram Stars / crypto are not built in.
* The Mini App is a small dependency-free SPA in `app/webapp/` (no build step). Restyle it freely.
