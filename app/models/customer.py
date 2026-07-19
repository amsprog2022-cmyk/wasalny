from datetime import datetime

import bcrypt

from app import db


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    wa_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120))
    opted_in = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Nullable because legacy customers (registered before we added passwords)
    # need to be prompted to set one on their next login rather than being locked out.
    password_hash = db.Column(db.String(255))

    fcm_token = db.Column(db.Text)
    fcm_platform = db.Column(db.String(16))   # 'ios' | 'android'
    fcm_updated_at = db.Column(db.DateTime)

    # Soft-delete: preserve trip history + referential integrity while
    # blocking login and clearing PII. Required by App Store + Play Store
    # policies (in-app account deletion).
    deleted_at = db.Column(db.DateTime)

    conversations = db.relationship("Conversation", backref="customer", lazy="dynamic")

    def set_password(self, plain: str) -> None:
        self.password_hash = bcrypt.hashpw(
            plain.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

    def check_password(self, plain: str) -> bool:
        if not self.password_hash:
            return False
        return bcrypt.checkpw(
            plain.encode("utf-8"), self.password_hash.encode("utf-8")
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "wa_id": self.wa_id,
            "name": self.name or self.wa_id,
            "opted_in": self.opted_in,
        }
