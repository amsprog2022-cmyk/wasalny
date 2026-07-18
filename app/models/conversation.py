from datetime import datetime
from app import db


CONV_KINDS = ("customer", "driver")


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False, default="customer")
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=True)
    assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    last_message_preview = db.Column(db.String(500))
    last_message_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_inbound_at = db.Column(db.DateTime)
    unread_count = db.Column(db.Integer, default=0, nullable=False)
    status = db.Column(db.String(20), default="open", nullable=False)  # open / closed
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    messages = db.relationship(
        "Message",
        backref="conversation",
        lazy="dynamic",
        order_by="Message.created_at.asc()",
        cascade="all, delete-orphan",
    )

    def peer_wa_id(self) -> str:
        if self.kind == "customer" and self.customer:
            return self.customer.wa_id
        if self.kind == "driver" and self.driver:
            return self.driver.wa_id
        return ""

    def peer_name(self) -> str:
        if self.kind == "customer" and self.customer:
            return self.customer.name or self.customer.wa_id
        if self.kind == "driver" and self.driver:
            return self.driver.name
        return "Unknown"

    def within_free_window(self) -> bool:
        """True if we can send free-form text (within 24h of last inbound)."""
        if not self.last_inbound_at:
            return False
        return (datetime.utcnow() - self.last_inbound_at).total_seconds() < 24 * 3600

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "peer_wa_id": self.peer_wa_id(),
            "peer_name": self.peer_name(),
            "assignee_id": self.assignee_id,
            "last_message_preview": self.last_message_preview,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "unread_count": self.unread_count,
            "status": self.status,
            "within_free_window": self.within_free_window(),
        }
