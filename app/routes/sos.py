"""Admin SOS alerts page + acknowledge/resolve actions."""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.ops import SosAlert
from app.services import audit


sos_bp = Blueprint("sos", __name__, url_prefix="/sos")


@sos_bp.route("/")
@login_required
def index():
    open_alerts = SosAlert.query.filter_by(status="open").order_by(SosAlert.created_at.desc()).all()
    ack = (
        SosAlert.query.filter(SosAlert.status != "open")
        .order_by(SosAlert.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template("sos/index.html", open_alerts=open_alerts, past=ack)


@sos_bp.route("/<int:sos_id>/ack", methods=["POST"])
@login_required
def acknowledge(sos_id: int):
    alert = SosAlert.query.get_or_404(sos_id)
    alert.status = "acknowledged"
    alert.acknowledged_by_user_id = current_user.id
    alert.acknowledged_at = datetime.utcnow()
    audit.record("sos.acknowledge", target_kind="sos_alert", target_id=sos_id)
    db.session.commit()
    flash("SOS acknowledged.", "success")
    return redirect(url_for("sos.index"))


@sos_bp.route("/<int:sos_id>/resolve", methods=["POST"])
@login_required
def resolve(sos_id: int):
    alert = SosAlert.query.get_or_404(sos_id)
    alert.status = "resolved"
    alert.resolved_at = datetime.utcnow()
    alert.notes = (request.form.get("notes") or "").strip() or alert.notes
    if alert.acknowledged_by_user_id is None:
        alert.acknowledged_by_user_id = current_user.id
        alert.acknowledged_at = alert.resolved_at
    audit.record("sos.resolve", target_kind="sos_alert", target_id=sos_id)
    db.session.commit()
    flash("SOS resolved.", "success")
    return redirect(url_for("sos.index"))
