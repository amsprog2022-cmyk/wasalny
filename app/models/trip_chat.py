"""In-app chat between the customer and their captain during an active ride.

Distinct from `Message` (WhatsApp/admin inbox) because:
  - Trip-scoped (ride_id FK)
  - Only 2-3 participants (customer, driver, optionally admin)
  - Different UI treatment (WhatsApp bubbles inside the trip screen)

Admin can inject messages here to intervene without exposing driver phone.
"""
from datetime import datetime

from app import db


SENDER_KINDS = ("customer", "driver", "admin")


class TripChatMessage(db.Model):
    __tablename__ = "trip_chat_messages"

    id = db.Column(db.BigInteger, primary_key=True)
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"), nullable=False, index=True)
    sender_kind = db.Column(db.String(16), nullable=False)   # customer / driver / admin
    sender_id = db.Column(db.Integer, nullable=False)         # id in the respective table
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    read_by_customer_at = db.Column(db.DateTime)
    read_by_driver_at = db.Column(db.DateTime)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ride_id": self.ride_id,
            "sender_kind": self.sender_kind,
            "sender_id": self.sender_id,
            "body": self.body,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "read_by_customer": self.read_by_customer_at is not None,
            "read_by_driver": self.read_by_driver_at is not None,
        }
