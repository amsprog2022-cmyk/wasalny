"""Admin money desk — captain debt settlement and customer credit.

Rides are cash, so this page is where the cash actually comes back to the
platform: a captain hands over the commission he owes and an admin records
it here, which is the only thing that clears his debt and unblocks him from
going online.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.wallet import CustomerWallet, DriverWallet
from app.services import audit
from app.services import wallet as wallet_svc


wallets_bp = Blueprint("wallets", __name__, url_prefix="/wallets")


def _amount(raw: str | None) -> Decimal | None:
    try:
        amount = Decimal(str(raw or "").strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount > 0 else None


@wallets_bp.route("/")
@login_required
def index():
    debtors = (
        db.session.query(DriverWallet, Driver)
        .join(Driver, Driver.id == DriverWallet.driver_id)
        .filter(DriverWallet.balance_egp < 0)
        .order_by(DriverWallet.balance_egp.asc())
        .all()
    )
    creditors = (
        db.session.query(DriverWallet, Driver)
        .join(Driver, Driver.id == DriverWallet.driver_id)
        .filter(DriverWallet.balance_egp > 0)
        .order_by(DriverWallet.balance_egp.desc())
        .all()
    )
    customer_credit = (
        db.session.query(CustomerWallet, Customer)
        .join(Customer, Customer.id == CustomerWallet.customer_id)
        .filter(CustomerWallet.balance_egp > 0)
        .order_by(CustomerWallet.balance_egp.desc())
        .limit(100)
        .all()
    )
    total_owed = sum((-w.balance_egp for w, _ in debtors), Decimal("0"))
    return render_template(
        "wallets/index.html",
        debtors=debtors,
        creditors=creditors,
        customer_credit=customer_credit,
        total_owed=total_owed,
    )


@wallets_bp.route("/driver/<int:driver_id>/settle", methods=["POST"])
@login_required
def driver_settle(driver_id: int):
    """Captain handed cash to an admin."""
    amount = _amount(request.form.get("amount_egp"))
    if amount is None:
        flash("اكتب مبلغ صحيح.", "error")
        return redirect(url_for("wallets.index"))
    before = float(wallet_svc.driver_balance(driver_id))
    wallet_svc.settle_driver_cash(
        driver_id, amount,
        admin_user_id=current_user.id,
        note=(request.form.get("note") or "").strip() or None,
    )
    audit.record(
        "wallet.driver_settle", target_kind="driver", target_id=driver_id,
        before={"balance_egp": before},
        after={"balance_egp": float(wallet_svc.driver_balance(driver_id))},
    )
    db.session.commit()
    flash(f"اتسجل تحصيل {amount} ج.م.", "success")
    return redirect(url_for("wallets.index"))


@wallets_bp.route("/driver/<int:driver_id>/payout", methods=["POST"])
@login_required
def driver_payout(driver_id: int):
    """We handed cash to the captain."""
    amount = _amount(request.form.get("amount_egp"))
    if amount is None:
        flash("اكتب مبلغ صحيح.", "error")
        return redirect(url_for("wallets.index"))
    before = float(wallet_svc.driver_balance(driver_id))
    wallet_svc.payout_driver_cash(
        driver_id, amount,
        admin_user_id=current_user.id,
        note=(request.form.get("note") or "").strip() or None,
    )
    audit.record(
        "wallet.driver_payout", target_kind="driver", target_id=driver_id,
        before={"balance_egp": before},
        after={"balance_egp": float(wallet_svc.driver_balance(driver_id))},
    )
    db.session.commit()
    flash(f"اتسجل صرف {amount} ج.م.", "success")
    return redirect(url_for("wallets.index"))


@wallets_bp.route("/customer/<int:customer_id>/adjust", methods=["POST"])
@login_required
def customer_adjust(customer_id: int):
    """Give or take back customer credit. Credit comes off the cash due on
    their next completed ride."""
    amount = _amount(request.form.get("amount_egp"))
    direction = request.form.get("direction") or "credit"
    if amount is None:
        flash("اكتب مبلغ صحيح.", "error")
        return redirect(request.referrer or url_for("wallets.index"))
    before = float(wallet_svc.get_balance(customer_id))
    note = (request.form.get("note") or "").strip() or None
    try:
        if direction == "debit":
            wallet_svc.debit(customer_id, amount, reason="admin_adjust", note=note)
        else:
            wallet_svc.credit(customer_id, amount, reason="admin_adjust", note=note)
    except ValueError as e:
        db.session.rollback()
        flash("الرصيد مش كفاية." if str(e) == "insufficient_balance" else str(e), "error")
        return redirect(request.referrer or url_for("wallets.index"))
    audit.record(
        "wallet.customer_adjust", target_kind="customer", target_id=customer_id,
        before={"balance_egp": before},
        after={"balance_egp": float(wallet_svc.get_balance(customer_id))},
    )
    db.session.commit()
    flash(f"اتعدل رصيد العميل بـ {amount} ج.م.", "success")
    return redirect(request.referrer or url_for("wallets.index"))
