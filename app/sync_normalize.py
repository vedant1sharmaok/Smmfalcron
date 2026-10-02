"""Validate and normalise provider service rows (stdlib only).

Provider data is untrusted: every field is type-checked, bounded and cleaned
before it can touch the catalog. Rows that fail validation are skipped and
counted, never guessed at.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

MAX_RATE_PER_1000 = 10_000_000.0  # provider currency units; anything above is treated as garbage
MAX_EXTERNAL_ID_LEN = 24
MAX_NAME_LEN = 160
MAX_CATEGORY_LEN = 120

SUPPORTED_TYPES = {"default", "comments", "mentions"}
UNSUPPORTED = "unsupported"

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_ID_OK = re.compile(r"^[A-Za-z0-9_.\-]+$")


@dataclass(frozen=True)
class NormalizedService:
    external_id: str
    name: str
    category: str
    rate_per_1000_paise: int
    min_qty: int
    max_qty: int
    service_type: str  # default | comments | mentions | unsupported
    raw_type: str
    refill: bool
    cancel: bool

    @property
    def supported(self) -> bool:
        return self.service_type in SUPPORTED_TYPES


def clean_text(value: object, max_len: int) -> str:
    text = _CTRL.sub(" ", str(value if value is not None else ""))
    text = _WS.sub(" ", text).strip()
    return text[:max_len]


def map_service_type(raw: object) -> str:
    """Map a panel's service type onto the order forms this platform can render."""
    label = clean_text(raw, 64).lower().replace("_", " ")
    if label in {"", "default"}:
        return "default"
    if label == "custom comments":
        return "comments"
    if label in {"mentions custom list", "custom mentions"}:
        return "mentions"
    return UNSUPPORTED  # package, poll, subscriptions, comment likes, ... need bespoke forms


def category_id_for(category: str) -> str:
    """Stable category id (<= 32 chars) derived from the provider category name."""
    slug = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")[:18] or "other"
    digest = hashlib.sha1(category.strip().lower().encode("utf-8")).hexdigest()[:6]
    return f"cat_{slug}_{digest}"


def service_id_for(provider_id: str, external_id: str) -> str:
    return f"{provider_id}-{external_id}"[:64]


def _as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def normalize_service(
    *,
    external_id: object,
    name: object,
    category: object,
    rate: object,
    min_qty: object,
    max_qty: object,
    service_type: object,
    refill: object = False,
    cancel: object = False,
    fx_to_inr: float = 1.0,
) -> NormalizedService | None:
    """Return a clean row, or None when the provider row must be rejected."""
    ext = clean_text(external_id, 64)
    if not ext or len(ext) > MAX_EXTERNAL_ID_LEN or not _ID_OK.match(ext):
        return None
    nm = clean_text(name, MAX_NAME_LEN)
    if not nm:
        return None
    cat = clean_text(category, MAX_CATEGORY_LEN) or "Other"
    try:
        rate_f = float(rate)  # type: ignore[arg-type]
        lo = int(float(min_qty))  # type: ignore[arg-type]
        hi = int(float(max_qty))  # type: ignore[arg-type]
        fx = float(fx_to_inr)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(rate_f) and math.isfinite(fx)):
        return None
    if rate_f < 0 or rate_f > MAX_RATE_PER_1000 or fx <= 0 or fx > 10_000:
        return None
    if lo < 1 or hi < lo or hi > 1_000_000_000:
        return None
    paise = int(round(rate_f * fx * 100))
    if paise <= 0:  # a free service is almost always a provider error; refuse to sell it
        return None
    raw_type = clean_text(service_type, 64)
    return NormalizedService(
        external_id=ext,
        name=nm,
        category=cat,
        rate_per_1000_paise=paise,
        min_qty=lo,
        max_qty=hi,
        service_type=map_service_type(raw_type),
        raw_type=raw_type,
        refill=_as_bool(refill),
        cancel=_as_bool(cancel),
    )


def looks_malformed(previous_count: int, valid_count: int, invalid_count: int) -> str | None:
    """Return a reason when a sync result should NOT replace the last trusted snapshot."""
    if valid_count == 0:
        return "provider returned no valid services"
    total = valid_count + invalid_count
    if total >= 10 and invalid_count / total > 0.30:
        return f"{invalid_count}/{total} provider rows failed validation"
    if previous_count >= 20 and valid_count < previous_count * 0.5:
        return f"service count dropped from {previous_count} to {valid_count}"
    return None
