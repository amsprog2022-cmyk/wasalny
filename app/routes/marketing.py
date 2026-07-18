"""Marketing broadcasts + in-app announcements admin pages."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.customer import Customer
from app.models.ride import Ride
from app.models.ops import AdminBroadcast, Announcement
from app.services import audit


marketing_bp = Blueprint("marketing", __name__, url_prefix="/marketing")


# ---------- Marketing broadcasts ----------

def _audience_count(kind: str) -> int:
    if kind == "all_customers":
        return Customer.query.count()
    if kind == "vip":
        # VIP = at least 20 completed trips
        from sqlalchemy import func
        rows = (
            db.session.query(Ride.customer_id, func.count(Ride.id))
            .filter(Ride.status == "completed")
            .group_by(Ride.customer_id)
            .having(func.count(Ride.id) >= 20)
            .all()
        )
        return len(rows)
    if kind == "dormant":
        thirty = datetime.utcnow() - timedelta(days=30)
        active = {
            r.customer_id
            for r in db.session.query(Ride.customer_id)
            .filter(Ride.created_at >= thirty).distinct().all()
        }
        return max(Customer.query.count() - len(active), 0)
    return 0


@marketing_bp.route("/")
@login_required
def index():
    broadcasts = AdminBroadcast.query.order_by(AdminBroadcast.created_at.desc()).limit(50).all()
    audience_previews = {
        k: _audience_count(k) for k in ("all_customers", "vip", "dormant")
    }
    return render_template(
        "marketing/index.html",
        broadcasts=broadcasts,
        audience_previews=audience_previews,
    )


@marketing_bp.route("/new", methods=["POST"])
@login_required
def new_broadcast():
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("marketing.index"))

    audience = request.form.get("audience", "all_customers")
    message_ar = (request.form.get("message_ar") or "").strip()
    if not message_ar:
        flash("Message can't be empty.", "error")
        return redirect(url_for("marketing.index"))
    template_name = (request.form.get("template_name") or "").strip() or None

    recipient_count = _audience_count(audience)
    b = AdminBroadcast(
        kind="whatsapp_marketing",
        template_name=template_name,
        message_ar=message_ar,
        audience_filter_json=json.dumps({"kind": audience}),
        recipient_count=recipient_count,
        created_by_user_id=current_user.id,
    )
    db.session.add(b)
    db.session.flush()
    audit.record(
        "marketing.compose",
        target_kind="broadcast",
        target_id=b.id,
        after={"audience": audience, "recipient_count": recipient_count},
    )
    db.session.commit()
    flash(f"Broadcast drafted. Will reach ~{recipient_count} customers when sent.", "success")
    return redirect(url_for("marketing.index"))


@marketing_bp.route("/<int:broadcast_id>/send", methods=["POST"])
@login_required
def send(broadcast_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("marketing.index"))
    b = AdminBroadcast.query.get_or_404(broadcast_id)
    if b.sent_at:
        flash("Already sent.", "error")
        return redirect(url_for("marketing.index"))
    # In real usage: enqueue RQ jobs to send via WhatsApp Cloud API.
    # For now we mark it as sent and stub the delivery count.
    b.sent_at = datetime.utcnow()
    b.delivered_count = b.recipient_count
    audit.record(
        "marketing.send",
        target_kind="broadcast",
        target_id=b.id,
        after={"delivered": b.delivered_count},
    )
    db.session.commit()
    flash(f"Broadcast #{b.id} sent to {b.delivered_count} recipients (mock).", "success")
    return redirect(url_for("marketing.index"))


# ---------- Announcements ----------

@marketing_bp.route("/announcements")
@login_required
def announcements():
    rows = Announcement.query.order_by(Announcement.starts_at.desc()).limit(50).all()
    return render_template("marketing/announcements.html", announcements=rows)


@marketing_bp.route("/announcements/new", methods=["POST"])
@login_required
def new_announcement():
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("marketing.announcements"))
    a = Announcement(
        audience=request.form.get("audience", "both"),
        title_ar=request.form.get("title_ar"),
        body_ar=request.form.get("body_ar"),
        priority=request.form.get("priority", "info"),
        created_by_user_id=current_user.id,
    )
    ends_in_hours = request.form.get("ends_in_hours")
    if ends_in_hours:
        try:
            a.ends_at = datetime.utcnow() + timedelta(hours=int(ends_in_hours))
        except ValueError:
            pass
    db.session.add(a)
    db.session.flush()
    audit.record("announcement.create", target_kind="announcement", target_id=a.id)
    db.session.commit()
    flash("Announcement live.", "success")
    return redirect(url_for("marketing.announcements"))
