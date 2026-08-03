from datetime import datetime

from app import db


INTERCITY_STATUSES = ("open", "contacted", "closed")


class IntercityRequest(db.Model):
    """A "سفر خارج بنها" enquiry that an admin calls back about.

    Deliberately not a Ride and not an AdminAlert: there is no pickup pin,
    no price, no captain to assign. The whole job is "someone from the
    office phones this customer back", so the row only carries what the
    customer said plus who handled it.
    """

    __tablename__ = "intercity_requests"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), index=True)
    wa_id = db.Column(db.String(20), nullable=False, index=True)

    # Stored verbatim. Gemini never touches this branch — the admin needs
    # the customer's own wording ("من بنها للقاهرة بكرة الصبح").
    raw_text = db.Column(db.Text)

    source = db.Column(db.String(20), default="whatsapp", nullable=False)  # whatsapp / app
    status = db.Column(db.String(20), default="open", nullable=False, index=True)

    note = db.Column(db.String(500))
    handled_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    handled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    handled_by = db.relationship("User", foreign_keys=[handled_by_user_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "wa_id": self.wa_id,
            "raw_text": self.raw_text,
            "source": self.source,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
