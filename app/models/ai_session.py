from datetime import datetime, timedelta
from app import db


AI_SESSION_STATUSES = ("parsing", "handoff", "completed", "failed")


class AiSession(db.Model):
    """Multi-turn conversation state for WhatsApp Gemini parsing.

    When a customer sends a booking message, we open (or reuse) a session so
    a second message like "من الرملة لجامعة بنها" is understood in context.

    Session expires 30 min after the last message.
    """
    __tablename__ = "ai_sessions"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    wa_id = db.Column(db.String(20), nullable=False, index=True)

    status = db.Column(db.String(16), default="parsing", nullable=False)

    partial_pickup_slug = db.Column(db.String(60))
    partial_dropoff_slug = db.Column(db.String(60))
    # How many times we've asked the customer for more info in this session.
    # After the second unsuccessful clarify we escalate to a human admin
    # rather than badger them a third time.
    clarify_count = db.Column(db.Integer, default=0, nullable=False)

    last_message_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def touch(self, ttl_minutes: int = 30) -> None:
        now = datetime.utcnow()
        self.last_message_at = now
        self.expires_at = now + timedelta(minutes=ttl_minutes)


class AdminAlert(db.Model):
    """Anything that needs human attention on the admin dashboard.

    Kinds:
      ai_handoff — Gemini couldn't parse a message; agent needs to take over
      no_driver  — no captain accepted a ride even after zone expansion
      dispute    — customer or captain flagged a completed trip
    """
    __tablename__ = "admin_alerts"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False, index=True)
    payload_json = db.Column(db.Text, nullable=False, default="{}")
    status = db.Column(db.String(20), default="open", nullable=False, index=True)
    handled_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = db.Column(db.DateTime)

    # Optional convenience FKs — nullable, only populated when relevant
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"), index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"))
