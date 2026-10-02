"""Provider service synchronisation (blueprint section 7).

Guarantees:
  * Provider data is validated row by row (app.sync_normalize); bad rows are skipped and counted.
  * A malformed or suspiciously small response NEVER replaces the last trusted catalog.
  * New upstream services are created INACTIVE. Nothing becomes sellable just because a provider
    listed it, unless the operator opted the provider in with auto_activate_new.
  * Services that vanish upstream are deactivated (never deleted) so historical orders stay intact.
  * Admin-edited names/descriptions are never overwritten; upstream facts live in external_* columns.
  * Every material change (price move, limits, removal) is reported for the operator to review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ProviderError
from app.audit import audit
from app.catalog import adapter_for
from app.models import Category, Provider, Service, SyncRun
from app.security import utcnow
from app.sync_normalize import (
    NormalizedService,
    category_id_for,
    looks_malformed,
    normalize_service,
    service_id_for,
)

log = logging.getLogger("falaron.sync")

MATERIAL_RATE_CHANGE = 0.10  # 10%


@dataclass
class Change:
    service_id: str
    name: str
    kind: str  # new | rate | limits | removed | restored | name | capabilities
    old: str = ""
    new: str = ""

    def line(self) -> str:
        arrow = f" {self.old} → {self.new}" if self.old or self.new else ""
        return f"[{self.kind}] {self.name[:48]} ({self.service_id}){arrow}"


@dataclass
class SyncReport:
    provider_id: str
    status: str = "ok"  # ok | aborted | error
    detail: str = ""
    added: int = 0
    updated: int = 0
    removed: int = 0
    restored: int = 0
    unchanged: int = 0
    skipped_invalid: int = 0
    unsupported_new: int = 0
    changes: list[Change] = field(default_factory=list)

    def material(self) -> list[Change]:
        return [c for c in self.changes if c.kind in {"rate", "limits", "removed", "restored"}]

    def summary(self) -> str:
        head = (
            f"{self.provider_id}: {self.status} — +{self.added} new, ~{self.updated} changed, "
            f"-{self.removed} removed, {self.restored} restored, {self.skipped_invalid} invalid"
        )
        return head + (f" ({self.detail})" if self.detail else "")


def _fmt_rate(paise: int) -> str:
    return f"₹{paise / 100:,.2f}/1k"


async def _ensure_category(
    session: AsyncSession, name: str, cache: dict[str, Category], *, active: bool
) -> Category:
    cid = category_id_for(name)
    cat = cache.get(cid)
    if cat is None:
        cat = await session.get(Category, cid)
    if cat is None:
        cat = Category(id=cid, name=name[:80], emoji="", description=None, sort_order=1000, is_active=active)
        session.add(cat)
        await session.flush()
    elif active and not cat.is_active:
        cat.is_active = True
    cache[cid] = cat
    return cat


async def sync_provider(session: AsyncSession, provider: Provider) -> SyncReport:
    report = SyncReport(provider_id=provider.id)
    run = SyncRun(provider_id=provider.id, status="ok", started_at=utcnow())
    session.add(run)
    await session.flush()

    try:
        adapter = await adapter_for(session, provider.id)
        upstream = await adapter.get_services()
    except (ProviderError, ValueError) as exc:
        report.status, report.detail = "error", f"provider request failed: {str(exc)[:120]}"
        return await _finish(session, provider, run, report)

    fx = float(provider.fx_to_inr or 1.0)
    rows: dict[str, NormalizedService] = {}
    invalid = 0
    for item in upstream:
        n = normalize_service(
            external_id=item.external_id,
            name=item.name,
            category=item.category,
            rate=item.rate,
            min_qty=item.min_qty,
            max_qty=item.max_qty,
            service_type=item.service_type,
            refill=item.refill,
            cancel=item.cancel,
            fx_to_inr=fx,
        )
        if n is None:
            invalid += 1
        else:
            rows[n.external_id] = n  # last one wins on duplicate ids
    report.skipped_invalid = invalid

    existing_rows = (
        await session.execute(select(Service).where(Service.provider_id == provider.id))
    ).scalars().all()
    by_external = {s.provider_service_id: s for s in existing_rows}
    previous_live = sum(1 for s in existing_rows if not s.is_removed_upstream)

    reason = looks_malformed(previous_live, len(rows), invalid)
    if reason:
        report.status, report.detail = "aborted", f"kept last trusted snapshot: {reason}"
        return await _finish(session, provider, run, report)

    now = utcnow()
    cat_cache: dict[str, Category] = {}
    auto = bool(provider.auto_activate_new)

    for ext_id, n in rows.items():
        svc = by_external.get(ext_id)
        if svc is None:
            activate = auto and n.supported
            cat = await _ensure_category(session, n.category, cat_cache, active=activate)
            session.add(
                Service(
                    id=service_id_for(provider.id, ext_id),
                    category_id=cat.id,
                    provider_id=provider.id,
                    provider_service_id=ext_id,
                    name=n.name,
                    external_name=n.name,
                    external_category=n.category,
                    description=None,
                    service_type=n.service_type,
                    min_qty=n.min_qty,
                    max_qty=n.max_qty,
                    rate_per_1000_paise=n.rate_per_1000_paise,
                    is_active=activate,
                    is_refillable=n.refill,
                    is_cancelable=n.cancel,
                    is_removed_upstream=False,
                    last_synced_at=now,
                    created_at=now,
                )
            )
            report.added += 1
            if not n.supported:
                report.unsupported_new += 1
            report.changes.append(Change(service_id_for(provider.id, ext_id), n.name, "new", "", _fmt_rate(n.rate_per_1000_paise)))
            continue

        changed = False
        if svc.is_removed_upstream:
            svc.is_removed_upstream = False  # comes back INACTIVE; operator re-enables deliberately
            report.restored += 1
            report.changes.append(Change(svc.id, svc.name, "restored"))
            changed = True
        if int(svc.rate_per_1000_paise) != n.rate_per_1000_paise:
            old = int(svc.rate_per_1000_paise)
            report.changes.append(Change(svc.id, svc.name, "rate", _fmt_rate(old), _fmt_rate(n.rate_per_1000_paise)))
            svc.rate_per_1000_paise = n.rate_per_1000_paise
            changed = True
        if svc.min_qty != n.min_qty or svc.max_qty != n.max_qty:
            report.changes.append(
                Change(svc.id, svc.name, "limits", f"{svc.min_qty}-{svc.max_qty}", f"{n.min_qty}-{n.max_qty}")
            )
            svc.min_qty, svc.max_qty = n.min_qty, n.max_qty
            changed = True
        if bool(svc.is_refillable) != n.refill or bool(svc.is_cancelable) != n.cancel:
            report.changes.append(Change(svc.id, svc.name, "capabilities"))
            svc.is_refillable, svc.is_cancelable = n.refill, n.cancel
            changed = True
        if svc.service_type != n.service_type:
            svc.service_type = n.service_type
            changed = True
            if not n.supported and svc.is_active:
                svc.is_active = False  # a type we cannot render must not stay sellable
        if svc.external_name != n.name:
            if svc.external_name is None or svc.name == svc.external_name:
                svc.name = n.name  # untouched by admin: follow upstream
            report.changes.append(Change(svc.id, svc.name, "name", svc.external_name or "", n.name))
            svc.external_name = n.name
            changed = True
        svc.external_category = n.category
        svc.last_synced_at = now
        if changed:
            report.updated += 1
        else:
            report.unchanged += 1

    for ext_id, svc in by_external.items():
        if ext_id in rows or svc.is_removed_upstream:
            continue
        svc.is_removed_upstream = True
        if svc.is_active:
            svc.is_active = False
            report.changes.append(Change(svc.id, svc.name, "removed"))
        report.removed += 1

    return await _finish(session, provider, run, report)


async def _finish(session: AsyncSession, provider: Provider, run: SyncRun, report: SyncReport) -> SyncReport:
    now = utcnow()
    run.status = report.status
    run.added, run.updated, run.removed = report.added, report.updated, report.removed
    run.skipped_invalid = report.skipped_invalid
    run.detail = report.detail[:255] or None
    run.finished_at = now
    provider.last_sync_at = now
    provider.last_sync_status = report.summary()[:160]
    if report.status == "ok" and (report.added or report.updated or report.removed):
        await audit(
            session,
            actor="system",
            action="provider.sync",
            target=provider.id,
            new={"added": report.added, "updated": report.updated, "removed": report.removed},
        )
    await session.flush()
    return report


async def sync_all(session: AsyncSession) -> list[SyncReport]:
    providers = (
        await session.execute(
            select(Provider).where(Provider.is_active.is_(True), Provider.adapter_type != "mock")
        )
    ).scalars().all()
    reports: list[SyncReport] = []
    for provider in providers:
        try:
            reports.append(await sync_provider(session, provider))
        except Exception as exc:  # noqa: BLE001
            log.exception("sync failed for %s", provider.id)
            reports.append(SyncReport(provider_id=provider.id, status="error", detail=type(exc).__name__))
    return reports


def render_alert(reports: list[SyncReport], limit: int = 12) -> str | None:
    """Telegram-ready HTML digest of material changes, or None when nothing needs attention."""
    from html import escape

    lines: list[str] = []
    for r in reports:
        if r.status != "ok":
            lines.append(f"⚠️ <b>{escape(r.provider_id)}</b> sync {escape(r.status)}: {escape(r.detail)}")
            continue
        material = r.material()
        if material or r.added:
            lines.append(
                f"🔄 <b>{escape(r.provider_id)}</b>: +{r.added} new (inactive), {len(material)} material changes"
            )
            for c in material[:limit]:
                lines.append("• " + escape(c.line()))
            if len(material) > limit:
                lines.append(f"…and {len(material) - limit} more")
    return "\n".join(lines) if lines else None


async def count_services(session: AsyncSession, provider_id: str) -> tuple[int, int]:
    total = (
        await session.execute(select(func.count()).select_from(Service).where(Service.provider_id == provider_id))
    ).scalar_one()
    active = (
        await session.execute(
            select(func.count()).select_from(Service).where(Service.provider_id == provider_id, Service.is_active.is_(True))
        )
    ).scalar_one()
    return int(total), int(active)
