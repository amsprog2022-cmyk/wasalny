from __future__ import annotations

from flask import Blueprint, render_template, request
from flask_login import login_required

from app.models.ops import AuditLog
from app.models import User


audit_bp = Blueprint("audit", __name__, url_prefix="/audit")


@audit_bp.route("/")
@login_required
def index():
    q = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    actor_ids = {r.actor_user_id for r in q if r.actor_user_id}
    actors = {u.id: u for u in User.query.filter(User.id.in_(actor_ids)).all()} if actor_ids else {}
    return render_template("audit/index.html", rows=q, actors=actors)
