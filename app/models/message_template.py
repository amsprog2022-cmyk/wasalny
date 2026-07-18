from datetime import datetime
from app import db


TEMPLATE_CATEGORIES = ("MARKETING", "UTILITY", "AUTHENTICATION")


class MessageTemplate(db.Model):
    __tablename__ = "message_templates"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    language = db.Column(db.String(10), default="ar", nullable=False)
    category = db.Column(db.String(20), default="UTILITY", nullable=False)
    body = db.Column(db.Text, nullable=False)
    variable_count = db.Column(db.Integer, default=0, nullable=False)
    approved = db.Column(db.Boolean, default=False, nullable=False)
    audience = db.Column(db.String(20), default="customer", nullable=False)  # customer / driver
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "language": self.language,
            "category": self.category,
            "body": self.body,
            "variable_count": self.variable_count,
            "approved": self.approved,
            "audience": self.audience,
        }
