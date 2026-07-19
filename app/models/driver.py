from datetime import datetime
import bcrypt
from app import db


DRIVER_CATEGORIES = ("economy", "business", "premium")
DISCIPLINE_STATUSES = ("active", "warned", "suspended", "banned")
APPROVAL_STATUSES = ("pending", "approved", "rejected")


class Driver(db.Model):
    __tablename__ = "drivers"

    id = db.Column(db.Integer, primary_key=True)
    wa_id = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)

    # Auth (admin-issued per Decision #13)
    password_hash = db.Column(db.String(255))
    must_change_password = db.Column(db.Boolean, default=True, nullable=False)

    # Identity documents (admin uploads)
    national_id = db.Column(db.String(30))
    license_number = db.Column(db.String(60))

    # Vehicle
    car_model = db.Column(db.String(80))
    car_plate = db.Column(db.String(30))
    car_color = db.Column(db.String(40))
    category = db.Column(db.String(20), default="economy", nullable=False)
    photo_url = db.Column(db.String(500))

    # Reputation
    rating = db.Column(db.Numeric(3, 2), default=5.00, nullable=False)
    total_trips = db.Column(db.Integer, default=0, nullable=False)

    # Discipline (Decision #12)
    discipline_status = db.Column(db.String(20), default="active", nullable=False)
    suspended_until = db.Column(db.DateTime)

    # Approval (public self-signup → pending; admin flips to approved)
    approval_status = db.Column(
        db.String(20), default="approved", nullable=False, index=True
    )
    approved_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    approved_at = db.Column(db.DateTime)
    signup_source = db.Column(db.String(20), default="admin", nullable=False)  # admin / public

    # Firebase Cloud Messaging token — captain app registers it on login so
    # trip offers can push through when the app is in the background.
    fcm_token = db.Column(db.Text)
    fcm_platform = db.Column(db.String(16))   # 'ios' | 'android'
    fcm_updated_at = db.Column(db.DateTime)

    # Housekeeping
    status = db.Column(db.String(20), default="offline", nullable=False)  # legacy inbox filter
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    conversations = db.relationship("Conversation", backref="driver", lazy="dynamic")

    # ---- password helpers ----
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
            "name": self.name,
            "car_model": self.car_model,
            "car_plate": self.car_plate,
            "car_color": self.car_color,
            "category": self.category,
            "rating": float(self.rating) if self.rating is not None else None,
            "total_trips": self.total_trips,
            "discipline_status": self.discipline_status,
            "is_active": self.is_active,
        }
