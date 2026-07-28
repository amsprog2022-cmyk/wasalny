"""Generic key/value settings row used by admin-editable knobs.

Currently backs the GPS pricing formula + captain commission rate so the
admin can tune fares without a redeploy. Values are stored as strings and
parsed by callers — Decimal for money, float for rates. Falls back to the
static Flask config when a key is missing so first-boot works with no rows.
"""
from datetime import datetime

from app import db


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(255), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
