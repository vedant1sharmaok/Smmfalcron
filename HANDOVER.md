# Handover: what was done, what was verified, what is still yours

## Starting point
The workspace contained a working **Python** bot + API (`falaron-smm-bot`) that was a demo: hard-wired mock
payments (anyone could "pay" with no money), a seeded fake catalog, a fake provider, SQLite only. The blueprint
(v2.2) asks for a production-grade platform. This delivery turns that codebase into a deployable one.
The TanStack/Grok web prototype in the workspace (`src/`, Vercel/Grok auth, in-browser engine) is a separate app
that is not wired to this backend; it is **not** part of the deployable. The Mini App now ships inside the
Python service instead (`app/webapp/`).

## Main changes
**Payments** — mock payments can no longer exist in production (startup refuses; handlers and routes hard-gated).
New gateway layer + Razorpay Payment Links: HMAC-verified webhooks over the raw body, amount/currency/reference/gateway
matching, replay protection (`payment_events` unique key), periodic reconcile against the gateway, mismatch/refund/dispute
parked for manual review, manual-confirm gateway as fallback, full audit trail.

**Wallet & orders** — per-user row locking (`SELECT … FOR UPDATE` on PostgreSQL) so concurrent orders can't overspend;
race-safe user creation; money columns widened to BigInteger; client idempotency keys namespaced per user; derived keys
time-bucketed (double-tap deduped, genuine repeat orders allowed); ambiguous provider timeouts **hold** funds in a
`review` state instead of refunding (prevents free deliveries) with owner alert + `resolve-order` CLI; coupon use returned
when an order fails; refunds capped to what was actually charged; unconfirmed provider cancels no longer refund;
terms acceptance enforced server-side.

**Pricing** — coupons can no longer push a sale below the minimum-margin floor; provider cost and reseller internals are
never sent to clients; catalog endpoint is paged (no per-service queries for whole catalog).

**Provider sync** (new) — validated row-by-row, FX conversion, new services inactive by default, bad/collapsed responses
never replace the last good catalog, upstream removals deactivate (never delete), admin-edited names preserved, owner digest.
SSRF guard on provider URLs, redirects disabled for provider calls, demo adapter blocked in production.

**Security/ops** — production config validator; `SECRET_KEY` mandatory in production (no token-derived key); `initData`
`auth_date` mandatory; per-user + per-IP + order-placement rate limits and bot flood throttle; request-size caps; CSP/HSTS/
nosniff headers; docs disabled in production; `/health` + `/health/ready`; audit log table; additive schema upgrade for
SQLite and PostgreSQL; Docker (non-root, healthcheck), compose with PostgreSQL + Caddy auto-HTTPS, backup/restore scripts.

**Mini App** — dependency-free SPA: catalog, quote, order, orders (refresh/cancel/refill), wallet, deposits with payment polling.
All server text rendered via `textContent` (no HTML injection).

## Verification status — please read
The build sandbox had **no network**, so the third-party packages (aiogram, FastAPI, SQLAlchemy, pydantic, httpx, asyncpg)
could not be installed and **the application was never booted end-to-end here**.

Verified here:
* every Python module compiles (`py_compile`), and a custom static pass found **no unresolved `app.*` imports and no
  undefined names** (the checker was itself validated against a planted bug);
* 28 unit tests pass for the pure logic: HMAC signing, Razorpay webhook parsing/rejection cases, SSRF guard,
  rate limiter, margin/coupon arithmetic (including a property sweep), provider-row validation;
* Mini App JavaScript passes `node --check`.

Written but **not executed here**: `tests/test_money_flows.py` (webhook exactly-once, mismatch hold, wrong-ref/gateway,
deposit limits, double-tap, wallet overspend) — run `pytest -q` first thing in your environment. Also unexecuted: the
Razorpay HTTP calls (field names follow Razorpay's public Payment Links/webhook docs), PostgreSQL-specific behaviour
(row locks, schema upgrade DDL), Caddy/Docker. Treat the first deploy as a staged rollout: test mode → small live payment.

## Not built / your decisions
* Other gateways (Stars, crypto, cards via Stripe), bot-webhook mode, Redis, multi-replica operation, i18n.
* A web admin dashboard (admin is the bot panel + CLI).
* Legal text, GST/KYC, and the content rules of the networks you sell for.
* Choosing and testing a real SMM provider; margins and pricing strategy.
