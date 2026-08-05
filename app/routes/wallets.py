"""Admin money desk — captain debt settlement and customer credit.

Rides are cash, so this page is where the cash actually comes back to the
platform: a captain hands over the commission he owes and an admin records
it here, which is the only thing that clears his debt and unblocks him from
going online.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.wallet import CustomerWallet, DriverWallet
from app.services import audit
from app.services import push_notifications as push
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
    # Part of that debt is customer change the captain parked in wallets, not
    # commission. It still has to come back, but it does not mean he has been
    # sitting on our cut, so the desk should be able to tell them apart.
    change_held = wallet_svc.driver_change_held_bulk([d.id for _, d in debtors])
    return render_template(
        "wallets/index.html",
        debtors=debtors,
        creditors=creditors,
        customer_credit=customer_credit,
        total_owed=total_owed,
        change_held=change_held,
        total_change_held=sum(change_held.values(), Decimal("0")),
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


@wallets_bp.route("/driver/<int:driver_id>/settle-all", methods=["POST"])
@login_required
def driver_settle_all(driver_id: int):
    """One-click: captain paid the full debt in cash. Settles the outstanding
    balance in a single wallet transaction and unblocks him (if he was
    blocked from rides because of the debt)."""
    bal = wallet_svc.driver_balance(driver_id)
    if bal >= 0:
        flash("مفيش مديونية على الكابتن ده.", "warning")
        return redirect(url_for("wallets.index"))
    amount = -bal  # bal is negative; owed amount is positive
    wallet_svc.settle_driver_cash(
        driver_id, amount,
        admin_user_id=current_user.id,
        note="Full settlement",
    )
    driver = db.session.get(Driver, driver_id)
    unblocked = False
    if driver and driver.discipline_status == "suspended":
        driver.discipline_status = "active"
        driver.suspended_until = None
        unblocked = True
    audit.record(
        "wallet.driver_settle_all", target_kind="driver", target_id=driver_id,
        before={"balance_egp": float(bal), "unblocked": unblocked},
        after={"balance_egp": float(wallet_svc.driver_balance(driver_id))},
    )
    db.session.commit()
    try:
        push.send_to_driver(
            driver_id,
            title="✅ الحساب اتسدد",
            body=f"استلمنا {amount:.0f} ج.م. يلا شغال تاني.",
            data={"kind": "wallet_settled"},
        )
    except Exception:
        pass
    msg = f"اتحصل {amount} ج.م والحساب صفر."
    if unblocked:
        msg += " والكابتن يقدر يشتغل تاني."
    flash(msg, "success")
    return redirect(url_for("wallets.index"))


@wallets_bp.route("/driver/<int:driver_id>/block", methods=["POST"])
@login_required
def driver_block(driver_id: int):
    """Stop the captain from receiving trips until he clears his debt.
    Matching engine skips discipline_status='suspended'."""
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("wallets.index"))
    driver = db.session.get(Driver, driver_id)
    if driver is None:
        flash("Captain not found.", "error")
        return redirect(url_for("wallets.index"))
    before = driver.discipline_status
    driver.discipline_status = "suspended"
    driver.suspended_until = None  # indefinite until admin unblocks
    db.session.commit()
    audit.record(
        "driver.block", target_kind="driver", target_id=driver_id,
        before={"discipline_status": before},
        after={"discipline_status": "suspended", "reason": "wallet_debt"},
    )
    owed = -wallet_svc.driver_balance(driver_id)
    try:
        push.send_to_driver(
            driver_id,
            title="🚫 اتوقفت عن الرحلات",
            body=(
                f"عليك {owed:.0f} ج.م. عدّي على الإدارة سدّد الفلوس "
                "علشان تشتغل تاني."
                if owed > 0 else
                "الإدارة أوقفتك مؤقتًا. كلّمهم علشان يفتحوا حسابك."
            ),
            data={"kind": "driver_blocked"},
        )
    except Exception:
        pass
    flash("الكابتن اتمنع من الرحلات لحد ما يسدد.", "success")
    return redirect(url_for("wallets.index"))


@wallets_bp.route("/driver/<int:driver_id>/unblock", methods=["POST"])
@login_required
def driver_unblock(driver_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("wallets.index"))
    driver = db.session.get(Driver, driver_id)
    if driver is None:
        flash("Captain not found.", "error")
        return redirect(url_for("wallets.index"))
    before = driver.discipline_status
    driver.discipline_status = "active"
    driver.suspended_until = None
    db.session.commit()
    audit.record(
        "driver.unblock", target_kind="driver", target_id=driver_id,
        before={"discipline_status": before},
        after={"discipline_status": "active"},
    )
    try:
        push.send_to_driver(
            driver_id,
            title="✅ رجّعناك للرحلات",
            body="حسابك اتفتح تاني. يلا شغال.",
            data={"kind": "driver_unblocked"},
        )
    except Exception:
        pass
    flash("الكابتن رجع يقدر يشتغل.", "success")
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
