"""Admin AI-handoff queue.

Shows every AdminAlert with kind=ai_handoff waiting for a human. Agents click
"take over" → alert marked handled → they jump into the customer's WhatsApp
conversation and complete the booking manually.
"""
from __future__ import annotations

import json
from datetime import datetime

from flask import Blueprint, redirect, render_template, url_for, flash
from flask_login import login_required, current_user

from app import db
from app.models.ai_session import AdminAlert
from app.models.customer import Customer
from app.models.conversation import Conversation


alerts_bp = Blueprint("alerts", __name__, url_prefix="/alerts")


@alerts_bp.route("/")
@login_required
def index():
    open_alerts = (
        AdminAlert.query.filter_by(status="open")
        .order_by(AdminAlert.created_at.desc())
        .limit(200)
        .all()
    )
    handoffs = [a for a in open_alerts if a.kind == "ai_handoff"]
    no_driver = [a for a in open_alerts if a.kind == "no_driver"]
    other = [a for a in open_alerts if a.kind not in ("ai_handoff", "no_driver")]

    # Preload customer info for handoff rows
    cust_ids = {a.customer_id for a in open_alerts if a.customer_id}
    customers = {c.id: c for c in Customer.query.filter(Customer.id.in_(cust_ids)).all()} if cust_ids else {}

    return render_template(
        "alerts/index.html",
        handoffs=handoffs,
        no_driver=no_driver,
        other=other,
        customers=customers,
        parse_json=json.loads,
    )


@alerts_bp.route("/<int:alert_id>/take-over", methods=["POST"])
@login_required
def take_over(alert_id: int):
    alert = AdminAlert.query.get_or_404(alert_id)
    alert.status = "handled"
    alert.handled_by_user_id = current_user.id
    alert.resolved_at = datetime.utcnow()
    db.session.commit()
    # Jump into the customer's WhatsApp conversation. The inbox is a SPA
    # rendered from /inbox — anchor to the conversation id and let the JS
    # focus it on load.
    if alert.customer_id:
        conv = Conversation.query.filter_by(customer_id=alert.customer_id, kind="customer").first()
        if conv:
            return redirect(url_for("inbox.index") + f"#conv-{conv.id}")
    flash("Alert marked handled.", "success")
    return redirect(url_for("alerts.index"))


@alerts_bp.route("/<int:alert_id>/resolve", methods=["POST"])
@login_required
def resolve(alert_id: int):
    alert = AdminAlert.query.get_or_404(alert_id)
    alert.status = "handled"
    alert.handled_by_user_id = current_user.id
    alert.resolved_at = datetime.utcnow()
    db.session.commit()
    flash("Alert resolved.", "success")
    return redirect(url_for("alerts.index"))
