"""Operator CLI:  python -m app.cli <command>   (inside Docker: docker compose exec app python -m app.cli ...)

Everything here is audited. Provider keys are read from a hidden prompt (or --key-env), never echoed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import func, select

from app.audit import audit
from app.config import configure_logging, get_settings
from app.db import dispose_engine, init_db, session_scope
from app.models import Category, Order, Payment, Provider, Service, User
from app.net import UnsafeUrl, validate_provider_url
from app.security import encrypt_secret

ACTOR = "cli"


def _die(msg: str, code: int = 1) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    raise SystemExit(code)


async def cmd_check_config(_: argparse.Namespace) -> None:
    s = get_settings()
    problems, warnings = s.production_problems(), s.production_warnings()
    print(f"APP_ENV={s.app_env}  gateway={s.payment_gateway}  db={'sqlite' if s.is_sqlite else 'postgres'}")
    for w in warnings:
        print(f"WARNING: {w}")
    for p in problems:
        print(f"PROBLEM: {p}")
    if problems:
        raise SystemExit(1)
    print("OK — configuration passes production checks." if s.is_production else "OK (development mode; production checks skipped).")


async def cmd_add_provider(a: argparse.Namespace) -> None:
    settings = get_settings()
    try:
        url = validate_provider_url(
            a.url, allow_private=settings.allow_private_provider_urls or not settings.is_production,
            require_https=settings.is_production,
        )
    except UnsafeUrl as exc:
        _die(f"Rejected URL: {exc}")
    key = os.environ.get(a.key_env, "").strip() if a.key_env else getpass.getpass("Provider API key (hidden): ").strip()
    if not key:
        _die("No API key provided.")
    await init_db(settings)
    async with session_scope() as session:
        row = await session.get(Provider, a.id)
        created = row is None
        if row is None:
            row = Provider(id=a.id, name=a.name or a.id)
            session.add(row)
        row.name = a.name or row.name
        row.adapter_type = "perfectpanel"
        row.base_url = url
        row.encrypted_api_key = encrypt_secret(key, settings)
        row.fx_to_inr = float(a.fx)
        row.auto_activate_new = bool(a.auto_activate)
        row.is_active = True
        await audit(session, actor=ACTOR, action="provider.upsert", target=a.id,
                    new={"url": url, "fx_to_inr": a.fx, "auto_activate_new": bool(a.auto_activate)})
    from app.catalog import reset_adapter_cache

    reset_adapter_cache()
    print(("Created" if created else "Updated") + f" provider {a.id}. Next: python -m app.cli sync --provider {a.id}")


async def cmd_sync(a: argparse.Namespace) -> None:
    from app.sync import sync_all, sync_provider

    await init_db()
    async with session_scope() as session:
        if a.provider:
            prov = await session.get(Provider, a.provider)
            if prov is None:
                _die(f"Unknown provider {a.provider}")
            reports = [await sync_provider(session, prov)]
        else:
            reports = await sync_all(session)
    for r in reports:
        print(r.summary())
        for c in r.material()[:25]:
            print("  " + c.line())
    print("New services are INACTIVE. Enable them with: python -m app.cli activate --provider ID --all-supported")


async def cmd_services(a: argparse.Namespace) -> None:
    await init_db()
    async with session_scope() as session:
        stmt = select(Service).order_by(Service.category_id, Service.name).limit(a.limit)
        if a.provider:
            stmt = stmt.where(Service.provider_id == a.provider)
        if a.inactive:
            stmt = stmt.where(Service.is_active.is_(False))
        if a.search:
            stmt = stmt.where(Service.name.ilike(f"%{a.search}%"))
        for s in (await session.execute(stmt)).scalars().all():
            flag = "ON " if s.is_active else "off"
            print(f"{flag} {s.id:<24} {s.service_type:<9} {s.rate_per_1000_paise / 100:>9.2f}/1k  {s.min_qty}-{s.max_qty}  {s.name[:60]}")


async def _toggle(a: argparse.Namespace, active: bool) -> None:
    from app.sync_normalize import SUPPORTED_TYPES

    await init_db()
    async with session_scope() as session:
        stmt = select(Service)
        if a.ids:
            stmt = stmt.where(Service.id.in_(a.ids))
        elif a.provider:
            stmt = stmt.where(Service.provider_id == a.provider)
            if a.category:
                stmt = stmt.join(Category, Category.id == Service.category_id).where(Category.name.ilike(f"%{a.category}%"))
            if not (a.all_supported or a.category):
                _die("Refusing to touch a whole provider without --all-supported or --category.")
        else:
            _die("Give --ids or --provider.")
        rows = (await session.execute(stmt)).scalars().all()
        changed = 0
        for svc in rows:
            if active and (svc.service_type not in SUPPORTED_TYPES or svc.is_removed_upstream):
                continue
            if svc.is_active != active:
                svc.is_active = active
                changed += 1
        # A category is visible only while it has at least one active service.
        for cid in {s.category_id for s in rows}:
            cat = await session.get(Category, cid)
            n = (await session.execute(select(func.count()).select_from(Service).where(
                Service.category_id == cid, Service.is_active.is_(True)))).scalar_one()
            if cat is not None:
                cat.is_active = n > 0
        await audit(session, actor=ACTOR, action="service.activate" if active else "service.deactivate",
                    new={"count": changed, "provider": a.provider, "category": a.category})
    print(f"{'Activated' if active else 'Deactivated'} {changed} service(s).")


async def cmd_activate(a: argparse.Namespace) -> None:
    await _toggle(a, True)


async def cmd_deactivate(a: argparse.Namespace) -> None:
    await _toggle(a, False)


async def cmd_resolve_order(a: argparse.Namespace) -> None:
    from app.orders import OrderError, resolve_review_order

    if bool(a.release) == bool(a.attach):
        _die("Choose exactly one of --release or --attach PROVIDER_ORDER_ID")
    await init_db()
    async with session_scope() as session:
        order = (await session.execute(select(Order).where(Order.public_id == a.order_id))).scalar_one_or_none()
        if order is None:
            _die("Order not found")
        try:
            await resolve_review_order(session, order, release_funds=a.release, attach_provider_order_id=a.attach, actor=ACTOR)
        except OrderError as exc:
            _die(str(exc))
        print(f"{order.public_id} -> {order.status}")


async def cmd_confirm_payment(a: argparse.Namespace) -> None:
    from app.payments.service import manual_confirm

    await init_db()
    async with session_scope() as session:
        payment = (await session.execute(select(Payment).where(Payment.public_id == a.payment_id))).scalar_one_or_none()
        if payment is None:
            _die("Payment not found")
        if payment.method != "manual":
            _die("Only manual-gateway invoices can be confirmed by hand.")
        out = await manual_confirm(session, a.payment_id, actor=ACTOR)
        print(out.outcome)


async def cmd_promote(a: argparse.Namespace) -> None:
    await init_db()
    async with session_scope() as session:
        user = await session.get(User, int(a.telegram_id))
        if user is None:
            _die("User has not started the bot yet.")
        old = user.role
        user.role = a.role
        await audit(session, actor=ACTOR, action="user.role", target=str(user.telegram_id), old=old, new=a.role)
        print(f"{user.telegram_id}: {old} -> {a.role}")


SETTABLE = {
    "payment_instructions": "Text shown to users who must pay you manually (PAYMENT_GATEWAY=manual)",
    "support_url": "Support link shown in the bot",
    "brand_name": "Brand name shown in the bot / Mini App",
    "global_markup_percent": "Default markup over provider cost, e.g. 25",
    "min_margin_percent": "Never sell below cost + this percent, e.g. 5",
}


async def cmd_set_setting(a: argparse.Namespace) -> None:
    from app.models import AppSetting
    from app.security import utcnow

    if a.key not in SETTABLE:
        _die("Settable keys:\n" + "\n".join(f"  {k:<24} {v}" for k, v in SETTABLE.items()))
    value = a.value
    if a.key in {"global_markup_percent", "min_margin_percent"}:
        try:
            if float(value) < 0 or float(value) > 1000:
                raise ValueError
        except ValueError:
            _die("Value must be a number between 0 and 1000")
    await init_db()
    async with session_scope() as session:
        row = await session.get(AppSetting, a.key)
        old = row.value if row is not None else None
        if row is None:
            session.add(AppSetting(key=a.key, value=value, updated_at=utcnow()))
        else:
            row.value = value
            row.updated_at = utcnow()
        await audit(session, actor=ACTOR, action="setting.set", target=a.key, old=old, new=value)
    print(f"{a.key} updated.")


async def cmd_stats(_: argparse.Namespace) -> None:
    await init_db()
    async with session_scope() as session:
        async def n(model, *where):
            return (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()

        print("users:", await n(User))
        print("orders:", await n(Order), " under review:", await n(Order, Order.status == "review"))
        print("services:", await n(Service), " active:", await n(Service, Service.is_active.is_(True)))
        print("payments paid:", await n(Payment, Payment.status == "paid"), " need review:", await n(Payment, Payment.status == "review"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-config", help="validate environment for production").set_defaults(fn=cmd_check_config)

    ap = sub.add_parser("add-provider", help="add/update a PerfectPanel-compatible SMM provider")
    ap.add_argument("--id", required=True, help="short id, e.g. prov_main")
    ap.add_argument("--name")
    ap.add_argument("--url", required=True, help="https://panel.example.com/api/v2")
    ap.add_argument("--key-env", help="read the API key from this env var instead of prompting")
    ap.add_argument("--fx", type=float, default=1.0, help="provider currency -> INR multiplier (e.g. 83 for USD)")
    ap.add_argument("--auto-activate", action="store_true", help="make newly synced services sellable immediately (not recommended)")
    ap.set_defaults(fn=cmd_add_provider)

    sp = sub.add_parser("sync", help="sync provider catalog(s)")
    sp.add_argument("--provider")
    sp.set_defaults(fn=cmd_sync)

    lp = sub.add_parser("services", help="list services")
    lp.add_argument("--provider")
    lp.add_argument("--inactive", action="store_true")
    lp.add_argument("--search")
    lp.add_argument("--limit", type=int, default=100)
    lp.set_defaults(fn=cmd_services)

    for name, fn in (("activate", cmd_activate), ("deactivate", cmd_deactivate)):
        x = sub.add_parser(name, help=f"{name} services")
        x.add_argument("--ids", nargs="*")
        x.add_argument("--provider")
        x.add_argument("--category", help="category name contains …")
        x.add_argument("--all-supported", action="store_true")
        x.set_defaults(fn=fn)

    ro = sub.add_parser("resolve-order", help="resolve an order held for review")
    ro.add_argument("order_id")
    ro.add_argument("--release", action="store_true", help="provider never got it: refund the customer")
    ro.add_argument("--attach", metavar="PROVIDER_ORDER_ID", help="provider did get it: charge and track")
    ro.set_defaults(fn=cmd_resolve_order)

    cp = sub.add_parser("confirm-payment", help="credit a manual-gateway deposit")
    cp.add_argument("payment_id")
    cp.set_defaults(fn=cmd_confirm_payment)

    pr = sub.add_parser("set-role", help="change a user's role")
    pr.add_argument("telegram_id")
    pr.add_argument("role", choices=["user", "admin"])
    pr.set_defaults(fn=cmd_promote)

    st = sub.add_parser("set-setting", help="change a business setting (keys: " + ", ".join(SETTABLE) + ")")
    st.add_argument("key")
    st.add_argument("value")
    st.set_defaults(fn=cmd_set_setting)

    sub.add_parser("stats", help="quick platform counters").set_defaults(fn=cmd_stats)
    return p


async def _amain(args: argparse.Namespace) -> None:
    configure_logging("WARNING")
    try:
        await args.fn(args)
    finally:
        await dispose_engine()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
