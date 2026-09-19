"""
Provider sync normalizer — detects changes between provider API responses
and our stored ProviderService records.

ServiceDiff records what changed: field name, old value, new value.
normalize_service() converts a raw provider response dict into a
canonical ProviderService-like object for comparison.
detect_changes() diffs the raw API data against the stored record.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional


@dataclass
class NormalizedService:
    """Canonical representation of a provider service for comparison."""
    provider_svc_id: str
    raw_name:        str
    category:        str
    rate:            Decimal
    min_qty:         int
    max_qty:         int
    refill:          bool
    cancel:          bool
    drip_feed:       bool
    raw:             dict    # original provider response


@dataclass
class ServiceDiff:
    """Records a single field change between provider data and stored data."""
    provider_svc_id: str
    field:           str
    old_value:       Any
    new_value:       Any

    def is_name_change(self) -> bool:
        return self.field == "name"

    def is_price_change(self) -> bool:
        return self.field == "rate"

    def is_availability_change(self) -> bool:
        return self.field in ("refill", "cancel", "drip_feed")

    def is_limit_change(self) -> bool:
        return self.field in ("min_qty", "max_qty")


def normalize_service(ps) -> NormalizedService:
    """
    Convert a ProviderService ORM object or raw dict into a NormalizedService.
    Accepts either the ORM model or a dict with 'provider_svc_id' etc.
    """
    if hasattr(ps, 'provider_svc_id'):
        # ORM object
        return NormalizedService(
            provider_svc_id=str(ps.provider_svc_id),
            raw_name=ps.raw_name,
            category=ps.category,
            rate=Decimal(str(ps.rate)),
            min_qty=int(ps.min_qty),
            max_qty=int(ps.max_qty),
            refill=bool(ps.refill),
            cancel=bool(ps.cancel),
            drip_feed=bool(ps.drip_feed),
            raw=ps.raw if hasattr(ps, 'raw') else {},
        )
    else:
        # Raw dict from provider API
        return NormalizedService(
            provider_svc_id=str(ps.get("service", ps.get("id", ""))),
            raw_name=str(ps.get("name", "")),
            category=str(ps.get("category", "")),
            rate=Decimal(str(ps.get("rate", "0"))),
            min_qty=int(ps.get("min", 0)),
            max_qty=int(ps.get("max", 0)),
            refill=bool(ps.get("refill", False)),
            cancel=bool(ps.get("cancel", False)),
            drip_feed=bool(ps.get("dripfeed", False)),
            raw=ps,
        )


def detect_changes(
    stored_raw: Optional[dict],
    incoming: NormalizedService,
) -> list[ServiceDiff]:
    """
    Compare an incoming normalized service against the stored raw dict.

    stored_raw: the raw JSON previously stored in ProviderService.raw
                (or None if this is a new service — returns empty list)
    incoming:   the newly fetched normalized service

    Returns a list of ServiceDiff objects for each changed field.
    Empty list = no changes.
    """
    if stored_raw is None:
        # New service — no diffs (caller handles insertion)
        return []

    diffs: list[ServiceDiff] = []
    svc_id = incoming.provider_svc_id

    # Name
    stored_name = str(stored_raw.get("name", ""))
    if stored_name != incoming.raw_name:
        diffs.append(ServiceDiff(svc_id, "name", stored_name, incoming.raw_name))

    # Rate
    try:
        stored_rate = Decimal(str(stored_raw.get("rate", "0")))
    except Exception:
        stored_rate = Decimal("0")
    if stored_rate != incoming.rate:
        diffs.append(ServiceDiff(svc_id, "rate", stored_rate, incoming.rate))

    # Min/max qty
    try:
        stored_min = int(stored_raw.get("min", 0))
        stored_max = int(stored_raw.get("max", 0))
    except Exception:
        stored_min = stored_max = 0

    if stored_min != incoming.min_qty:
        diffs.append(ServiceDiff(svc_id, "min_qty", stored_min, incoming.min_qty))
    if stored_max != incoming.max_qty:
        diffs.append(ServiceDiff(svc_id, "max_qty", stored_max, incoming.max_qty))

    # Refill / cancel / dripfeed
    stored_refill    = bool(stored_raw.get("refill", False))
    stored_cancel    = bool(stored_raw.get("cancel", False))
    stored_dripfeed  = bool(stored_raw.get("dripfeed", False))

    if stored_refill != incoming.refill:
        diffs.append(ServiceDiff(svc_id, "refill", stored_refill, incoming.refill))
    if stored_cancel != incoming.cancel:
        diffs.append(ServiceDiff(svc_id, "cancel", stored_cancel, incoming.cancel))
    if stored_dripfeed != incoming.drip_feed:
        diffs.append(ServiceDiff(svc_id, "drip_feed", stored_dripfeed, incoming.drip_feed))

    return diffs
