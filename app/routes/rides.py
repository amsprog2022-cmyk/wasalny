"""Admin dashboard rides view — using the real Ride model (Phase 2)."""
from __future__ import annotations

import json

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app import db
from app.models.ride import Ride, RideStatusEvent, Broadcast
from app.services import ride_lifecycle


rides_bp = Blueprint("rides", __name__, url_prefix="/rides")


STATUS_TABS = [
    ("active", "Active", ["broadcasting", "assigned", "started"]),
    ("new", "Waiting", ["new"]),
    ("completed", "Completed", ["completed"]),
    ("cancelled", "Cancelled", ["cancelled", "cancelled_no_show"]),
    ("all", "All", None),
]


@rides_bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "active")
    filter_ = next((t for t in STATUS_TABS if t[0] == tab), STATUS_TABS[0])
    q = Ride.query
    if filter_[2] is not None:
        q = q.filter(Ride.status.in_(filter_[2]))
    rides = q.order_by(Ride.created_at.desc()).limit(200).all()
    counts = {t[0]: Ride.query.filter(Ride.status.in_(t[2])).count() if t[2] else Ride.query.count() for t in STATUS_TABS}
    return render_template(
        "rides/index.html",
        rides=rides,
        counts=counts,
        tabs=STATUS_TABS,
        active_tab=tab,
    )


@rides_bp.route("/<int:ride_id>")
@login_required
def show(ride_id: int):
    ride = Ride.query.get_or_404(ride_id)
    events = (
        RideStatusEvent.query.filter_by(ride_id=ride_id)
        .order_by(RideStatusEvent.created_at.asc())
        .all()
    )
    broadcasts = (
        Broadcast.query.filter_by(ride_id=ride_id)
        .order_by(Broadcast.started_at.asc())
        .all()
    )
    return render_template(
        "rides/show.html",
        ride=ride,
        events=events,
        broadcasts=broadcasts,
        parse_json=json.loads,
    )


@rides_bp.route("/<int:ride_id>/cancel", methods=["POST"])
@login_required
def admin_cancel(ride_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("rides.show", ride_id=ride_id))
    ride = Ride.query.get_or_404(ride_id)
    reason = request.form.get("reason") or "admin_override"
    try:
        ride_lifecycle.cancel(ride, actor="admin", reason=reason)
        flash("Ride cancelled.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("rides.show", ride_id=ride_id))
