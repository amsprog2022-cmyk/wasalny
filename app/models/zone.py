from datetime import datetime
from app import db


class Zone(db.Model):
    __tablename__ = "zones"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(60), unique=True, nullable=False, index=True)
    name_ar = db.Column(db.String(120), nullable=False)
    name_en = db.Column(db.String(120), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name_ar": self.name_ar,
            "name_en": self.name_en,
            "is_active": self.is_active,
        }


class ZonePricing(db.Model):
    __tablename__ = "zone_pricing"
    __table_args__ = (
        db.UniqueConstraint("from_zone_id", "to_zone_id", name="uq_zone_pair"),
    )

    id = db.Column(db.Integer, primary_key=True)
    from_zone_id = db.Column(
        db.Integer, db.ForeignKey("zones.id"), nullable=False, index=True
    )
    to_zone_id = db.Column(
        db.Integer, db.ForeignKey("zones.id"), nullable=False, index=True
    )
    price_egp = db.Column(db.Numeric(8, 2), nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    from_zone = db.relationship("Zone", foreign_keys=[from_zone_id])
    to_zone = db.relationship("Zone", foreign_keys=[to_zone_id])
