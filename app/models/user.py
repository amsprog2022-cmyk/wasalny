from datetime import datetime
import bcrypt
from flask_login import UserMixin
from app import db


ROLES = ("admin", "dispatcher", "agent")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="agent")
    is_active_user = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    assigned_conversations = db.relationship(
        "Conversation", backref="assignee", lazy="dynamic"
    )

    def set_password(self, plain: str) -> None:
        self.password_hash = bcrypt.hashpw(
            plain.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

    def check_password(self, plain: str) -> bool:
        try:
            return bcrypt.checkpw(
                plain.encode("utf-8"), self.password_hash.encode("utf-8")
            )
        except (ValueError, AttributeError):
            return False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_dispatcher(self) -> bool:
        return self.role in ("admin", "dispatcher")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "is_active": self.is_active_user,
        }
