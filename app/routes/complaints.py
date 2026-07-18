"""Admin complaints ticketing page + resolution actions.

The filing endpoint (for customer/captain apps) lives in app/api/rides_api.py
so mobile clients don't hit this dashboard blueprint.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.ops import Complaint, ComplaintComment
from app.models.customer import Customer
from app.models.driver import Driver
from app.services import complaints as complaints_svc


complaints_bp = Blueprint("complaints", __name__, url_prefix="/complaints")


TABS = [
    ("open", "Open", ["open", "in_progress", "waiting_user"]),
    ("resolved", "Resolved", ["resolved", "closed"]),
    ("all", "All", None),
]


def _apply_sla(complaints: list[Complaint]) -> None:
    """Flag any open complaint older than 4h/24h — used for color-coding."""
    now = datetime.utcnow()
    for c in complaints:
        if c.status in ("resolved", "closed"):
            continue
        age = now - c.created_at
        c.sla_breach = age > timedelta(hours=4)


@complaints_bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "open")
    match = next((t for t in TABS if t[0] == tab), TABS[0])
    q = Complaint.query
    if match[2] is not None:
        q = q.filter(Complaint.status.in_(match[2]))
    complaints = q.order_by(Complaint.created_at.desc()).limit(200).all()
    _apply_sla(complaints)

    # Look up filer names for display
    cust_ids = {c.filed_by_id for c in complaints if c.filed_by_kind == "customer"}
    drv_ids = {c.filed_by_id for c in complaints if c.filed_by_kind == "driver"}
    customers = {x.id: x for x in Customer.query.filter(Customer.id.in_(cust_ids)).all()} if cust_ids else {}
    drivers = {x.id: x for x in Driver.query.filter(Driver.id.in_(drv_ids)).all()} if drv_ids else {}
    counts = {
        t[0]: (Complaint.query.filter(Complaint.status.in_(t[2])).count() if t[2] else Complaint.query.count())
        for t in TABS
    }
    return render_template(
        "complaints/index.html",
        complaints=complaints,
        counts=counts,
        tabs=TABS,
        active_tab=tab,
        customers=customers,
        drivers=drivers,
        now=datetime.utcnow(),
    )


@complaints_bp.route("/<int:complaint_id>")
@login_required
def show(complaint_id: int):
    c = Complaint.query.get_or_404(complaint_id)
    comments = (
        ComplaintComment.query.filter_by(complaint_id=complaint_id)
        .order_by(ComplaintComment.created_at.asc())
        .all()
    )
    filer_name = "—"
    if c.filed_by_kind == "customer":
        cust = db.session.get(Customer, c.filed_by_id)
        filer_name = (cust.name or cust.wa_id) if cust else "—"
    elif c.filed_by_kind == "driver":
        drv = db.session.get(Driver, c.filed_by_id)
        filer_name = drv.name if drv else "—"

    return render_template(
        "complaints/show.html",
        complaint=c,
        comments=comments,
        filer_name=filer_name,
    )


@complaints_bp.route("/<int:complaint_id>/comment", methods=["POST"])
@login_required
def comment(complaint_id: int):
    body = (request.form.get("body") or "").strip()
    if not body:
        flash("Comment can't be empty.", "error")
        return redirect(url_for("complaints.show", complaint_id=complaint_id))
    complaints_svc.add_comment(complaint_id, current_user.id, body, is_internal=True)
    return redirect(url_for("complaints.show", complaint_id=complaint_id))


@complaints_bp.route("/<int:complaint_id>/resolve", methods=["POST"])
@login_required
def resolve(complaint_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("complaints.show", complaint_id=complaint_id))

    action = request.form.get("action", "none")
    resolution_text = (request.form.get("resolution") or "").strip() or "resolved"
    refund_amount = None
    raw_amt = request.form.get("amount")
    if raw_amt:
        try:
            refund_amount = Decimal(raw_amt)
        except InvalidOperation:
            flash("Bad refund amount.", "error")
            return redirect(url_for("complaints.show", complaint_id=complaint_id))
    hours = None
    if request.form.get("hours"):
        try:
            hours = int(request.form["hours"])
        except ValueError:
            hours = None

    complaints_svc.resolve(
        complaint_id,
        action=action,
        resolution_text=resolution_text,
        refund_amount_egp=refund_amount,
        suspend_captain_hours=hours,
        ban_reason=request.form.get("ban_reason"),
    )
    flash("Complaint resolved.", "success")
    return redirect(url_for("complaints.show", complaint_id=complaint_id))
