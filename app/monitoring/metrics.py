"""Prometheus metrics — 30 metrics across 6 subsystems."""
from __future__ import annotations
import contextlib, time
from typing import Generator
from prometheus_client import Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest

# Orders
orders_created           = Counter("smm_orders_created_total",              "Orders created",            ["source","status"])
orders_completed         = Counter("smm_orders_completed_total",            "Orders terminal status",    ["terminal_status"])
order_processing_duration= Histogram("smm_order_processing_duration_seconds","Order processing time",    ["source"], buckets=(60,300,600,1800,3600,7200,86400))
orders_auto_refunded     = Counter("smm_orders_auto_refunded_total",        "Auto-refunded orders")
orders_active_gauge      = Gauge("smm_orders_active",                       "Active orders")

# Payments
payments_initiated  = Counter("smm_payments_initiated_total",  "Payment intents created",   ["provider"])
payments_verified   = Counter("smm_payments_verified_total",   "Payments verified",         ["provider","path"])
payments_failed     = Counter("smm_payments_failed_total",     "Payments failed",           ["provider"])
payments_reconciled = Counter("smm_payments_reconciled_total", "Payments reconciled",       ["provider"])
payment_amount_inr  = Histogram("smm_payment_amount_inr",     "Payment amounts INR",        buckets=(10,50,100,250,500,1000,2000,5000,10000,50000,100000))

# Wallet
wallet_credits         = Counter("smm_wallet_credits_total",            "Wallet credits",        ["tx_type"])
wallet_debits          = Counter("smm_wallet_debits_total",             "Wallet debits",         ["tx_type"])
wallet_credit_amount   = Counter("smm_wallet_credit_amount_inr_total",  "Total INR credited")
wallet_debit_amount    = Counter("smm_wallet_debit_amount_inr_total",   "Total INR debited")
ledger_violations      = Counter("smm_ledger_invariant_violations_total","Ledger violations")

# Provider API
provider_api_requests  = Counter("smm_provider_api_requests_total",           "Provider API calls",      ["provider_id","action","status"])
provider_api_duration  = Histogram("smm_provider_api_request_duration_seconds","Provider API latency",   ["provider_id","action"], buckets=(0.1,0.25,0.5,1.0,2.5,5.0,10.0,30.0))
provider_cb_state      = Gauge("smm_provider_circuit_breaker_state",          "Circuit breaker state",   ["provider_id"])
provider_services_synced=Counter("smm_provider_services_synced_total",        "Services synced",         ["provider_id","operation"])
provider_sync_duration = Histogram("smm_provider_sync_duration_seconds",       "Provider sync time",      ["provider_id"], buckets=(1,5,10,30,60,120,300))

# Bot
bot_updates         = Counter("smm_bot_updates_total",             "Bot updates",           ["update_type"])
bot_handler_duration= Histogram("smm_bot_handler_duration_seconds","Bot handler time",      ["handler"], buckets=(0.05,0.1,0.25,0.5,1.0,2.5,5.0))
bot_rate_limits     = Counter("smm_bot_rate_limit_hits_total",    "Bot rate limit hits")
bot_active_users    = Gauge("smm_bot_active_users_24h",           "Active users 24h")

# API / System
api_requests    = Counter("smm_api_requests_total",             "API requests",           ["method","endpoint","status_code"])
api_duration    = Histogram("smm_api_request_duration_seconds", "API request latency",    ["method","endpoint"], buckets=(0.01,0.025,0.05,0.1,0.25,0.5,1.0,2.5,5.0))
api_errors      = Counter("smm_api_errors_total",               "API 5xx errors",         ["endpoint"])
worker_job_runs = Counter("smm_worker_job_runs_total",          "Worker job runs",        ["job_name","status"])
worker_job_dur  = Histogram("smm_worker_job_duration_seconds",  "Worker job time",        ["job_name"], buckets=(0.1,1,5,30,60,300,600,3600))
backup_runs     = Counter("smm_backup_runs_total",              "Backup runs",            ["status"])
backup_size     = Histogram("smm_backup_size_bytes",            "Backup file size",       buckets=(1024**2,10*1024**2,50*1024**2,100*1024**2,500*1024**2,1024**3))


def generate_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


@contextlib.contextmanager
def track_duration(histogram: Histogram, **labels) -> Generator[None, None, None]:
    start = time.monotonic()
    try:
        yield
    finally:
        histogram.labels(**labels).observe(time.monotonic() - start)


def record_order_created(source: str, success: bool) -> None:
    orders_created.labels(source=source, status="submitted" if success else "failed").inc()


def record_payment_verified(provider: str, path: str) -> None:
    payments_verified.labels(provider=provider, path=path).inc()
    if path == "reconciler":
        payments_reconciled.labels(provider=provider).inc()


def record_wallet_operation(tx_type: str, amount: float, is_credit: bool) -> None:
    if is_credit:
        wallet_credits.labels(tx_type=tx_type).inc()
        wallet_credit_amount.inc(amount)
    else:
        wallet_debits.labels(tx_type=tx_type).inc()
        wallet_debit_amount.inc(amount)


def update_circuit_breaker_state(provider_id: int, state: str) -> None:
    provider_cb_state.labels(provider_id=str(provider_id)).set({"closed":0,"degraded":1,"open":2}.get(state, 0))
