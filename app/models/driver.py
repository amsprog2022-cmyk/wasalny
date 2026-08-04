from datetime import datetime
import bcrypt
from app import db


DRIVER_CATEGORIES = ("economy", "business", "premium")
DISCIPLINE_STATUSES = ("active", "warned", "suspended", "banned")
APPROVAL_STATUSES = ("pending", "approved", "rejected")

# The vehicle-type buckets. Only "private" and "intercity" are offered to
# customers now; suzuki/delivery/vip stay in the tuple because live driver
# and ride rows still carry those values and dropping them would break the
# admin filters and historical trip pages.
#
# private   — regular ملاكي car inside Benha, auto-broadcast to nearest captains
# intercity — travel outside Benha. Never auto-matched: the request lands on
#             the /intercity admin board and someone calls the customer back.
SERVICE_KINDS = ("private", "intercity", "suzuki", "delivery", "vip")
SERVICE_KIND_LABELS_AR = {
    "private": "ملاكي داخل بنها",
    "intercity": "سفر خارج بنها",
    "suzuki": "سوزوكي",
    "delivery": "دليفري موتوسيكل",
    "vip": "VIP",
}


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
    # Which of the four customer-facing service cards this driver serves.
    # Admin dispatch of non-private rides is filtered by this column so
    # a سوزوكي request only sees suzuki drivers, etc.
    service_kind = db.Column(db.String(20), default="private", nullable=False)
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

    # Set when the captain proved ownership of the number via reverse OTP
    # (currently only exercised by the forgot-password flow).
    phone_verified_at = db.Column(db.DateTime)

    # Firebase Cloud Messaging token — captain app registers it on login so
    # trip offers can push through when the app is in the background.
    fcm_token = db.Column(db.Text)
    fcm_platform = db.Column(db.String(16))   # 'ios' | 'android'
    fcm_updated_at = db.Column(db.DateTime)

    # Soft-delete: preserves earnings/rides history + referential integrity
    # while blocking login and clearing PII. Required by App Store + Play
    # Store policies (in-app account deletion).
    deleted_at = db.Column(db.DateTime)

    # Live GPS. Redis (driver_positions GEO set) is the hot path; these columns
    # are the durable snapshot updated on trip lifecycle events + when the
    # captain goes online after a long gap. Nullable so captains never issued
    # a position (offline drivers, admin-created seeds) don't break queries.
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    last_position_at = db.Column(db.DateTime)

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

    def to_dict(self, *, include_status: bool = False) -> dict:
        """Serialize the captain.

        `include_status` — expose the captain's own discipline/active state.
        Only for endpoints the captain calls about himself; this rides along
        inside every ride payload otherwise, telling customers which of our
        captains is suspended.
        """
        data = {
            "id": self.id,
            "wa_id": self.wa_id,
            "name": self.name,
            "car_model": self.car_model,
            "car_plate": self.car_plate,
            "car_color": self.car_color,
            "category": self.category,
            "rating": float(self.rating) if self.rating is not None else None,
            "total_trips": self.total_trips,
        }
        if include_status:
            data["discipline_status"] = self.discipline_status
            data["is_active"] = self.is_active
        return data
