"""Pricing service — computes the customer-facing quote for a trip.

Rules per PLAN.md:
- Fixed zone-to-zone price from `zone_pricing` (Decision #2)
- Commission = 15% of ride price (Decision #10, config: WASSALNY_COMMISSION_RATE)
- Pending fees (e.g. no-show fee from a prior trip) are added on top (Decision #14)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from flask import current_app

from app import db
from app.models.zone import ZonePricing
from app.models.ride import CustomerPendingFee


@dataclass
class Quote:
    from_zone_id: int
    to_zone_id: int
    ride_price_egp: Decimal
    commission_egp: Decimal
    pending_fees_egp: Decimal
    total_egp: Decimal
    pending_fee_ids: list[int]

    def to_dict(self) -> dict:
        return {
            "from_zone_id": self.from_zone_id,
            "to_zone_id": self.to_zone_id,
            "ride_price_egp": float(self.ride_price_egp),
            "commission_egp": float(self.commission_egp),
            "pending_fees_egp": float(self.pending_fees_egp),
            "total_egp": float(self.total_egp),
            "pending_fee_ids": self.pending_fee_ids,
        }


def _commission_rate() -> Decimal:
    return Decimal(str(current_app.config.get("WASSALNY_COMMISSION_RATE", "0.15")))


def get_pending_fees(customer_id: int) -> tuple[Decimal, list[int]]:
    """Sum of unapplied, unwaived pending fees + their ids."""
    rows = (
        CustomerPendingFee.query.filter_by(
            customer_id=customer_id,
            applied_to_ride_id=None,
            waived_at=None,
        )
        .all()
    )
    total = sum((r.amount_egp for r in rows), Decimal("0"))
    return total, [r.id for r in rows]


def quote(customer_id: int, from_zone_id: int, to_zone_id: int) -> Optional[Quote]:
    """Return the price quote for a would-be booking.

    Falls back to DEFAULT_ZONE_PRICE_EGP when no ZonePricing row exists for
    the pair — with ~350 hyperlocal Benha regions we can't maintain a full
    122k-row matrix. The captain can always override the price on-the-fly
    once they've picked up the customer.
    """
    pricing = ZonePricing.query.filter_by(
        from_zone_id=from_zone_id, to_zone_id=to_zone_id
    ).first()
    if pricing is None:
        default = current_app.config.get("DEFAULT_ZONE_PRICE_EGP", "25")
        ride_price = Decimal(str(default))
    else:
        ride_price = Decimal(pricing.price_egp)
    commission = (ride_price * _commission_rate()).quantize(Decimal("0.01"))
    pending, pending_ids = get_pending_fees(customer_id)
    total = (ride_price + pending).quantize(Decimal("0.01"))
    # ride_price stays untyped (Decimal). Keep the Optional[Quote] contract
    # working — we no longer return None because default fallback exists.
    return Quote(
        from_zone_id=from_zone_id,
        to_zone_id=to_zone_id,
        ride_price_egp=ride_price,
        commission_egp=commission,
        pending_fees_egp=pending,
        total_egp=total,
        pending_fee_ids=pending_ids,
    )


def apply_pending_fees(ride_id: int, pending_fee_ids: list[int]) -> None:
    """Mark the given pending fee rows as applied to this ride."""
    from datetime import datetime
    if not pending_fee_ids:
        return
    now = datetime.utcnow()
    (
        CustomerPendingFee.query.filter(CustomerPendingFee.id.in_(pending_fee_ids))
        .update({"applied_to_ride_id": ride_id, "applied_at": now}, synchronize_session=False)
    )
    db.session.commit()
