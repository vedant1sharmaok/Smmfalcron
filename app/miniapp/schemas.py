"""
Mini App API schemas — all request/response Pydantic models.

Security invariants enforced here:
  - user_id NEVER in any request schema — always from session
  - provider_id, provider_svc_id, credentials_enc NEVER in response schemas
  - service_public_id normalised to uppercase SVC-NNNN format
  - idempotency_key minimum length enforced
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class CreateOrderRequest(BaseModel):
    service_public_id: str = Field(min_length=4, max_length=16)
    quantity:          int = Field(gt=0)
    link:              Optional[str] = None
    coupon_code:       Optional[str] = None
    idempotency_key:   str = Field(min_length=8, max_length=128)

    @field_validator("service_public_id")
    @classmethod
    def normalise_public_id(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.startswith("SVC-"):
            raise ValueError("service_public_id must start with 'SVC-'")
        return v

    @field_validator("link")
    @classmethod
    def strip_link(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        return stripped if stripped else None


class CreateDepositRequest(BaseModel):
    amount:   Decimal = Field(gt=0)
    provider: str     = Field(default="razorpay")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        if v < 10:
            raise ValueError("Minimum deposit is ₹10")
        if v > 100000:
            raise ValueError("Maximum deposit is ₹1,00,000")
        return v

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        allowed = {"razorpay", "stripe"}
        if v not in allowed:
            raise ValueError(f"Provider must be one of: {allowed}")
        return v


class PricePreviewRequest(BaseModel):
    service_public_id: str     = Field(min_length=4, max_length=16)
    quantity:          int     = Field(gt=0)
    coupon_code:       Optional[str] = None

    @field_validator("service_public_id")
    @classmethod
    def normalise(cls, v: str) -> str:
        v = v.strip().upper()
        if not v.startswith("SVC-"):
            raise ValueError("service_public_id must start with 'SVC-'")
        return v


class ApplyCouponRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)

    @field_validator("code")
    @classmethod
    def upper_code(cls, v: str) -> str:
        return v.strip().upper()


class PaginationParams(BaseModel):
    page:      int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


# ── Response schemas — no provider fields ──────────────────────────────────────

class ServiceOut(BaseModel):
    public_id:       str
    display_name:    str
    category:        Optional[str] = None
    min_qty:         int
    max_qty:         int
    price_per_1000:  str           # Decimal serialised as string for precision
    refill_eligible: bool
    cancel_eligible: bool

    # provider_id, provider_svc_id, provider_rate, credentials_enc are NEVER here


class OrderOut(BaseModel):
    id:                str
    public_ref:        str
    service_public_id: str
    service_name:      str
    status:            str
    quantity:          int
    price_charged:     str         # Decimal as string
    currency:          str
    refill_eligible:   bool
    cancel_eligible:   bool
    source:            str
    created_at:        str
    remains:           Optional[int] = None
    start_count:       Optional[int] = None
    link:              Optional[str] = None

    # provider_id, provider_order_id, provider_svc_id, credentials_enc NEVER here


class WalletOut(BaseModel):
    balance:  str   # Decimal as string
    currency: str


class DepositOut(BaseModel):
    payment_id:   int
    provider_ref: str
    amount:       str
    provider:     str
    payment_link: Optional[str] = None
    client_secret: Optional[str] = None


class ProfileOut(BaseModel):
    telegram_id:  int
    first_name:   str
    username:     Optional[str] = None
    is_premium:   bool
    plan:         str
    total_orders: int
    member_since: Optional[str] = None
