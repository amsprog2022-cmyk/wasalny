"""Admin board for "سفر خارج بنها" enquiries.

These never become rides — an admin phones the customer back and closes
the row. Requests arrive from two places: the WhatsApp menu (press 2)
and the customer app's intercity card.
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.intercity_request import IntercityRequest
from app.services import audit


intercity_bp = Blueprint("intercity", __name__, url_prefix="/intercity")


@intercity_bp.route("/")
@login_required
def index():
    open_requests = (
        IntercityRequest.query.filter_by(status="open")
        .order_by(IntercityRequest.created_at.desc())
        .all()
    )
    past = (
        IntercityRequest.query.filter(IntercityRequest.status != "open")
        .order_by(IntercityRequest.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template("intercity/index.html", open_requests=open_requests, past=past)


@intercity_bp.route("/<int:req_id>/contact", methods=["POST"])
@login_required
def contact(req_id: int):
    req = IntercityRequest.query.get_or_404(req_id)
    req.status = "contacted"
    req.handled_by_user_id = current_user.id
    req.handled_at = datetime.utcnow()
    note = (request.form.get("note") or "").strip()
    if note:
        req.note = note
    audit.record("intercity.contact", target_kind="intercity_request", target_id=req_id)
    db.session.commit()
    flash("اتسجل إنك اتواصلت مع العميل.", "success")
    return redirect(url_for("intercity.index"))


@intercity_bp.route("/<int:req_id>/close", methods=["POST"])
@login_required
def close(req_id: int):
    req = IntercityRequest.query.get_or_404(req_id)
    req.status = "closed"
    req.handled_at = datetime.utcnow()
    if req.handled_by_user_id is None:
        req.handled_by_user_id = current_user.id
    note = (request.form.get("note") or "").strip()
    if note:
        req.note = note
    audit.record("intercity.close", target_kind="intercity_request", target_id=req_id)
    db.session.commit()
    flash("اتقفل الطلب.", "success")
    return redirect(url_for("intercity.index"))
