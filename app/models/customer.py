from datetime import datetime
from app import db


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    wa_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120))
    opted_in = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    conversations = db.relationship("Conversation", backref="customer", lazy="dynamic")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "wa_id": self.wa_id,
            "name": self.name or self.wa_id,
            "opted_in": self.opted_in,
        }
