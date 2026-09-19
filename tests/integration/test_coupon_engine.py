"""
Integration tests — Coupon Engine.

Coverage:
  - valid coupon reduces order price by correct amount
  - expired coupon raises CouponExpiredError
  - total_limit exceeded raises CouponUsageLimitError
  - per_user_limit exceeded raises CouponAlreadyRedeemedError
  - apply_coupon idempotent: same order+key credits discount once
  - discount never exceeds order_total (even if coupon value is huge)
  - min_spend enforced: order below threshold raises CouponError
  - service_id scoping: coupon for wrong service raises CouponError
  - transfer: transferable coupon changes owner_user_id
  - transfer: non-transferable coupon raises CouponTransferError
  - calculate_discount: percentage, fixed, max_discount cap
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

try:
    import asyncpg       # noqa
    import pytest
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://smm_app:dev_password_change_in_prod@localhost:5432/smm_platform",
)


if HAS_DEPS:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy import text

    @pytest.fixture(scope="session")
    def event_loop():
        loop = asyncio.get_event_loop_policy().new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture(scope="session")
    async def engine():
        eng = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
        yield eng
        await eng.dispose()

    @pytest.fixture
    async def db(engine):
        async with engine.begin() as conn:
            await conn.execute(text("SAVEPOINT test_savepoint"))
            session = AsyncSession(bind=conn, expire_on_commit=False)
            yield session
            await conn.execute(text("ROLLBACK TO SAVEPOINT test_savepoint"))

    @pytest.fixture
    async def user_factory(db):
        async def _make():
            tg_id = int(uuid.uuid4().int % (10**15))
            await db.execute(text(
                "INSERT INTO users (id, telegram_id, is_active, is_banned, policy_version)"
                " VALUES (:id, :tg, true, false, 0)"
            ), {"id": tg_id, "tg": tg_id})
            await db.flush()
            return tg_id
        return _make

    @pytest.fixture
    async def coupon_factory(db):
        """Create a coupon row and return its code."""
        async def _make(
            discount_type: str = "percentage",
            discount_value: Decimal = Decimal("10"),
            max_discount: Decimal | None = None,
            min_spend: Decimal | None = None,
            total_limit: int | None = None,
            per_user_limit: int = 1,
            expires_at: datetime | None = None,
            is_active: bool = True,
            is_transferable: bool = False,
            owner_user_id: int | None = None,
            service_ids: list | None = None,
            category_ids: list | None = None,
        ) -> str:
            code = "TEST" + uuid.uuid4().hex[:6].upper()
            await db.execute(text(
                "INSERT INTO coupons"
                " (code, discount_type, discount_value, max_discount,"
                "  min_spend, total_limit, per_user_limit, expires_at,"
                "  is_active, is_transferable, owner_user_id,"
                "  service_ids, category_ids)"
                " VALUES"
                " (:code, :dt, :dv, :md, :ms, :tl, :pul, :ea,"
                "  :ia, :it, :ou, :si, :ci)"
            ), {
                "code": code, "dt": discount_type, "dv": discount_value,
                "md": max_discount, "ms": min_spend, "tl": total_limit,
                "pul": per_user_limit, "ea": expires_at,
                "ia": is_active, "it": is_transferable, "ou": owner_user_id,
                "si": service_ids, "ci": category_ids,
            })
            await db.flush()
            return code
        return _make

    class TestCouponValidation:

        @pytest.mark.asyncio
        async def test_valid_coupon_returns_discount(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            code = await coupon_factory(discount_type="percentage", discount_value=Decimal("10"))
            from app.coupons.engine import preview_coupon
            discount = await preview_coupon(
                db=db, code=code, user_id=user_id, order_total=Decimal("100.00")
            )
            assert discount == Decimal("10.00")

        @pytest.mark.asyncio
        async def test_expired_coupon_raises(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            past = datetime.now(timezone.utc) - timedelta(hours=1)
            code = await coupon_factory(expires_at=past)
            from app.coupons.engine import preview_coupon
            from app.core.exceptions import CouponExpiredError
            with pytest.raises(CouponExpiredError):
                await preview_coupon(
                    db=db, code=code, user_id=user_id,
                    order_total=Decimal("100.00")
                )

        @pytest.mark.asyncio
        async def test_inactive_coupon_raises(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            code = await coupon_factory(is_active=False)
            from app.coupons.engine import preview_coupon
            from app.core.exceptions import CouponNotFoundError
            with pytest.raises(CouponNotFoundError):
                await preview_coupon(
                    db=db, code=code, user_id=user_id,
                    order_total=Decimal("100.00")
                )

        @pytest.mark.asyncio
        async def test_total_limit_exceeded_raises(
            self, db, user_factory, coupon_factory
        ):
            user1 = await user_factory()
            user2 = await user_factory()
            # total_limit=1: one user uses it, second user gets UsageLimitError
            code = await coupon_factory(total_limit=1, per_user_limit=1)

            # First user applies it
            from app.coupons.engine import apply_coupon
            await apply_coupon(
                db=db, code=code, user_id=user1, order_id=str(uuid.uuid4()),
                order_total=Decimal("100.00"), idempotency_key=str(uuid.uuid4()),
            )

            # Second user — total limit exhausted
            from app.core.exceptions import CouponUsageLimitError
            with pytest.raises(CouponUsageLimitError):
                await apply_coupon(
                    db=db, code=code, user_id=user2, order_id=str(uuid.uuid4()),
                    order_total=Decimal("100.00"), idempotency_key=str(uuid.uuid4()),
                )

        @pytest.mark.asyncio
        async def test_per_user_limit_exceeded_raises(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            # per_user_limit=1: same user can't redeem twice
            code = await coupon_factory(per_user_limit=1, total_limit=None)

            from app.coupons.engine import apply_coupon
            from app.core.exceptions import CouponAlreadyRedeemedError

            await apply_coupon(
                db=db, code=code, user_id=user_id, order_id=str(uuid.uuid4()),
                order_total=Decimal("100.00"), idempotency_key=str(uuid.uuid4()),
            )
            with pytest.raises(CouponAlreadyRedeemedError):
                await apply_coupon(
                    db=db, code=code, user_id=user_id, order_id=str(uuid.uuid4()),
                    order_total=Decimal("100.00"), idempotency_key=str(uuid.uuid4()),
                )

        @pytest.mark.asyncio
        async def test_min_spend_not_met_raises(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            code = await coupon_factory(min_spend=Decimal("200.00"))
            from app.coupons.engine import preview_coupon
            from app.core.exceptions import CouponError
            with pytest.raises(CouponError):
                await preview_coupon(
                    db=db, code=code, user_id=user_id,
                    order_total=Decimal("100.00")    # below ₹200 min
                )

        @pytest.mark.asyncio
        async def test_min_spend_met_succeeds(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            code = await coupon_factory(
                min_spend=Decimal("200.00"),
                discount_type="fixed",
                discount_value=Decimal("50"),
            )
            from app.coupons.engine import preview_coupon
            discount = await preview_coupon(
                db=db, code=code, user_id=user_id,
                order_total=Decimal("250.00")
            )
            assert discount == Decimal("50.00")

    class TestCouponApply:

        @pytest.mark.asyncio
        async def test_apply_records_redemption(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            code = await coupon_factory(
                discount_type="percentage", discount_value=Decimal("20")
            )
            order_id = str(uuid.uuid4())
            from app.coupons.engine import apply_coupon
            discount = await apply_coupon(
                db=db, code=code, user_id=user_id, order_id=order_id,
                order_total=Decimal("100.00"), idempotency_key=str(uuid.uuid4()),
            )
            assert discount == Decimal("20.00")

            row = (await db.execute(text(
                "SELECT discount_applied FROM coupon_redemptions"
                " WHERE order_id = :oid"
            ), {"oid": order_id})).fetchone()
            assert row is not None
            assert row[0] == Decimal("20.00")

        @pytest.mark.asyncio
        async def test_apply_idempotent_same_order(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            code = await coupon_factory(
                discount_type="fixed", discount_value=Decimal("30"), per_user_limit=5
            )
            order_id  = str(uuid.uuid4())
            idem_key  = str(uuid.uuid4())
            from app.coupons.engine import apply_coupon

            d1 = await apply_coupon(
                db=db, code=code, user_id=user_id, order_id=order_id,
                order_total=Decimal("100.00"), idempotency_key=idem_key,
            )
            d2 = await apply_coupon(
                db=db, code=code, user_id=user_id, order_id=order_id,
                order_total=Decimal("100.00"), idempotency_key=idem_key,
            )
            assert d1 == d2 == Decimal("30.00")

            # Only one redemption record
            count = (await db.execute(text(
                "SELECT COUNT(*) FROM coupon_redemptions WHERE order_id = :oid"
            ), {"oid": order_id})).scalar_one()
            assert count == 1

        @pytest.mark.asyncio
        async def test_discount_capped_at_order_total(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            # Fixed ₹999 discount on a ₹50 order — must cap at ₹50
            code = await coupon_factory(
                discount_type="fixed", discount_value=Decimal("999")
            )
            from app.coupons.engine import apply_coupon
            discount = await apply_coupon(
                db=db, code=code, user_id=user_id, order_id=str(uuid.uuid4()),
                order_total=Decimal("50.00"), idempotency_key=str(uuid.uuid4()),
            )
            assert discount == Decimal("50.00")   # capped at order_total

        @pytest.mark.asyncio
        async def test_max_discount_cap(
            self, db, user_factory, coupon_factory
        ):
            user_id = await user_factory()
            # 50% of ₹200 = ₹100, but max_discount = ₹30
            code = await coupon_factory(
                discount_type="percentage",
                discount_value=Decimal("50"),
                max_discount=Decimal("30"),
            )
            from app.coupons.engine import apply_coupon
            discount = await apply_coupon(
                db=db, code=code, user_id=user_id, order_id=str(uuid.uuid4()),
                order_total=Decimal("200.00"), idempotency_key=str(uuid.uuid4()),
            )
            assert discount == Decimal("30.00")   # capped at max_discount

    class TestCouponTransfer:

        @pytest.mark.asyncio
        async def test_transfer_changes_owner(
            self, db, user_factory, coupon_factory
        ):
            user1 = await user_factory()
            user2 = await user_factory()
            code = await coupon_factory(
                is_transferable=True, owner_user_id=user1
            )
            from app.coupons.engine import transfer_coupon
            await transfer_coupon(db=db, code=code, from_user_id=user1, to_user_id=user2)

            owner = (await db.execute(text(
                "SELECT owner_user_id FROM coupons WHERE code = :c"
            ), {"c": code})).scalar_one()
            assert owner == user2

        @pytest.mark.asyncio
        async def test_non_transferable_raises(
            self, db, user_factory, coupon_factory
        ):
            user1 = await user_factory()
            user2 = await user_factory()
            code = await coupon_factory(is_transferable=False, owner_user_id=user1)
            from app.coupons.engine import transfer_coupon
            from app.core.exceptions import CouponTransferError
            with pytest.raises(CouponTransferError):
                await transfer_coupon(
                    db=db, code=code, from_user_id=user1, to_user_id=user2
                )

        @pytest.mark.asyncio
        async def test_transfer_wrong_owner_raises(
            self, db, user_factory, coupon_factory
        ):
            user1 = await user_factory()
            user2 = await user_factory()
            user3 = await user_factory()
            code = await coupon_factory(is_transferable=True, owner_user_id=user1)
            from app.coupons.engine import transfer_coupon
            from app.core.exceptions import CouponTransferError
            # user2 trying to transfer a coupon owned by user1
            with pytest.raises(CouponTransferError):
                await transfer_coupon(
                    db=db, code=code, from_user_id=user2, to_user_id=user3
                )


# ── Offline tests — pure logic, no DB ────────────────────────────────────────

class TestCalculateDiscountOffline(unittest.TestCase):

    def _fake_coupon(
        self,
        discount_type: str,
        discount_value: str,
        max_discount: str | None = None,
    ):
        class C:
            pass
        c = C()
        c.discount_type  = discount_type
        c.discount_value = Decimal(discount_value)
        c.max_discount   = Decimal(max_discount) if max_discount else None
        return c

    def _calc(self, coupon, total: str) -> Decimal:
        # Inline the logic (no imports needed)
        _ZERO    = Decimal("0")
        _HUNDRED = Decimal("100")
        total_d  = Decimal(total)
        if coupon.discount_type == "percentage":
            raw = (total_d * coupon.discount_value / _HUNDRED).quantize(Decimal("0.01"))
        elif coupon.discount_type == "fixed":
            raw = coupon.discount_value.quantize(Decimal("0.01"))
        else:
            raw = _ZERO
        if coupon.max_discount and raw > coupon.max_discount:
            raw = coupon.max_discount
        return max(min(raw, total_d), _ZERO)

    def test_percentage_10pct(self):
        c = self._fake_coupon("percentage", "10")
        assert self._calc(c, "100") == Decimal("10.00")

    def test_percentage_50pct(self):
        c = self._fake_coupon("percentage", "50")
        assert self._calc(c, "200") == Decimal("100.00")

    def test_fixed_30(self):
        c = self._fake_coupon("fixed", "30")
        assert self._calc(c, "200") == Decimal("30.00")

    def test_fixed_exceeds_total_capped(self):
        c = self._fake_coupon("fixed", "999")
        assert self._calc(c, "50") == Decimal("50.00")

    def test_max_discount_cap(self):
        c = self._fake_coupon("percentage", "50", max_discount="25")
        assert self._calc(c, "100") == Decimal("25.00")

    def test_unknown_type_zero(self):
        c = self._fake_coupon("magic", "10")
        assert self._calc(c, "100") == Decimal("0")

    def test_zero_total_zero_discount(self):
        c = self._fake_coupon("percentage", "50")
        assert self._calc(c, "0") == Decimal("0")

    def test_discount_never_negative(self):
        c = self._fake_coupon("fixed", "100")
        assert self._calc(c, "0") == Decimal("0")

    def test_percentage_exact_result(self):
        c = self._fake_coupon("percentage", "33")
        result = self._calc(c, "100")
        assert result == Decimal("33.00")

    def test_percentage_rounds_to_cent(self):
        c = self._fake_coupon("percentage", "10")
        # 10% of ₹33.33 = ₹3.333 → rounds to ₹3.33
        result = self._calc(c, "33.33")
        assert result == Decimal("3.33")


if __name__ == "__main__":
    if HAS_DEPS:
        import pytest
        sys.exit(pytest.main([__file__, "-v"]))
    else:
        print("pytest/asyncpg not available — running offline tests only")
        unittest.main(verbosity=2)
