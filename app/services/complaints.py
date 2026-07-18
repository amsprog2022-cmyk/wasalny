"""Complaint filing + resolution business logic."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app import db
from app.models.driver import Driver
from app.models.ops import (
    Complaint, ComplaintComment, Ban, CreditAdjustment,
)
from app.services import audit


def file_complaint(
    *,
    filed_by_kind: str,
    filed_by_id: int,
    subject: str,
    description: str | None = None,
    category: str = "other",
    ride_id: int | None = None,
) -> Complaint:
    c = Complaint(
        filed_by_kind=filed_by_kind,
        filed_by_id=filed_by_id,
        subject=subject,
        description=description,
        category=category,
        ride_id=ride_id,
        status="open",
    )
    db.session.add(c)
    db.session.commit()
    return c


def add_comment(complaint_id: int, author_user_id: int, body: str, is_internal: bool = True) -> ComplaintComment:
    cc = ComplaintComment(
        complaint_id=complaint_id,
        author_user_id=author_user_id,
        body=body,
        is_internal=is_internal,
    )
    db.session.add(cc)
    db.session.commit()
    return cc


def resolve(
    complaint_id: int,
    *,
    action: str,
    resolution_text: str,
    refund_amount_egp: Decimal | None = None,
    suspend_captain_hours: int | None = None,
    ban_reason: str | None = None,
) -> Complaint:
    """Terminal transition. Side effects vary by action.

    - refund → creates a CreditAdjustment for the customer
    - warn → sets captain discipline_status='warned'
    - suspend → sets captain suspended_until
    - ban → creates a Ban row against the captain
    - none → just close the ticket
    """
    from datetime import timedelta
    c = Complaint.query.get_or_404(complaint_id)
    before = {"status": c.status, "action": c.resolution_action}

    c.status = "resolved"
    c.resolution = resolution_text
    c.resolution_action = action
    c.resolved_at = datetime.utcnow()

    # Locate related driver + customer via the ride
    ride = c.ride
    driver_id = ride.driver_id if ride else None
    customer_id = ride.customer_id if ride else None

    if action == "refund" and customer_id and refund_amount_egp is not None:
        db.session.add(
            CreditAdjustment(
                target_kind="customer",
                target_id=customer_id,
                amount_egp=refund_amount_egp,
                direction="credit",
                reason=f"refund from complaint #{c.id}",
                from_complaint_id=c.id,
            )
        )
    elif action == "credit" and customer_id and refund_amount_egp is not None:
        db.session.add(
            CreditAdjustment(
                target_kind="customer",
                target_id=customer_id,
                amount_egp=refund_amount_egp,
                direction="credit",
                reason=f"goodwill credit from complaint #{c.id}",
                from_complaint_id=c.id,
            )
        )
    elif action == "warn" and driver_id:
        d = db.session.get(Driver, driver_id)
        if d and d.discipline_status == "active":
            d.discipline_status = "warned"
    elif action == "suspend" and driver_id:
        d = db.session.get(Driver, driver_id)
        if d:
            d.discipline_status = "suspended"
            d.suspended_until = datetime.utcnow() + timedelta(hours=suspend_captain_hours or 24)
    elif action == "ban" and driver_id:
        db.session.add(
            Ban(
                target_kind="driver",
                target_id=driver_id,
                reason=ban_reason or "banned via complaint",
            )
        )
        d = db.session.get(Driver, driver_id)
        if d:
            d.discipline_status = "banned"
            d.is_active = False

    audit.record(
        "complaint.resolve",
        target_kind="complaint",
        target_id=c.id,
        before=before,
        after={"status": c.status, "action": c.resolution_action},
    )
    db.session.commit()
    return c
