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


def driver_change_held(driver_id: int) -> Decimal:
    """Customer change the captain is still holding since his last settlement.

    Sits in the same ledger as commission but must not count toward the debt
    gate: a captain who does the right thing six times over would otherwise be
    locked offline for it.
    """
    last_settlement = (
        DriverWalletTransaction.query
        .filter_by(driver_id=driver_id, reason="settlement")
        .order_by(DriverWalletTransaction.id.desc())
        .first()
    )
    q = DriverWalletTransaction.query.filter_by(
        driver_id=driver_id, reason="customer_credit", direction="debit",
    )
    if last_settlement is not None:
        q = q.filter(DriverWalletTransaction.id > last_settlement.id)
    total = sum((Decimal(str(t.amount_egp)) for t in q.all()), Decimal("0.00"))
    return total


def driver_change_held_bulk(driver_ids: list[int]) -> dict[int, Decimal]:
    """`driver_change_held` for many captains in one round trip.

    The admin money desk lists every captain in debt; calling the per-driver
    helper in a loop is two queries each and turns one page load into
    hundreds.
    """
    if not driver_ids:
        return {}
    from sqlalchemy import func

    T = DriverWalletTransaction
    last_settlement = (
        db.session.query(T.driver_id, func.max(T.id).label("last_id"))
        .filter(T.driver_id.in_(driver_ids), T.reason == "settlement")
        .group_by(T.driver_id)
        .subquery()
    )
    rows = (
        db.session.query(T.driver_id, func.sum(T.amount_egp))
        .outerjoin(last_settlement, last_settlement.c.driver_id == T.driver_id)
        .filter(
            T.driver_id.in_(driver_ids),
            T.reason == "customer_credit",
            T.direction == "debit",
            (last_settlement.c.last_id.is_(None)) | (T.id > last_settlement.c.last_id),
        )
        .group_by(T.driver_id)
        .all()
    )
    return {did: Decimal(str(total or 0)) for did, total in rows}


def driver_debt_block(driver_id: int) -> tuple[bool, Decimal, Decimal]:
    """`(blocked, owed, cap)` — a captain carrying too much unpaid debt can't go
    online until he settles. Cap of 0 disables the gate.

    Change he parked in customers' wallets is exempt up to a ceiling of its own:
    unlimited exemption would let a captain and a friendly customer recycle cash
    through the wallet forever without ever settling.
    """
    from flask import current_app
    cap = Decimal(str(current_app.config.get("CAPTAIN_MAX_DEBT_EGP", 300) or 0))
    exempt_cap = Decimal(
        str(current_app.config.get("CAPTAIN_MAX_CHANGE_EXEMPT_EGP", 300) or 0)
    )
    owed = driver_owed(driver_id)
    exempt = min(driver_change_held(driver_id), exempt_cap)
    gated = owed - exempt
    return (cap > 0 and gated >= cap), owed, cap


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


def record_change_credit(ride, amount_egp) -> Decimal:
    """The captain had no change, so the difference goes to the customer's
    wallet and he hands over nothing.

    Two postings that cancel out over time: the customer gains credit, the
    captain is debited because he is holding cash that is not his. When the
    customer later spends it, `apply_ride_credit` pushes that ride's
    `cash_due` below `net`, and `post_ride_settlement` credits the second
    captain the same amount back.

    No commission is taken — this is not fare, it is change.
    """
    amount = Decimal(str(amount_egp))
    if amount <= 0:
        raise ValueError("amount must be positive")
    if ride.driver_id is None:
        raise ValueError("ride has no driver")
    credit(
        ride.customer_id, amount, reason="captain_change",
        ride_id=ride.id, note=f"باقي رحلة #{ride.id}",
    )
    driver_debit(
        ride.driver_id, amount, reason="customer_credit",
        ride_id=ride.id, note=f"باقي العميل رحلة #{ride.id}",
    )
    ride.change_credit_egp = amount
    return amount


def post_ride_settlement(ride) -> None:
    """Move a finished ride's money onto the captain's ledger.

    Every ride is cash, so the captain physically holds `cash_due_egp` and
    is entitled to `net_egp`. The difference is what changes hands with the
    platform: normally he owes us the commission, but if wallet credit
    covered more than our cut he collected less than his net and *we* owe
    *him* the shortfall.

    Change parked in the customer's wallet is excluded here even though the
    captain did collect it: `record_change_credit` already posted it as its
    own `customer_credit` debit. Counting it again — `cash_due_egp` includes
    it — would charge him the change twice.
    """
    if ride.driver_id is None:
        return

    collected = (
        Decimal(str(ride.cash_due_egp)) - Decimal(str(ride.change_credit_egp or 0))
    )
    owed = collected - Decimal(str(ride.net_egp))
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
