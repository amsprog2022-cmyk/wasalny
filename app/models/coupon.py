"""Percent-discount promo codes the admin hands out (RIDE10, etc.).

Redemptions are not tracked in their own table — a use is a row in `rides`
carrying this coupon's id. That way cancelling a ride hands the use back
with no extra bookkeeping.
"""
from datetime import datetime

from app import db


class Coupon(db.Model):
    __tablename__ = "coupons"

    id = db.Column(db.Integer, primary_key=True)
    # Always stored uppercase; every lookup uppercases its input so the
    # customer can type `ride10` and still match `RIDE10`.
    code = db.Column(db.String(24), unique=True, nullable=False, index=True)
    discount_pct = db.Column(db.Integer, nullable=False)      # 1..100
    description_ar = db.Column(db.String(200))

    max_uses_per_customer = db.Column(db.Integer, default=1, nullable=False)
    min_fare_egp = db.Column(db.Numeric(8, 2), default=0, nullable=False)

    starts_at = db.Column(db.DateTime)   # NULL = live from creation
    ends_at = db.Column(db.DateTime)     # NULL = never expires
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def is_live(self) -> bool:
        if not self.is_active:
            return False
        now = datetime.utcnow()
        if self.starts_at and self.starts_at > now:
            return False
        return self.ends_at is None or self.ends_at > now

    @property
    def status_ar(self) -> str:
        if not self.is_active:
            return "متوقف"
        now = datetime.utcnow()
        if self.starts_at and self.starts_at > now:
            return "لسه مبدأش"
        if self.ends_at is not None and self.ends_at <= now:
            return "انتهى"
        return "شغال"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "discount_pct": self.discount_pct,
            "description_ar": self.description_ar,
            "max_uses_per_customer": self.max_uses_per_customer,
            "min_fare_egp": float(self.min_fare_egp or 0),
            "starts_at": self.starts_at.isoformat() if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() if self.ends_at else None,
            "is_active": self.is_active,
        }
