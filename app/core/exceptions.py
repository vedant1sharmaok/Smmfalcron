"""
Typed exception hierarchy for the SMM platform.

All exceptions carry:
  detail       — internal message (logged, never sent to users)
  user_message — safe string shown in bot/API responses
  status_code  — HTTP status code for API responses

Import pattern:
  from app.core.exceptions import InsufficientBalanceError, OrderError, ...
"""

from __future__ import annotations


class SMMError(Exception):
    """Base exception for all platform errors."""
    status_code: int = 500

    def __init__(
        self,
        detail: str = "",
        user_message: str = "An unexpected error occurred.",
        **kwargs,
    ) -> None:
        super().__init__(detail)
        self.detail       = detail
        self.user_message = user_message
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.detail!r})"


# ── Auth ───────────────────────────────────────────────────────────────────────
class AuthError(SMMError):
    status_code = 401

class InvalidInitDataError(AuthError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Authentication failed. Please restart the bot.", **kw)

class InvalidCredentialsError(AuthError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Invalid credentials.", **kw)

class SessionExpiredError(AuthError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Session expired. Please restart.", **kw)

class AdminAuthError(AuthError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Admin authentication failed.", **kw)

class AdminLockedError(AuthError):
    status_code = 423
    def __init__(self, detail="", user_message="Account temporarily locked.", **kw):
        super().__init__(detail, user_message=user_message, **kw)

class InvalidTOTPError(AuthError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Invalid authentication code.", **kw)

class PermissionDeniedError(AuthError):
    status_code = 403
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="You do not have permission to perform this action.", **kw)

class UserBannedError(AuthError):
    status_code = 403
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Your account has been suspended.", **kw)


# ── Validation ────────────────────────────────────────────────────────────────
class ValidationError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Invalid input.", **kw):
        super().__init__(detail, user_message=user_message, **kw)


# ── Wallet ────────────────────────────────────────────────────────────────────
class WalletError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Wallet operation failed.", **kw):
        super().__init__(detail, user_message=user_message, **kw)

class InsufficientBalanceError(WalletError):
    def __init__(self, detail="", required=None, available=None, **kw):
        msg = "Insufficient wallet balance."
        if required and available:
            from decimal import Decimal
            msg = (f"Insufficient balance. Required: ₹{Decimal(str(required)):.2f}, "
                   f"available: ₹{Decimal(str(available)):.2f}.")
        super().__init__(detail, user_message=msg, **kw)
        self.required  = required
        self.available = available


# ── Orders ────────────────────────────────────────────────────────────────────
class OrderError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Order operation failed.", **kw):
        super().__init__(detail, user_message=user_message, **kw)

class DuplicateOrderError(OrderError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Duplicate order request.", **kw)

class OrderNotFoundError(OrderError):
    status_code = 404
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Order not found.", **kw)

class OrderCancelError(OrderError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="This order cannot be cancelled.", **kw)

class OrderRefillError(OrderError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="This order is not eligible for refill.", **kw)


# ── Payments ──────────────────────────────────────────────────────────────────
class PaymentError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Payment operation failed.", **kw):
        super().__init__(detail, user_message=user_message, **kw)

class DuplicatePaymentError(PaymentError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Payment already processed.", **kw)

class InvalidWebhookSignatureError(PaymentError):
    status_code = 400
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Invalid webhook signature.", **kw)

class PaymentVerificationError(PaymentError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Payment verification failed.", **kw)


# ── Providers ─────────────────────────────────────────────────────────────────
class ProviderError(SMMError):
    status_code = 502

    def __init__(self, detail="", provider_id=None, user_message="Provider error.", **kw):
        super().__init__(detail, user_message=user_message, **kw)
        self.provider_id = provider_id

class ProviderResponseError(ProviderError):
    def __init__(self, detail="", provider_id=None, **kw):
        super().__init__(detail, provider_id=provider_id,
                         user_message="Provider returned an unexpected response.", **kw)

class ProviderUnavailableError(ProviderError):
    def __init__(self, detail="", provider_id=None, **kw):
        super().__init__(detail, provider_id=provider_id,
                         user_message="Service provider is temporarily unavailable.", **kw)


# ── Services ──────────────────────────────────────────────────────────────────
class ServiceError(SMMError):
    status_code = 422

class ServiceNotFoundError(ServiceError):
    status_code = 404
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Service not found.", **kw)

class ServiceUnavailableError(ServiceError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="This service is currently unavailable.", **kw)


# ── Kill switches ─────────────────────────────────────────────────────────────
class KillSwitchError(SMMError):
    status_code = 503

    def __init__(self, detail="", switch_name="", **kw):
        super().__init__(detail, user_message="This feature is temporarily disabled.", **kw)
        self.switch_name = switch_name


# ── Rate limiting ─────────────────────────────────────────────────────────────
class RateLimitError(SMMError):
    status_code = 429

    def __init__(self, detail="", retry_after=None, **kw):
        super().__init__(detail, user_message="Too many requests. Please slow down.", **kw)
        self.retry_after = retry_after


# ── Coupons ───────────────────────────────────────────────────────────────────
class CouponError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Coupon error.", **kw):
        super().__init__(detail, user_message=user_message, **kw)

class CouponNotFoundError(CouponError):
    status_code = 404
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Coupon not found or inactive.", **kw)

class CouponExpiredError(CouponError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="This coupon has expired.", **kw)

class CouponUsageLimitError(CouponError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="This coupon has reached its usage limit.", **kw)

class CouponAlreadyRedeemedError(CouponError):
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="You have already used this coupon.", **kw)

class CouponTransferError(CouponError):
    def __init__(self, detail="", user_message="Coupon cannot be transferred.", **kw):
        super().__init__(detail, user_message=user_message, **kw)


# ── Tenants ───────────────────────────────────────────────────────────────────
class TenantError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Tenant operation failed.", **kw):
        super().__init__(detail, user_message=user_message, **kw)

class TenantNotFoundError(TenantError):
    status_code = 404
    def __init__(self, detail="", **kw):
        super().__init__(detail, user_message="Tenant not found.", **kw)


# ── Premium ───────────────────────────────────────────────────────────────────
class PremiumError(SMMError):
    status_code = 403

    def __init__(self, detail="", user_message="Premium subscription required.", **kw):
        super().__init__(detail, user_message=user_message, **kw)


# ── CMS ───────────────────────────────────────────────────────────────────────
class CMSError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Content operation failed.", **kw):
        super().__init__(detail, user_message=user_message, **kw)


# ── Pricing ───────────────────────────────────────────────────────────────────
class PricingError(SMMError):
    status_code = 422

    def __init__(self, detail="", user_message="Pricing calculation failed.", **kw):
        super().__init__(detail, user_message=user_message, **kw)

class PriceChangedError(PricingError):
    def __init__(self, detail="", **kw):
        super().__init__(detail,
            user_message="The price changed between preview and order. Please try again.", **kw)
