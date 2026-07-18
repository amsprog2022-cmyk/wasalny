"""Captain rates the customer back — separate table to avoid schema drift on
the existing Ride model in production. Runs alongside `Ride.rating` (customer's
rating of the captain).
"""
from datetime import datetime
from app import db


class CaptainRatingOfCustomer(db.Model):
    __tablename__ = "captain_ratings_of_customer"
    __table_args__ = (
        db.UniqueConstraint("ride_id", "driver_id", name="uq_captain_rating_ride_driver"),
    )

    id = db.Column(db.Integer, primary_key=True)
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"), nullable=False, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    stars = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
