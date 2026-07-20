"""Observability log for every Gemini AI call.

One row per parse attempt. Kept intentionally lean so we can grep the
last N days for latency P95, error rate, cost forecasting, and spot
customers who trigger unusual patterns (spam / confused / bugs).
"""
from datetime import datetime

from app import db


class GeminiCallLog(db.Model):
    __tablename__ = "gemini_call_logs"

    id = db.Column(db.BigInteger, primary_key=True)
    customer_id = db.Column(db.Integer, index=True)
    wa_id = db.Column(db.String(20), index=True)
    latency_ms = db.Column(db.Integer, nullable=False)
    intent = db.Column(db.String(30))            # book_ride / clarify / chat / ...
    confidence = db.Column(db.Float)
    used_fallback = db.Column(db.Boolean, default=False, nullable=False)
    was_rate_limited = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False, index=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "wa_id": self.wa_id,
            "latency_ms": self.latency_ms,
            "intent": self.intent,
            "confidence": self.confidence,
            "used_fallback": self.used_fallback,
            "was_rate_limited": self.was_rate_limited,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
