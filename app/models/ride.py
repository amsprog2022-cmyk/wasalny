from datetime import datetime
from decimal import Decimal

from app import db


# Trip state machine (PLAN §10)
RIDE_STATUSES = (
    "new",
    "broadcasting",
    "assigned",
    "started",
    "completed",
    "cancelled",
    "cancelled_no_show",
)

RIDE_SOURCES = ("whatsapp", "app", "admin")


class Ride(db.Model):
    __tablename__ = "rides"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=True, index=True)

    from_zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=False)
    # to_zone_id is nullable because WhatsApp bookings only capture pickup;
    # the captain sets the destination on arrival. App bookings always set it.
    to_zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=True)

    # Phase 2 GPS booking. Nullable — WhatsApp / legacy zone-only rides
    # leave these NULL and continue to use the zone_id columns above.
    pickup_lat  = db.Column(db.Float)
    pickup_lng  = db.Column(db.Float)
    dropoff_lat = db.Column(db.Float)
    dropoff_lng = db.Column(db.Float)

    # Reverse-geocoded free-form address strings shown in the trip UI
    # (both apps) instead of the coarse zone name. Populated on create for
    # GPS rides; NULL for WhatsApp / legacy zone-only rides (UI falls back
    # to the zone name in that case).
    pickup_address  = db.Column(db.Text)
    dropoff_address = db.Column(db.Text)

    # Money — computed at create time when both zones are known. For WhatsApp
    # rides this starts as 0 and captain overrides via /rides/<id>/price.
    price_egp = db.Column(db.Numeric(8, 2), nullable=False)
    commission_egp = db.Column(db.Numeric(8, 2), nullable=False)
    no_show_fee_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)
    # Retired end-of-trip surcharge. No endpoint writes this any more — the
    # column stays because live rides carry non-zero values and dropping it
    # would rewrite money that was already collected.
    captain_extra_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)
    # Every ride is paid in cash — there is no payment provider. This column is
    # how much of the total was covered by the customer's wallet credit
    # (refunds, admin goodwill), so the captain collects that much less cash.
    # The platform eats it out of its own commission; the captain is always
    # made whole for `net_egp`.
    wallet_discount_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)
    # Promo-code discount, resolved server-side at booking. Like the wallet
    # discount it comes out of the platform's commission, never the captain's
    # `net_egp` — which is why `coupons.evaluate` caps it at `commission_egp`.
    coupon_id = db.Column(db.Integer, db.ForeignKey("coupons.id"), nullable=True, index=True)
    coupon_discount_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)
    # Change the captain couldn't give back in cash, parked in the customer's
    # wallet instead (fare 150, customer hands over 200 → 50 lands here). The
    # customer hands over that much *more* cash now and spends it on a later
    # ride, so it raises `cash_due_egp` but never `total_egp` — the platform
    # takes no commission on change.
    change_credit_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)

    status = db.Column(db.String(24), default="new", nullable=False, index=True)
    # "app" | "whatsapp" | "office". The office value is load-bearing: it is
    # what keeps ride_lifecycle.assign and matching from WhatsApping a customer
    # who never contacted us — the office phoned them, we did not.
    source = db.Column(db.String(16), default="app", nullable=False)
    # Which office pasted this trip in, so the accept confirmation knows where
    # to reply. NULL on every other source.
    office_wa_id = db.Column(db.String(20), index=True)
    # Chained-rides / en-route matching. When a new ride is created and a
    # captain finishing his current trip within 3km of the new pickup is
    # reserved for it, we stamp him here and set status="queued" instead of
    # broadcasting. The customer sees a normal assignment; the captain sees
    # a "طلب قادم بعد الرحلة" badge. On his complete(), we transition this
    # ride to broadcasting and fire the standard offer flow.
    queued_for_driver_id = db.Column(
        db.Integer, db.ForeignKey("drivers.id"), nullable=True, index=True,
    )
    queue_expires_at = db.Column(db.DateTime, nullable=True)
    # Vehicle-type bucket the customer picked on the home cards.
    # "private" is the current auto-broadcast flow; the other three
    # (suzuki, delivery, vip) stay in `broadcasting` waiting for admin
    # manual dispatch — see rides_api.rides_create.
    service_kind = db.Column(db.String(20), default="private", nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    assigned_at = db.Column(db.DateTime)
    # Stamped by ride_lifecycle.arrived() — captain reached the pickup.
    # The "free waiting minutes" grace period is measured from this.
    arrived_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.String(120))

    # Waiting-time charging. Captain toggles a "بدأ الانتظار" button after
    # the grace period at pickup, or any time during the trip when the
    # customer stops him. `waiting_started_at` is only set while a session
    # is open; on stop we add the elapsed seconds into `waiting_seconds`
    # and NULL it. `waiting_price_egp` is finalized inside complete().
    waiting_started_at = db.Column(db.DateTime)
    waiting_seconds = db.Column(db.Integer, default=0, nullable=False)
    waiting_price_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)

    rating = db.Column(db.Integer)          # 1..5
    rating_comment = db.Column(db.Text)

    customer = db.relationship("Customer", backref=db.backref("rides", lazy="dynamic"))
    # Two FKs now point at drivers.id (driver_id + queued_for_driver_id).
    # foreign_keys= disambiguates which one this relationship follows.
    driver = db.relationship(
        "Driver", backref=db.backref("rides", lazy="dynamic"),
        foreign_keys=[driver_id],
    )
    queued_for_driver = db.relationship(
        "Driver", foreign_keys=[queued_for_driver_id],
    )
    from_zone = db.relationship("Zone", foreign_keys=[from_zone_id])
    to_zone = db.relationship("Zone", foreign_keys=[to_zone_id])

    @property
    def total_egp(self) -> Decimal:
        """What the customer actually owes for this ride, all lines included.

        Every money reader should use this rather than `price_egp`, which is
        only the fare agreed at booking.
        """
        return (
            Decimal(str(self.price_egp or 0))
            + Decimal(str(self.no_show_fee_egp or 0))
            + Decimal(str(self.captain_extra_egp or 0))
            + Decimal(str(self.waiting_price_egp or 0))
        )

    @property
    def net_egp(self) -> Decimal:
        """The captain's share — the total minus the platform commission."""
        return self.total_egp - Decimal(str(self.commission_egp or 0))

    @property
    def cash_due_egp(self) -> Decimal:
        """Cash the customer actually hands over — the total less the promo
        code and whatever wallet credit covered, plus any change parked in
        their wallet."""
        due = (
            self.total_egp
            - Decimal(str(self.coupon_discount_egp or 0))
            - Decimal(str(self.wallet_discount_egp or 0))
        )
        if due < 0:
            due = Decimal("0.00")
        return due + Decimal(str(self.change_credit_egp or 0))

    def to_dict(self, *, include_customer_contact: bool = False) -> dict:
        """Serialize the ride.

        `include_customer_contact` — when True, expose the customer's name and
        phone so the captain app can display + tap-to-call. Only pass True in
        endpoints that are authenticated as the driver assigned to this ride
        (or an admin), never in customer-facing responses.
        """
        data = {
            "id": self.id,
            "customer_id": self.customer_id,
            "driver_id": self.driver_id,
            "from_zone_id": self.from_zone_id,
            "to_zone_id": self.to_zone_id,
            "from_zone": self.from_zone.name_ar if self.from_zone else None,
            "to_zone": self.to_zone.name_ar if self.to_zone else None,
            "pickup_lat":  self.pickup_lat,
            "pickup_lng":  self.pickup_lng,
            "dropoff_lat": self.dropoff_lat,
            "dropoff_lng": self.dropoff_lng,
            "pickup_address":  self.pickup_address,
            "dropoff_address": self.dropoff_address,
            "price_egp": float(self.price_egp),
            "commission_egp": float(self.commission_egp),
            "no_show_fee_egp": float(self.no_show_fee_egp or 0),
            "captain_extra_egp": float(self.captain_extra_egp or 0),
            # Cash the customer hands over, and the captain's share of it.
            "total_egp": float(self.total_egp),
            "net_egp": float(self.net_egp),
            "wallet_discount_egp": float(self.wallet_discount_egp or 0),
            "coupon_discount_egp": float(self.coupon_discount_egp or 0),
            "change_credit_egp": float(self.change_credit_egp or 0),
            "cash_due_egp": float(self.cash_due_egp),
            "status": self.status,
            "source": self.source,
            "service_kind": self.service_kind,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "arrived_at": self.arrived_at.isoformat() if self.arrived_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancel_reason": self.cancel_reason,
            "waiting_seconds": int(self.waiting_seconds or 0),
            "waiting_started_at": (
                self.waiting_started_at.isoformat()
                if self.waiting_started_at else None
            ),
            "waiting_price_egp": float(self.waiting_price_egp or 0),
            "rating": self.rating,
            # The customer app gates the captain card *and* the chat button on
            # this, so every path that serializes a ride has to carry it —
            # socket events included, or the first event after assignment wipes
            # the captain out of the trip screen.
            "driver": self.driver.to_dict() if self.driver else None,
        }
        if include_customer_contact and self.customer is not None:
            data["customer"] = {
                "id": self.customer.id,
                "name": self.customer.name or self.customer.wa_id,
                "wa_id": self.customer.wa_id,   # phone in international format
            }
        return data


