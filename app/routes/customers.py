"""Admin customer management: search, profile, ban/waive/credit actions."""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import func

from app import db
from app.models.customer import Customer
from app.models.ride import Ride, CustomerPendingFee


customers_bp = Blueprint("customers", __name__, url_prefix="/customers")


@customers_bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    filter_ = request.args.get("filter", "all")

    query = Customer.query
    if q:
        query = query.filter((Customer.name.ilike(f"%{q}%")) | (Customer.wa_id.ilike(f"%{q}%")))

    if filter_ == "active":
        # customers with at least 1 ride in last 30 days
        thirty = datetime.utcnow() - timedelta(days=30)
        active_ids = {
            r.customer_id
            for r in Ride.query.filter(Ride.created_at >= thirty).with_entities(Ride.customer_id).all()
        }
        query = query.filter(Customer.id.in_(active_ids)) if active_ids else query.filter(False)
    elif filter_ == "with_pending":
        pending_ids = {
            r.customer_id
            for r in CustomerPendingFee.query.filter_by(applied_to_ride_id=None, waived_at=None)
            .with_entities(CustomerPendingFee.customer_id)
            .all()
        }
        query = query.filter(Customer.id.in_(pending_ids)) if pending_ids else query.filter(False)

    customers = query.order_by(Customer.id.desc()).limit(200).all()

    # Quick stats per customer for the list
    ride_counts = dict(
        db.session.query(Ride.customer_id, func.count(Ride.id))
        .filter(Ride.customer_id.in_([c.id for c in customers]) if customers else False)
        .group_by(Ride.customer_id)
        .all()
    )
    return render_template(
        "customers/index.html",
        customers=customers,
        ride_counts=ride_counts,
        q=q,
        active_filter=filter_,
    )


@customers_bp.route("/<int:customer_id>")
@login_required
def show(customer_id: int):
    customer = Customer.query.get_or_404(customer_id)

    rides = (
        Ride.query.filter_by(customer_id=customer_id)
        .order_by(Ride.created_at.desc())
        .limit(30)
        .all()
    )
    pending = (
        CustomerPendingFee.query.filter_by(customer_id=customer_id)
        .order_by(CustomerPendingFee.created_at.desc())
        .limit(20)
        .all()
    )
    stats = (
        db.session.query(
            func.count(Ride.id).label("total"),
            func.sum(Ride.price_egp).label("spent"),
        )
        .filter(Ride.customer_id == customer_id, Ride.status == "completed")
        .first()
    )
    return render_template(
        "customers/show.html",
        customer=customer,
        rides=rides,
        pending=pending,
        stats={
            "total": stats.total or 0,
            "spent": float(stats.spent or 0),
        },
    )


@customers_bp.route("/<int:customer_id>/waive-fee/<int:fee_id>", methods=["POST"])
@login_required
def waive_fee(customer_id: int, fee_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("customers.show", customer_id=customer_id))
    fee = CustomerPendingFee.query.get_or_404(fee_id)
    if fee.customer_id != customer_id:
        flash("Fee doesn't belong to this customer.", "error")
        return redirect(url_for("customers.show", customer_id=customer_id))
    fee.waived_at = datetime.utcnow()
    fee.waived_by_admin_id = current_user.id
    fee.waive_reason = request.form.get("reason") or "admin_waived"
    db.session.commit()
    flash("Fee waived.", "success")
    return redirect(url_for("customers.show", customer_id=customer_id))
