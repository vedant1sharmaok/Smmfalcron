"""
ARQ background worker entrypoint — all 7 cron jobs.
Section 32-33 of blueprint.
"""
from __future__ import annotations
from app.core.logging import configure_logging, get_logger
from app.core.database import AsyncSessionLocal

logger = get_logger(__name__)


async def startup(ctx: dict) -> None:
    configure_logging()
    from app.core.redis import init_redis
    from app.providers.registry import registry

    await init_redis()
    async with AsyncSessionLocal() as db:
        count = await registry.load_providers(db)
        logger.info("worker_providers_loaded", count=count)

    from app.workers.order_monitor    import OrderMonitor
    from app.workers.payment_reconciler import PaymentReconciler
    from app.workers.backup_worker    import BackupWorker

    ctx["order_monitor"]      = OrderMonitor(registry)
    ctx["payment_reconciler"] = PaymentReconciler()
    ctx["backup_worker"]      = BackupWorker()
    logger.info("worker_ready")


async def shutdown(ctx: dict) -> None:
    from app.core.redis import close_redis
    from app.providers.registry import registry
    await registry.shutdown()
    await close_redis()


async def run_order_monitor(ctx: dict) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            result = await ctx["order_monitor"].run_once(db)
            await db.commit()
        logger.info("job_order_monitor_complete", **result)
        return result
    except Exception as exc:
        logger.error("job_order_monitor_failed", error=str(exc))
        raise


async def run_payment_reconciler(ctx: dict) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            result = await ctx["payment_reconciler"].run_once(db)
            await db.commit()
        logger.info("job_payment_reconciler_complete", **result)
        return result
    except Exception as exc:
        logger.error("job_payment_reconciler_failed", error=str(exc))
        raise


async def run_provider_sync_all(ctx: dict) -> dict:
    try:
        from app.sync.engine import ProviderSyncEngine, build_change_report
        from app.providers.registry import registry
        engine = ProviderSyncEngine(registry)
        async with AsyncSessionLocal() as db:
            results = await engine.sync_all_providers(db)
            await db.commit()
        report = build_change_report(results)
        # Telegram log
        try:
            from app.notifications.telegram_logger import tg_logger
            for r in results:
                from app.core.database import AsyncSessionLocal as ASL
                async with ASL() as db:
                    from app.core.models import Provider
                    p = await db.get(Provider, r.provider_id)
                    if p:
                        await tg_logger.provider_sync_complete(
                            p.name, r.inserted, r.updated, r.deactivated, len(r.errors)
                        )
        except Exception:
            pass
        logger.info("job_provider_sync_complete", **report)
        return report
    except Exception as exc:
        logger.error("job_provider_sync_failed", error=str(exc))
        raise


async def run_provider_health_check(ctx: dict) -> dict:
    try:
        from app.providers.registry import registry
        from datetime import datetime, timezone
        results = await registry.health_check_all()

        # Persist health records
        async with AsyncSessionLocal() as db:
            for prov_id, is_healthy in results.items():
                try:
                    from app.core.models import Provider
                    p = await db.get(Provider, prov_id)
                    if p:
                        old_status = p.health_status
                        p.health_status = "healthy" if is_healthy else "degraded"
                        p.last_health_check_at = datetime.now(timezone.utc)
                        # Log status change
                        if old_status != p.health_status:
                            from app.notifications.telegram_logger import tg_logger
                            if not is_healthy:
                                await tg_logger.provider_degraded(p.name, "health check failed")
                            else:
                                await tg_logger.provider_recovered(p.name)
                except Exception:
                    pass
            await db.commit()

        summary = {"checked": len(results), "healthy": sum(1 for v in results.values() if v)}
        logger.info("job_provider_health_complete", **summary)
        return summary
    except Exception as exc:
        logger.error("job_provider_health_failed", error=str(exc))
        raise


async def run_backup(ctx: dict) -> dict:
    try:
        result  = await ctx["backup_worker"].run()
        summary = result.to_notification_dict()
        try:
            from app.notifications.telegram_logger import tg_logger
            await tg_logger.backup_complete(
                result.size_bytes, result.sha256_hex[:16], result.s3_key
            )
        except Exception:
            pass
        logger.info("job_backup_complete", **summary)
        return summary
    except Exception as exc:
        logger.error("job_backup_failed", error=str(exc))
        try:
            from app.notifications.telegram_logger import tg_logger
            await tg_logger.backup_failed(str(exc)[:200])
        except Exception:
            pass
        raise


async def run_wallet_reconciler(ctx: dict) -> dict:
    try:
        from app.wallet.ledger import verify_ledger_invariant
        from sqlalchemy import select
        from app.core.models import Wallet

        violations = 0
        async with AsyncSessionLocal() as db:
            uids = [r[0] for r in (await db.execute(select(Wallet.user_id))).all()]
            for uid in uids:
                try:
                    valid, cached, computed = await verify_ledger_invariant(db, uid)
                    if not valid:
                        violations += 1
                        logger.error("ledger_violation", user_id=uid, cached=str(cached), computed=str(computed))
                except Exception:
                    violations += 1
            await db.commit()

        summary = {"wallets_checked": len(uids), "violations": violations}
        logger.info("job_wallet_reconciler_complete", **summary)
        return summary
    except Exception as exc:
        logger.error("job_wallet_reconciler_failed", error=str(exc))
        raise


async def run_premium_expiry(ctx: dict) -> dict:
    try:
        from app.premium.service import expire_all_lapsed
        async with AsyncSessionLocal() as db:
            expired = await expire_all_lapsed(db)
            await db.commit()
        summary = {"expired": expired}
        logger.info("job_premium_expiry_complete", **summary)
        return summary
    except Exception as exc:
        logger.error("job_premium_expiry_failed", error=str(exc))
        raise


async def run_session_cleanup(ctx: dict) -> dict:
    try:
        from sqlalchemy import delete
        from app.core.models import UserSession
        from datetime import datetime, timezone
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                delete(UserSession).where(UserSession.expires_at < datetime.now(timezone.utc))
            )
            await db.commit()
        summary = {"sessions_deleted": result.rowcount}
        logger.info("job_session_cleanup_complete", **summary)
        return summary
    except Exception as exc:
        logger.error("job_session_cleanup_failed", error=str(exc))
        raise


class WorkerSettings:
    functions   = [
        run_order_monitor,
        run_payment_reconciler,
        run_provider_sync_all,
        run_provider_health_check,
        run_backup,
        run_wallet_reconciler,
        run_premium_expiry,
        run_session_cleanup,
    ]
    on_startup  = startup
    on_shutdown = shutdown
    max_tries   = 3
    job_timeout = 3600
    keep_result = 3600