class Broadcast(db.Model):
    """Audit log of a matching attempt for one ride in one zone."""
    __tablename__ = "broadcasts"

    id = db.Column(db.Integer, primary_key=True)
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"), nullable=False, index=True)
    zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=False)
    driver_ids_json = db.Column(db.Text, nullable=False, default="[]")
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ended_at = db.Column(db.DateTime)
    accepted_by_driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    outcome = db.Column(db.String(20))  # accepted / timeout / expanded / no_drivers


class RideStatusEvent(db.Model):
    """Every state change flows through here for disputes and analytics."""
    __tablename__ = "ride_status_events"

    id = db.Column(db.Integer, primary_key=True)
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"), nullable=False, index=True)
    event = db.Column(db.String(40), nullable=False)
    actor = db.Column(db.String(20), nullable=False)  # customer/driver/admin/system
    payload_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class CustomerPendingFee(db.Model):
    """Fees added to a customer's next trip (e.g. no-show, Decision #14)."""
    __tablename__ = "customer_pending_fees"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    reason = db.Column(db.String(30), nullable=False)
    amount_egp = db.Column(db.Numeric(8, 2), nullable=False)
    from_ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    applied_to_ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"))
    applied_at = db.Column(db.DateTime)

    waived_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    waived_at = db.Column(db.DateTime)
    waive_reason = db.Column(db.String(200))
