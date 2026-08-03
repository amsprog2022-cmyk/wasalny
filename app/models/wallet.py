"""Customer + captain wallets and their transaction ledgers.

Balances are authoritative in Postgres. Every debit/credit writes a
transaction row so we always have a full audit trail; the `balance_egp`
column is a running sum kept in-sync by the service layer.

The two wallets mean opposite things, which is the thing to keep straight:

* `CustomerWallet` — credit we owe the customer (refunds, goodwill).
  Never goes below zero. There is no payment provider, so this is never
  spent directly: it comes off the cash the customer hands the captain
  on their next ride.
* `DriverWallet` — a running account with the platform, and it is
  normally *negative*. Rides settle in cash, so the captain pockets the
  full fare and owes us the commission; that debt accrues here until he
  settles up. It only goes positive when we owe him — a wallet discount
  bigger than our cut, or a correction.
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

# Driver-side reasons.
#   commission    — debit; our cut of a cash ride the captain collected.
#   trip_earning  — credit; he collected less cash than his net (wallet
#                   discount covered more than our commission).
#   settlement    — credit; the captain handed cash to an admin.
#   payout        — debit; we paid the captain what we owed him.
DRIVER_TXN_REASONS = (
    "commission", "trip_earning", "settlement", "payout", "admin_adjust",
)


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


class DriverWallet(db.Model):
    __tablename__ = "driver_wallets"

    driver_id = db.Column(
        db.Integer, db.ForeignKey("drivers.id"), primary_key=True
    )
    # Negative = the captain owes the platform. Positive = we owe him.
    balance_egp = db.Column(
        db.Numeric(10, 2), nullable=False, default=Decimal("0.00")
    )
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )

    @property
    def owed_egp(self) -> Decimal:
        """What the captain must hand over, as a positive number. 0 when
        he is square or in credit — the app shows this, not the signed
        balance, because "you owe 40" reads better than "-40"."""
        bal = Decimal(str(self.balance_egp or 0))
        return -bal if bal < 0 else Decimal("0.00")

    def to_dict(self) -> dict:
        return {
            "driver_id": self.driver_id,
            "balance_egp": float(self.balance_egp or 0),
            "owed_egp": float(self.owed_egp),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class DriverWalletTransaction(db.Model):
    __tablename__ = "driver_wallet_transactions"

    id = db.Column(db.Integer, primary_key=True)
    driver_id = db.Column(
        db.Integer, db.ForeignKey("drivers.id"), nullable=False, index=True
    )
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"))
    amount_egp = db.Column(db.Numeric(8, 2), nullable=False)
    direction = db.Column(db.String(10), nullable=False)  # credit | debit
    reason = db.Column(db.String(30), nullable=False)
    balance_after_egp = db.Column(db.Numeric(10, 2))
    note = db.Column(db.String(200))
    # Who recorded a settlement or payout. NULL for automatic postings.
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
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
