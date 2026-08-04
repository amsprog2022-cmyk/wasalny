"""Promo-code evaluation — the single source of truth.

Both the quote endpoint and the create endpoint call `evaluate`, so a code
can never validate on the confirm sheet and then fail at booking (or the
reverse). The customer's app never sends us a price to discount; the
server recomputes the fare and applies the percentage to that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from app import db
from app.models.coupon import Coupon
from app.models.ride import Ride


# Coupons are an app-booking promotion. WhatsApp rides have no price at
# booking time (the captain sets it on arrival), and the other service
# kinds go to a manual admin queue with no computed fare.
COUPON_SERVICE_KINDS = ("private",)

# A cancelled ride hands the use back, so these never count against the
# customer's per-customer limit.
_NON_COUNTING_STATUSES = ("cancelled", "cancelled_no_show")


class CouponRejected(ValueError):
    """A code that was fine on the confirm sheet but not at booking.

    Subclasses ValueError so `create_ride`'s existing callers keep working;
    the API layer catches it separately to return the Arabic reason under its
    own key instead of a generic `error` string.
    """

    def __init__(self, message_ar: str, code: str | None = None):
        super().__init__(message_ar)
        self.message_ar = message_ar
        self.error_code = code


@dataclass
class CouponResult:
    coupon_id: int | None = None
    code: str | None = None
    discount_egp: Decimal = Decimal("0.00")
    error: str | None = None
    message_ar: str | None = None

    @property
    def ok(self) -> bool:
        return self.coupon_id is not None and self.error is None


def _fail(error: str, message_ar: str) -> CouponResult:
    return CouponResult(error=error, message_ar=message_ar)


def normalize(code: str | None) -> str | None:
    """Codes are stored uppercase so `ride10` matches `RIDE10`."""
    cleaned = (code or "").strip().upper()
    return cleaned or None


def uses_by_customer(coupon_id: int, customer_id: int) -> int:
    return (
        Ride.query
        .filter(Ride.coupon_id == coupon_id,
                Ride.customer_id == customer_id,
                Ride.status.notin_(_NON_COUNTING_STATUSES))
        .count()
    )


def total_uses(coupon_id: int) -> int:
    return Ride.query.filter(Ride.coupon_id == coupon_id).count()


def evaluate(
    code: str | None,
    *,
    customer_id: int,
    price_egp,
    commission_egp,
    service_kind: str = "private",
) -> CouponResult:
    """Resolve a code into a discount, or into the Arabic reason it can't be used.

    The discount is taken on `price_egp`, not on the ride total — otherwise a
    customer could discount away a pending no-show fee carried over from a
    previous trip.
    """
    code = normalize(code)
    if code is None:
        return CouponResult()

    coupon = Coupon.query.filter(db.func.upper(Coupon.code) == code).first()
    if coupon is None:
        return _fail("not_found", "الكود ده مش موجود.")
    if not coupon.is_active:
        return _fail("inactive", "الكود ده متوقف.")

    now = datetime.utcnow()
    if coupon.starts_at and coupon.starts_at > now:
        return _fail("not_started", "الكود ده لسه مبدأش.")
    if coupon.ends_at is not None and coupon.ends_at <= now:
        return _fail("expired", "الكود ده انتهت صلاحيته.")

    if service_kind not in COUPON_SERVICE_KINDS:
        return _fail("wrong_service", "الكود ده يشتغل على الرحلات الخاصة بس.")

    price = Decimal(str(price_egp or 0))
    min_fare = Decimal(str(coupon.min_fare_egp or 0))
    if price < min_fare:
        return _fail(
            "below_min_fare",
            f"الكود ده على الرحلات من {min_fare.quantize(Decimal('1'))} ج.م وفوق.",
        )

    if uses_by_customer(coupon.id, customer_id) >= (coupon.max_uses_per_customer or 1):
        return _fail("used_up", "استخدمت الكود ده قبل كده.")

    # Capped at the commission so the platform can earn nothing on the ride
    # but never pays out of its own pocket — the captain's `net_egp` is
    # untouched either way.
    raw = (price * Decimal(coupon.discount_pct) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    discount = min(raw, Decimal(str(commission_egp or 0)))
    if discount <= 0:
        return _fail("no_value", "الكود ده مش هيفرق حاجة في الرحلة دي.")

    return CouponResult(coupon_id=coupon.id, code=coupon.code, discount_egp=discount)
