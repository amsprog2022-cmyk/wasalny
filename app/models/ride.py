from datetime import datetime
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
    to_zone_id = db.Column(db.Integer, db.ForeignKey("zones.id"), nullable=False)

    # Money — computed at create time so the customer sees a locked price.
    price_egp = db.Column(db.Numeric(8, 2), nullable=False)
    commission_egp = db.Column(db.Numeric(8, 2), nullable=False)
    no_show_fee_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)

    status = db.Column(db.String(24), default="new", nullable=False, index=True)
    source = db.Column(db.String(16), default="app", nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    assigned_at = db.Column(db.DateTime)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    cancel_reason = db.Column(db.String(120))

    rating = db.Column(db.Integer)          # 1..5
    rating_comment = db.Column(db.Text)

    customer = db.relationship("Customer", backref=db.backref("rides", lazy="dynamic"))
    driver = db.relationship("Driver", backref=db.backref("rides", lazy="dynamic"))
    from_zone = db.relationship("Zone", foreign_keys=[from_zone_id])
    to_zone = db.relationship("Zone", foreign_keys=[to_zone_id])

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
            "price_egp": float(self.price_egp),
            "commission_egp": float(self.commission_egp),
            "no_show_fee_egp": float(self.no_show_fee_egp or 0),
            "status": self.status,
            "source": self.source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "cancel_reason": self.cancel_reason,
            "rating": self.rating,
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
