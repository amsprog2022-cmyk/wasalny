from datetime import datetime
from app import db


RIDE_STATUSES = ("new", "dispatched", "accepted", "in_progress", "completed", "cancelled")


class RideRequest(db.Model):
    __tablename__ = "ride_requests"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    assigned_driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    pickup = db.Column(db.String(255))
    dropoff = db.Column(db.String(255))
    notes = db.Column(db.Text)
    fare = db.Column(db.Numeric(10, 2))
    status = db.Column(db.String(20), default="new", nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    dispatched_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", backref="ride_requests")
    driver = db.relationship("Driver", backref="ride_requests")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "customer": self.customer.to_dict() if self.customer else None,
            "driver": self.driver.to_dict() if self.driver else None,
            "pickup": self.pickup,
            "dropoff": self.dropoff,
            "notes": self.notes,
            "fare": float(self.fare) if self.fare is not None else None,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
