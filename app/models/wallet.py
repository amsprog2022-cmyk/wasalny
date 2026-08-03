"""Customer wallet + transactions.

Balance is authoritative in Postgres. Every debit/credit writes a
`WalletTransaction` row so we always have a full audit trail; the
`balance_egp` column on `CustomerWallet` is a running sum kept
in-sync by the service layer.

The captain end-of-trip surcharge deducts from this wallet first;
when the balance is insufficient, we fall back to the existing
`CustomerPendingFee` mechanism so the customer eventually pays.
Top-up is a stub for now (returns 501) — the real payment
integration lands later.
"""
from datetime import datetime
from decimal import Decimal

from app import db


class CustomerWallet(db.Model):
    __tablename__ = "customer_wallets"

    # Customer_id is the PK — one wallet per customer, no separate id.
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id"), primary_key=True
    )
    balance_egp = db.Column(
        db.Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "customer_id": self.customer_id,
            "balance_egp": float(self.balance_egp or 0),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# Reasons a wallet moves. Kept as a small closed set so the admin
# dashboard can group by them later without free-text drift.
TXN_REASONS = ("topup", "ride_charge", "refund", "captain_extra", "admin_adjust")
TXN_DIRECTIONS = ("credit", "debit")


class WalletTransaction(db.Model):
    __tablename__ = "wallet_transactions"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True
    )
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"))
    amount_egp = db.Column(db.Numeric(8, 2), nullable=False)
    direction = db.Column(db.String(10), nullable=False)  # credit | debit
    reason = db.Column(db.String(30), nullable=False)
    balance_after_egp = db.Column(db.Numeric(10, 2))
    note = db.Column(db.String(200))
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False, index=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ride_id": self.ride_id,
            "amount_egp": float(self.amount_egp or 0),
            "direction": self.direction,
            "reason": self.reason,
            "balance_after_egp": (
                float(self.balance_after_egp)
                if self.balance_after_egp is not None else None
            ),
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
