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

    # Firebase Cloud Messaging token — set from the customer app on login.
    # Nullable because we support legacy customers who signed up before FCM was live.
    fcm_token = db.Column(db.Text)
    fcm_platform = db.Column(db.String(16))   # 'ios' | 'android'
    fcm_updated_at = db.Column(db.DateTime)

    conversations = db.relationship("Conversation", backref="customer", lazy="dynamic")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "wa_id": self.wa_id,
            "name": self.name or self.wa_id,
            "opted_in": self.opted_in,
        }
