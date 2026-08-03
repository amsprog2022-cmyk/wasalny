"""Customer wallet operations.

Every balance change goes through `credit()` / `debit()` which:
  1. Upserts the `CustomerWallet` row.
  2. Updates `balance_egp` atomically inside the same transaction.
  3. Writes a `WalletTransaction` audit row.

`try_debit()` is the safe entry-point for anything that might not
succeed (e.g., captain end-of-trip surcharge when the customer is
short) — it returns False without touching the DB when the balance
can't cover the amount, so the caller can fall back to
`CustomerPendingFee`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app import db
from app.models.wallet import CustomerWallet, WalletTransaction, TXN_REASONS


def _get_or_create(customer_id: int) -> CustomerWallet:
    """Lazy-init the wallet on first touch. Fresh customers start at 0."""
    w = db.session.get(CustomerWallet, customer_id)
    if w is None:
        w = CustomerWallet(customer_id=customer_id, balance_egp=Decimal("0.00"))
        db.session.add(w)
        db.session.flush()  # so subsequent reads in same txn see it
    return w


def get_balance(customer_id: int) -> Decimal:
    w = db.session.get(CustomerWallet, customer_id)
    return Decimal(str(w.balance_egp)) if w else Decimal("0.00")


def credit(
    customer_id: int,
    amount_egp,
    *,
    reason: str,
    ride_id: Optional[int] = None,
    note: Optional[str] = None,
) -> WalletTransaction:
    """Add money to the wallet (top-up, refund, admin adjust)."""
    amount = Decimal(str(amount_egp))
    if amount <= 0:
        raise ValueError("credit amount must be positive")
    if reason not in TXN_REASONS:
        raise ValueError(f"unknown reason: {reason}")
    w = _get_or_create(customer_id)
    w.balance_egp = Decimal(str(w.balance_egp)) + amount
    txn = WalletTransaction(
        customer_id=customer_id, ride_id=ride_id,
        amount_egp=amount, direction="credit", reason=reason,
        balance_after_egp=w.balance_egp, note=note,
    )
    db.session.add(txn)
    return txn


def debit(
    customer_id: int,
    amount_egp,
    *,
    reason: str,
    ride_id: Optional[int] = None,
    note: Optional[str] = None,
) -> WalletTransaction:
    """Deduct — raises when the wallet can't cover it. Prefer `try_debit`
    when the fallback is a pending fee."""
    amount = Decimal(str(amount_egp))
    if amount <= 0:
        raise ValueError("debit amount must be positive")
    if reason not in TXN_REASONS:
        raise ValueError(f"unknown reason: {reason}")
    w = _get_or_create(customer_id)
    if Decimal(str(w.balance_egp)) < amount:
        raise ValueError("insufficient_balance")
    w.balance_egp = Decimal(str(w.balance_egp)) - amount
    txn = WalletTransaction(
        customer_id=customer_id, ride_id=ride_id,
        amount_egp=amount, direction="debit", reason=reason,
        balance_after_egp=w.balance_egp, note=note,
    )
    db.session.add(txn)
    return txn


def try_debit(
    customer_id: int,
    amount_egp,
    *,
    reason: str,
    ride_id: Optional[int] = None,
    note: Optional[str] = None,
) -> Optional[WalletTransaction]:
    """Non-raising debit. Returns the transaction on success, None on
    insufficient balance (caller falls back to pending-fee)."""
    amount = Decimal(str(amount_egp))
    if amount <= 0 or get_balance(customer_id) < amount:
        return None
    return debit(
        customer_id, amount, reason=reason, ride_id=ride_id, note=note,
    )


def recent_transactions(customer_id: int, *, limit: int = 30) -> list[WalletTransaction]:
    return (
        WalletTransaction.query
        .filter_by(customer_id=customer_id)
        .order_by(WalletTransaction.id.desc())
        .limit(limit)
        .all()
    )
