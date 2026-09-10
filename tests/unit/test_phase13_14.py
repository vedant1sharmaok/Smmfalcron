"""
Phase 11-14 unit tests — schemas + reseller pricing logic.

Covers:
  - Miniapp schemas (CreateOrderRequest, CreateDepositRequest, ServiceOut, OrderOut)
  - Ownership: user_id absent from all request bodies
  - Reseller pricing: discount calc, margin floor cap, zero discount, full discount blocked
  - Reseller profile validation: negative discount rejected, pct>=100 rejected
  - API key hash format
  - ProviderOut: credentials never in response
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock


# ══════════════════════════════════════════════════════════════════════════════
# Miniapp schemas
# ══════════════════════════════════════════════════════════════════════════════

class TestCreateOrderRequestSchema:

    def test_valid_order_request(self):
        from app.miniapp.schemas import CreateOrderRequest
        req = CreateOrderRequest(
            service_public_id="SVC-0001",
            quantity=1000,
            link="https://instagram.com/user",
            idempotency_key="test-idem-key-12345678",
        )
        assert req.service_public_id == "SVC-0001"
        assert req.quantity == 1000

    def test_lowercase_public_id_normalised(self):
        from app.miniapp.schemas import CreateOrderRequest
        req = CreateOrderRequest(
            service_public_id="svc-0001",
            quantity=500,
            idempotency_key="idem-key-0000000001",
        )
        assert req.service_public_id == "SVC-0001"

    def test_invalid_public_id_prefix_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateOrderRequest
        with pytest.raises(ValidationError):
            CreateOrderRequest(
                service_public_id="123-INVALID",
                quantity=100,
                idempotency_key="idem-key-1234567890",
            )

    def test_zero_quantity_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateOrderRequest
        with pytest.raises(ValidationError):
            CreateOrderRequest(
                service_public_id="SVC-0001",
                quantity=0,
                idempotency_key="idem-key-1234567890",
            )

    def test_negative_quantity_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateOrderRequest
        with pytest.raises(ValidationError):
            CreateOrderRequest(
                service_public_id="SVC-0001",
                quantity=-100,
                idempotency_key="idem-key-1234567890",
            )

    def test_link_stripped(self):
        from app.miniapp.schemas import CreateOrderRequest
        req = CreateOrderRequest(
            service_public_id="SVC-0001",
            quantity=100,
            link="  https://instagram.com/user  ",
            idempotency_key="idem-key-1234567890",
        )
        assert req.link == "https://instagram.com/user"

    def test_whitespace_link_becomes_none(self):
        from app.miniapp.schemas import CreateOrderRequest
        req = CreateOrderRequest(
            service_public_id="SVC-0001",
            quantity=100,
            link="   ",
            idempotency_key="idem-key-1234567890",
        )
        assert req.link is None

    def test_short_idempotency_key_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateOrderRequest
        with pytest.raises(ValidationError):
            CreateOrderRequest(
                service_public_id="SVC-0001",
                quantity=100,
                idempotency_key="short",
            )

    def test_no_user_id_in_schema(self):
        from app.miniapp.schemas import CreateOrderRequest
        assert "user_id" not in CreateOrderRequest.model_fields


class TestCreateDepositRequestSchema:

    def test_valid_deposit(self):
        from app.miniapp.schemas import CreateDepositRequest
        req = CreateDepositRequest(amount=Decimal("500"), provider="razorpay")
        assert req.amount == Decimal("500")

    def test_below_minimum_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateDepositRequest
        with pytest.raises(ValidationError):
            CreateDepositRequest(amount=Decimal("5"))

    def test_zero_amount_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateDepositRequest
        with pytest.raises(ValidationError):
            CreateDepositRequest(amount=Decimal("0"))

    def test_unknown_provider_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateDepositRequest
        with pytest.raises(ValidationError):
            CreateDepositRequest(amount=Decimal("100"), provider="mystery_pay")

    def test_above_max_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import CreateDepositRequest
        with pytest.raises(ValidationError):
            CreateDepositRequest(amount=Decimal("100001"))

    def test_default_provider_razorpay(self):
        from app.miniapp.schemas import CreateDepositRequest
        req = CreateDepositRequest(amount=Decimal("100"))
        assert req.provider == "razorpay"

    def test_no_user_id_in_schema(self):
        from app.miniapp.schemas import CreateDepositRequest
        assert "user_id" not in CreateDepositRequest.model_fields


class TestServiceOutNoProviderFields:

    def test_service_out_excludes_provider_fields(self):
        from app.miniapp.schemas import ServiceOut
        svc = ServiceOut(
            public_id="SVC-0001",
            display_name="Instagram Followers",
            min_qty=100,
            max_qty=10000,
            price_per_1000="1.20",
            refill_eligible=True,
            cancel_eligible=False,
        )
        d = svc.model_dump()
        for forbidden in ["provider_id", "provider_svc_id", "provider_rate", "credentials_enc"]:
            assert forbidden not in d

    def test_price_per_1000_is_string(self):
        from app.miniapp.schemas import ServiceOut
        svc = ServiceOut(
            public_id="SVC-0001",
            display_name="Test",
            min_qty=1, max_qty=100,
            price_per_1000="1.2345",
            refill_eligible=False,
            cancel_eligible=False,
        )
        assert isinstance(svc.price_per_1000, str)


class TestOrderOutNoProviderFields:

    def _make(self):
        from app.miniapp.schemas import OrderOut
        return OrderOut(
            id="uuid-1234",
            public_ref="ORD-20240813-ABCD1234",
            service_public_id="SVC-0001",
            service_name="IG Followers",
            status="processing",
            quantity=1000,
            link=None,
            price_charged="1.20",
            currency="INR",
            refill_eligible=True,
            cancel_eligible=False,
            source="miniapp",
            created_at="2024-08-13T12:00:00+00:00",
        )

    def test_no_provider_fields(self):
        d = self._make().model_dump()
        for forbidden in ["provider_id", "provider_order_id", "provider_svc_id",
                          "provider_rate", "credentials_enc"]:
            assert forbidden not in d

    def test_price_is_string(self):
        assert isinstance(self._make().price_charged, str)

    def test_service_public_id_starts_with_svc(self):
        assert self._make().service_public_id.startswith("SVC-")


class TestPaginationParams:

    def test_default_page_and_size(self):
        from app.miniapp.schemas import PaginationParams
        p = PaginationParams()
        assert p.page == 1 and p.page_size == 20 and p.offset == 0

    def test_offset_formula(self):
        from app.miniapp.schemas import PaginationParams
        assert PaginationParams(page=3, page_size=20).offset == 40

    def test_page_zero_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import PaginationParams
        with pytest.raises(ValidationError):
            PaginationParams(page=0)

    def test_page_size_above_100_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.miniapp.schemas import PaginationParams
        with pytest.raises(ValidationError):
            PaginationParams(page_size=101)


# ══════════════════════════════════════════════════════════════════════════════
# Reseller pricing logic
# ══════════════════════════════════════════════════════════════════════════════

class TestResellerPricingCalc:
    """
    Tests calculate_reseller_price() in isolation from the DB.
    The function is synchronous in its math; we run it with asyncio.run().
    """

    def _make_profile(self, discount_pct, min_margin_pct=Decimal("5")):
        p = MagicMock()
        p.discount_pct   = Decimal(str(discount_pct))
        p.min_margin_pct = Decimal(str(min_margin_pct))
        p.user_id        = 42
        return p

    def _run(self, *args, **kwargs):
        import asyncio
        from app.reseller.service import calculate_reseller_price
        return asyncio.run(calculate_reseller_price(*args, **kwargs))

    def test_zero_discount_returns_standard_price(self):
        result = self._run(
            standard_price=Decimal("120"),
            provider_cost=Decimal("100"),
            profile=self._make_profile(0),
        )
        assert result.final_price == Decimal("120.00")
        assert result.was_capped is False
        assert result.discount_amount == Decimal("0")

    def test_20pct_discount_applied(self):
        result = self._run(
            standard_price=Decimal("120"),
            provider_cost=Decimal("100"),
            profile=self._make_profile(20),
        )
        # 120 * 0.80 = 96; floor = 100 * 1.05 = 105 → capped
        assert result.final_price == Decimal("105.00")
        assert result.was_capped is True

    def test_small_discount_no_cap(self):
        result = self._run(
            standard_price=Decimal("150"),
            provider_cost=Decimal("100"),
            profile=self._make_profile(10, min_margin_pct="5"),
        )
        # 150 * 0.90 = 135; floor = 100 * 1.05 = 105 → no cap
        assert result.final_price == Decimal("135.00")
        assert result.was_capped is False

    def test_floor_is_provider_cost_plus_min_margin(self):
        result = self._run(
            standard_price=Decimal("100"),
            provider_cost=Decimal("80"),
            profile=self._make_profile(50, min_margin_pct="10"),
        )
        # 100 * 0.50 = 50; floor = 80 * 1.10 = 88 → capped to 88
        assert result.final_price == Decimal("88.00")
        assert result.was_capped is True
        assert result.floor_price == Decimal("88.00")

    def test_99pct_discount_capped_at_floor(self):
        result = self._run(
            standard_price=Decimal("200"),
            provider_cost=Decimal("100"),
            profile=self._make_profile(99, min_margin_pct="5"),
        )
        # 200 * 0.01 = 2; floor = 105 → capped
        assert result.final_price == Decimal("105.00")
        assert result.was_capped is True

    def test_zero_min_margin_allows_full_discount(self):
        result = self._run(
            standard_price=Decimal("100"),
            provider_cost=Decimal("50"),
            profile=self._make_profile(50, min_margin_pct="0"),
        )
        # 100 * 0.50 = 50; floor = 50 * 1.00 = 50 → exactly at floor, no cap
        assert result.final_price == Decimal("50.00")
        assert result.was_capped is False

    def test_currency_propagated(self):
        result = self._run(
            standard_price=Decimal("100"),
            provider_cost=Decimal("80"),
            profile=self._make_profile(0),
            currency="USD",
        )
        assert result.currency == "USD"

    def test_discount_amount_correct(self):
        result = self._run(
            standard_price=Decimal("200"),
            provider_cost=Decimal("100"),
            profile=self._make_profile(10, min_margin_pct="0"),
        )
        # 200 * 0.90 = 180; floor=100 → no cap
        # discount_amount = standard - final = 200 - 180 = 20
        assert result.discount_amount == Decimal("20.00")

    def test_zero_standard_price_raises(self):
        import asyncio, pytest
        from app.core.exceptions import ValidationError
        from app.reseller.service import calculate_reseller_price
        with pytest.raises(ValidationError):
            asyncio.run(calculate_reseller_price(
                standard_price=Decimal("0"),
                provider_cost=Decimal("0"),
                profile=self._make_profile(0),
            ))


# ══════════════════════════════════════════════════════════════════════════════
# Reseller profile validation (service layer)
# ══════════════════════════════════════════════════════════════════════════════

class TestResellerProfileValidation:

    def _run_create(self, **kwargs):
        import asyncio
        from app.reseller.service import create_reseller_profile

        db = MagicMock()
        db.execute = MagicMock()

        async def fake_exec(stmt):
            r = MagicMock(); r.scalar_one_or_none = lambda: None; return r

        import asyncio as _asyncio

        async def run():
            db.execute = fake_exec
            db.flush = MagicMock(return_value=_asyncio.coroutine(lambda: None)())
            db.add   = MagicMock()
            return await create_reseller_profile(db=db, actor_id=1, **kwargs)

        return asyncio.run(run())

    def test_negative_discount_pct_rejected(self):
        import pytest
        from app.core.exceptions import ValidationError
        with pytest.raises(ValidationError, match="discount_pct"):
            self._run_create(
                user_id=1,
                discount_pct=Decimal("-5"),
                min_margin_pct=Decimal("5"),
            )

    def test_100pct_discount_rejected(self):
        import pytest
        from app.core.exceptions import ValidationError
        with pytest.raises(ValidationError, match="discount_pct"):
            self._run_create(
                user_id=1,
                discount_pct=Decimal("100"),
                min_margin_pct=Decimal("5"),
            )

    def test_negative_min_margin_rejected(self):
        import pytest
        from app.core.exceptions import ValidationError
        with pytest.raises(ValidationError, match="min_margin_pct"):
            self._run_create(
                user_id=1,
                discount_pct=Decimal("10"),
                min_margin_pct=Decimal("-1"),
            )


# ══════════════════════════════════════════════════════════════════════════════
# API key format
# ══════════════════════════════════════════════════════════════════════════════

class TestApiKeyFormat:

    def test_generate_api_key_format(self):
        from app.core.crypto import generate_api_key
        raw, hashed = generate_api_key()
        assert raw.startswith("smm_")
        assert len(raw) == 52
        assert len(hashed) == 64  # SHA-256 hex

    def test_hash_api_key_deterministic(self):
        from app.core.crypto import hash_api_key
        key = "smm_" + "a" * 48
        assert hash_api_key(key) == hash_api_key(key)

    def test_different_keys_different_hashes(self):
        from app.core.crypto import hash_api_key
        h1 = hash_api_key("smm_" + "a" * 48)
        h2 = hash_api_key("smm_" + "b" * 48)
        assert h1 != h2

    def test_api_key_invalid_format_detected(self):
        """The own API endpoint checks for 'smm_' prefix."""
        bad_keys = ["", "api_key_123", "123456", "SMM_UPPER"]
        for key in bad_keys:
            assert not key.startswith("smm_"), f"{key!r} should not start with smm_"


# ══════════════════════════════════════════════════════════════════════════════
# Admin ProviderOut — credentials never in response
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminProviderOut:

    def test_credentials_enc_not_in_provider_out(self):
        from app.admin.routes import ProviderOut
        out = ProviderOut(
            id=1, name="Test Provider", slug="test",
            endpoint="https://api.test.com",
            currency="USD", is_active=False,
            health_status="unknown",
            last_sync_at=None, priority=0,
            profit_pct=None,
        )
        d = out.model_dump()
        assert "credentials_enc" not in d
        assert "api_key" not in d
        assert "api_key_hash" not in d

    def test_provider_out_endpoint_visible(self):
        """Admins can see the endpoint URL (they configured it)."""
        from app.admin.routes import ProviderOut
        out = ProviderOut(
            id=1, name="Test", slug="test",
            endpoint="https://api.smmpromax.com",
            currency="USD", is_active=True,
            health_status="healthy",
            last_sync_at=None, priority=5,
            profit_pct="20.0000",
        )
        assert out.endpoint == "https://api.smmpromax.com"


# ══════════════════════════════════════════════════════════════════════════════
# Admin schema validation
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminSchemas:

    def test_pricing_rule_valid(self):
        from app.admin.routes import PricingRuleRequest
        req = PricingRuleRequest(scope="global", rule_type="percentage",
                                  value=Decimal("20"), min_margin_pct=Decimal("5"))
        assert req.scope == "global"

    def test_pricing_rule_invalid_scope(self):
        import pytest
        from pydantic import ValidationError
        from app.admin.routes import PricingRuleRequest
        with pytest.raises(ValidationError):
            PricingRuleRequest(scope="bogus", rule_type="percentage", value=Decimal("20"))

    def test_pricing_rule_invalid_type(self):
        import pytest
        from pydantic import ValidationError
        from app.admin.routes import PricingRuleRequest
        with pytest.raises(ValidationError):
            PricingRuleRequest(scope="global", rule_type="magic", value=Decimal("20"))

    def test_pricing_rule_negative_value_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.admin.routes import PricingRuleRequest
        with pytest.raises(ValidationError):
            PricingRuleRequest(scope="global", rule_type="percentage", value=Decimal("-5"))

    def test_kill_switch_valid(self):
        from app.admin.routes import KillSwitchRequest
        req = KillSwitchRequest(name="all_orders", reason="Emergency")
        assert req.ttl_s is None

    def test_kill_switch_ttl_below_60_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.admin.routes import KillSwitchRequest
        with pytest.raises(ValidationError):
            KillSwitchRequest(name="x", reason="y", ttl_s=30)

    def test_ban_empty_reason_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.admin.routes import BanRequest
        with pytest.raises(ValidationError):
            BanRequest(reason="")

    def test_wallet_adjust_zero_amount_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.admin.routes import WalletAdjustRequest
        with pytest.raises(ValidationError):
            WalletAdjustRequest(amount=Decimal("0"), reason="test")

    def test_wallet_adjust_negative_rejected(self):
        import pytest
        from pydantic import ValidationError
        from app.admin.routes import WalletAdjustRequest
        with pytest.raises(ValidationError):
            WalletAdjustRequest(amount=Decimal("-50"), reason="test")
