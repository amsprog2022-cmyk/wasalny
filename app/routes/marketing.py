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
from app.services import localtime
from app.services import push_notifications as push


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

ANNOUNCEMENT_AUDIENCES = ("customer", "driver", "both")
ANNOUNCEMENT_PRIORITIES = ("info", "warning", "critical")


def _parse_local(value: str | None) -> datetime | None:
    """Read a browser `datetime-local` value as Cairo wall-clock and return
    naive UTC. Reading it as UTC put every announcement 2-3 hours in the
    future, so a card scheduled for "now" never went live."""
    if not value:
        return None
    try:
        return localtime.cairo_to_utc(datetime.strptime(value, "%Y-%m-%dT%H:%M"))
    except ValueError:
        return None


@marketing_bp.route("/announcements")
@login_required
def announcements():
    rows = Announcement.query.order_by(Announcement.starts_at.desc()).limit(50).all()
    return render_template(
        "marketing/announcements.html",
        announcements=rows,
        now=datetime.utcnow(),
    )


@marketing_bp.route("/announcements/new", methods=["POST"])
@login_required
def new_announcement():
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("marketing.announcements"))

    title_ar = (request.form.get("title_ar") or "").strip()
    body_ar = (request.form.get("body_ar") or "").strip()
    if not title_ar:
        flash("لازم تكتب عنوان الإعلان.", "error")
        return redirect(url_for("marketing.announcements"))

    audience = request.form.get("audience", "customer")
    priority = request.form.get("priority", "info")
    if audience not in ANNOUNCEMENT_AUDIENCES or priority not in ANNOUNCEMENT_PRIORITIES:
        flash("اختيار غير صحيح.", "error")
        return redirect(url_for("marketing.announcements"))

    # A `datetime-local` the browser rejected arrives empty; a malformed one
    # parses to None, which would mean "never expires" — a permanent card
    # nobody meant to publish. Refuse rather than guess.
    raw_ends = (request.form.get("ends_at") or "").strip()
    ends_at = _parse_local(raw_ends)
    if raw_ends and ends_at is None:
        flash("وقت النهاية مش مفهوم.", "error")
        return redirect(url_for("marketing.announcements"))

    a = Announcement(
        audience=audience,
        title_ar=title_ar,
        body_ar=body_ar or None,
        priority=priority,
        created_by_user_id=current_user.id,
    )
    a.starts_at = _parse_local(request.form.get("starts_at")) or datetime.utcnow()
    a.ends_at = ends_at
    if a.ends_at is not None and a.ends_at <= a.starts_at:
        flash("وقت النهاية لازم يكون بعد وقت البداية.", "error")
        return redirect(url_for("marketing.announcements"))

    db.session.add(a)
    db.session.flush()
    audit.record(
        "announcement.create", target_kind="announcement", target_id=a.id,
        after={"audience": a.audience, "title": a.title_ar},
    )
    db.session.commit()

    msg = "الإعلان اتنشر."
    if request.form.get("also_push"):
        counts = push.broadcast_push(
            a.audience, title=title_ar, body=body_ar or title_ar,
            data={"kind": "announcement", "announcement_id": a.id},
        )
        audit.record(
            "announcement.push", target_kind="announcement", target_id=a.id,
            after=counts,
        )
        db.session.commit()
        msg += f" واتبعت اشعار لـ {counts['customers']} عميل و {counts['drivers']} كابتن."
    flash(msg, "success")
    return redirect(url_for("marketing.announcements"))


@marketing_bp.route("/announcements/<int:ann_id>/end", methods=["POST"])
@login_required
def end_announcement(ann_id: int):
    """Pull a live announcement down now. Kept rather than deleted so the
    audit trail still shows what customers were shown and when."""
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("marketing.announcements"))
    a = Announcement.query.get_or_404(ann_id)
    now = datetime.utcnow()
    if a.ends_at is not None and a.ends_at <= now:
        flash("الإعلان ده خلص خلاص.", "error")
        return redirect(url_for("marketing.announcements"))
    # Cancelling one that has not started yet must not leave ends_at before
    # starts_at — an impossible window the liveness check reads as dead but
    # every report would have to special-case.
    a.ends_at = max(now, a.starts_at)
    audit.record("announcement.end", target_kind="announcement", target_id=a.id)
    db.session.commit()
    flash("الإعلان اتوقف.", "success")
    return redirect(url_for("marketing.announcements"))


# ---------- Direct push to the apps ----------

@marketing_bp.route("/push", methods=["POST"])
@login_required
def send_push():
    """One-off notification with no announcement card behind it."""
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("marketing.announcements"))

    audience = request.form.get("audience", "customer")
    title = (request.form.get("title") or "").strip()
    body = (request.form.get("body") or "").strip()
    if not title or not body:
        flash("لازم تكتب عنوان ونص الاشعار.", "error")
        return redirect(url_for("marketing.announcements"))

    counts = push.broadcast_push(
        audience, title=title, body=body, data={"kind": "admin_message"},
    )
    audit.record(
        "push.broadcast", target_kind="push", target_id=None,
        after={"audience": audience, "title": title, **counts},
    )
    db.session.commit()
    flash(
        f"الاشعار اتبعت لـ {counts['customers']} عميل و {counts['drivers']} كابتن.",
        "success",
    )
    return redirect(url_for("marketing.announcements"))
