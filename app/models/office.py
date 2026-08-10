"""WhatsApp numbers that belong to a Wassalny office rather than a customer.

The office takes trips by phone call and pastes the caller's number (plus
usually a place) into WhatsApp. Messages from these numbers skip the customer
AI booking pipeline entirely and go through app.services.office_dispatch.
"""
from datetime import datetime

from app import db


class OfficeNumber(db.Model):
    __tablename__ = "office_numbers"

    id = db.Column(db.Integer, primary_key=True)
    # Stored the way Meta sends it on the webhook: bare international, no
    # leading "+" (201050084115), so an inbound msg["from"] matches directly.
    wa_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    label = db.Column(db.String(80))
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def status_ar(self) -> str:
        return "شغال" if self.is_active else "متوقف"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "wa_id": self.wa_id,
            "label": self.label,
            "is_active": self.is_active,
        }


class ServiceNumber(db.Model):
    """Public phone numbers we hand out on WhatsApp for depot services.

    Different from OfficeNumber, which is inbound (an office pastes a
    customer number to us). This one is outbound — the customer picks
    "خدمات تانية" off the WhatsApp menu and gets back a formatted list
    of every active row here (Suzuki depot, delivery guy, Toyota, ...).
    """
    __tablename__ = "service_numbers"

    id = db.Column(db.Integer, primary_key=True)
    service_label = db.Column(db.String(80), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def status_ar(self) -> str:
        return "شغال" if self.is_active else "متوقف"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "service_label": self.service_label,
            "phone": self.phone,
            "is_active": self.is_active,
        }
