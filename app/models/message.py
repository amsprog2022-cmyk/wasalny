from datetime import datetime
from app import db


DIRECTIONS = ("inbound", "outbound")
STATUSES = ("pending", "sent", "delivered", "read", "failed")
MSG_TYPES = ("text", "template", "image", "audio", "video", "document", "location")


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("conversations.id"), nullable=False, index=True
    )
    wa_message_id = db.Column(db.String(120), index=True)
    direction = db.Column(db.String(10), nullable=False)
    msg_type = db.Column(db.String(20), default="text", nullable=False)
    body = db.Column(db.Text)
    template_name = db.Column(db.String(120))
    media_url = db.Column(db.String(500))
    status = db.Column(db.String(20), default="pending", nullable=False)
    error = db.Column(db.Text)
    sent_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "direction": self.direction,
            "msg_type": self.msg_type,
            "body": self.body,
            "template_name": self.template_name,
            "media_url": self.media_url,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
