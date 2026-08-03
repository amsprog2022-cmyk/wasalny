"""Customer + captain wallet operations.

Every balance change goes through a credit/debit helper which:
  1. Upserts the wallet row.
  2. Updates `balance_egp` inside the caller's transaction.
  3. Writes an audit row.

None of these commit — the caller owns the transaction boundary, so a
wallet move and the ride change that caused it land together or not
at all.

The customer helpers refuse to go below zero. The driver helpers
deliberately allow it: a negative driver balance *is* the commission
the captain owes us on cash rides.

There is no payment provider anywhere in Wassalny — every ride settles
in cash. Customer wallet credit therefore never "pays" for a ride
directly; it reduces the cash the customer hands the captain, and the
platform covers the difference out of its commission.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app import db
from app.models.wallet import (
    CustomerWallet, WalletTransaction, TXN_REASONS,
    DriverWallet, DriverWalletTransaction, DRIVER_TXN_REASONS,
)


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


# ---------------------------------------------------------------- driver


def _get_or_create_driver(driver_id: int) -> DriverWallet:
    w = db.session.get(DriverWallet, driver_id)
    if w is None:
        w = DriverWallet(driver_id=driver_id, balance_egp=Decimal("0.00"))
        db.session.add(w)
        db.session.flush()
    return w


def driver_balance(driver_id: int) -> Decimal:
    """Signed. Negative means the captain owes the platform."""
    w = db.session.get(DriverWallet, driver_id)
    return Decimal(str(w.balance_egp)) if w else Decimal("0.00")


def driver_owed(driver_id: int) -> Decimal:
    """What the captain owes us, as a positive number (0 if square)."""
    bal = driver_balance(driver_id)
    return -bal if bal < 0 else Decimal("0.00")


def driver_debt_block(driver_id: int) -> tuple[bool, Decimal, Decimal]:
    """`(blocked, owed, cap)` — a captain carrying too much unpaid commission
    can't go online until he settles. Cap of 0 disables the gate."""
    from flask import current_app
    cap = Decimal(str(current_app.config.get("CAPTAIN_MAX_DEBT_EGP", 300) or 0))
    owed = driver_owed(driver_id)
    return (cap > 0 and owed >= cap), owed, cap


def settle_driver_cash(
    driver_id: int, amount_egp, *, admin_user_id: int, note: Optional[str] = None
) -> DriverWalletTransaction:
    """The captain handed cash to an admin — clears that much of his debt."""
    return driver_credit(
        driver_id, amount_egp, reason="settlement",
        note=note, created_by_user_id=admin_user_id,
    )


def payout_driver_cash(
    driver_id: int, amount_egp, *, admin_user_id: int, note: Optional[str] = None
) -> DriverWalletTransaction:
    """We handed cash to the captain — reduces what we owe him."""
    return driver_debit(
        driver_id, amount_egp, reason="payout",
        note=note, created_by_user_id=admin_user_id,
    )


def _driver_move(
    driver_id: int,
    amount_egp,
    *,
    direction: str,
    reason: str,
    ride_id: Optional[int] = None,
    note: Optional[str] = None,
    created_by_user_id: Optional[int] = None,
) -> DriverWalletTransaction:
    amount = Decimal(str(amount_egp))
    if amount <= 0:
        raise ValueError("amount must be positive")
    if reason not in DRIVER_TXN_REASONS:
        raise ValueError(f"unknown reason: {reason}")
    w = _get_or_create_driver(driver_id)
    delta = amount if direction == "credit" else -amount
    w.balance_egp = Decimal(str(w.balance_egp)) + delta
    txn = DriverWalletTransaction(
        driver_id=driver_id, ride_id=ride_id,
        amount_egp=amount, direction=direction, reason=reason,
        balance_after_egp=w.balance_egp, note=note,
        created_by_user_id=created_by_user_id,
    )
    db.session.add(txn)
    return txn


def driver_credit(driver_id: int, amount_egp, *, reason: str, **kw) -> DriverWalletTransaction:
    return _driver_move(driver_id, amount_egp, direction="credit", reason=reason, **kw)


def driver_debit(driver_id: int, amount_egp, *, reason: str, **kw) -> DriverWalletTransaction:
    """Allowed to push the balance negative — that is the whole point."""
    return _driver_move(driver_id, amount_egp, direction="debit", reason=reason, **kw)


def driver_recent_transactions(
    driver_id: int, *, limit: int = 30
) -> list[DriverWalletTransaction]:
    return (
        DriverWalletTransaction.query
        .filter_by(driver_id=driver_id)
        .order_by(DriverWalletTransaction.id.desc())
        .limit(limit)
        .all()
    )


def apply_ride_credit(ride) -> Decimal:
    """Spend the customer's wallet credit on a finished ride.

    Runs at completion rather than at booking because that is the first
    moment the total is final — WhatsApp rides are priced by the captain
    on arrival, and any end-of-trip surcharge lands before this. Whatever
    it covers, the captain collects that much less cash.
    """
    already = Decimal(str(ride.wallet_discount_egp or 0))
    if already > 0:
        return already
    total = Decimal(str(ride.total_egp))
    if total <= 0:
        return Decimal("0.00")
    use = min(get_balance(ride.customer_id), total)
    if use <= 0:
        return Decimal("0.00")
    debit(
        ride.customer_id, use, reason="ride_charge",
        ride_id=ride.id, note=f"خصم من رصيدك على رحلة #{ride.id}",
    )
    ride.wallet_discount_egp = use
    return use


def post_ride_settlement(ride, *, extra_commission_egp=None) -> None:
    """Move a finished ride's money onto the captain's ledger.

    Every ride is cash, so the captain physically holds `cash_due_egp` and
    is entitled to `net_egp`. The difference is what changes hands with the
    platform: normally he owes us the commission, but if wallet credit
    covered more than our cut he collected less than his net and *we* owe
    *him* the shortfall.

    `extra_commission_egp` posts only the delta, for when a captain adds a
    surcharge after the ride's commission was already booked.
    """
    if ride.driver_id is None:
        return

    if extra_commission_egp is not None:
        amount = Decimal(str(extra_commission_egp))
        if amount > 0:
            driver_debit(
                ride.driver_id, amount, reason="commission",
                ride_id=ride.id, note=f"عمولة إضافة رحلة #{ride.id}",
            )
        return

    owed = Decimal(str(ride.cash_due_egp)) - Decimal(str(ride.net_egp))
    if owed > 0:
        driver_debit(
            ride.driver_id, owed, reason="commission",
            ride_id=ride.id, note=f"عمولة رحلة #{ride.id}",
        )
    elif owed < 0:
        driver_credit(
            ride.driver_id, -owed, reason="trip_earning",
            ride_id=ride.id, note=f"فرق رصيد رحلة #{ride.id}",
        )
