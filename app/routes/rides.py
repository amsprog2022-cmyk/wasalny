"""Admin dashboard rides view — using the real Ride model (Phase 2)."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy import or_

from app import db, socketio
from app.models.driver import Driver
from app.models.ride import Ride, RideStatusEvent, Broadcast
from app.services import ride_lifecycle
from app.services import audit
from app.services import settings as settings_svc


rides_bp = Blueprint("rides", __name__, url_prefix="/rides")


STATUS_TABS = [
    ("active", "Active", ["broadcasting", "queued", "assigned", "started"]),
    ("new", "Waiting", ["new"]),
    ("completed", "Completed", ["completed"]),
    ("cancelled", "Cancelled", ["cancelled", "cancelled_no_show"]),
    ("all", "All", None),
]

# Statuses the admin can still edit — anything terminal is off-limits so we
# don't rewrite completed cash reconciliations or resurrect cancelled trips.
EDITABLE_STATUSES = {"broadcasting", "queued", "assigned", "started"}


@rides_bp.route("/")
@login_required
def index():
    tab = request.args.get("tab", "active")
    q_text = (request.args.get("q") or "").strip()
    filter_ = next((t for t in STATUS_TABS if t[0] == tab), STATUS_TABS[0])
    q = Ride.query
    if filter_[2] is not None:
        q = q.filter(Ride.status.in_(filter_[2]))
    if q_text:
        # Search by captain name or captain phone — the two things the
        # office/admin has in front of them when a customer calls back.
        digits = "".join(ch for ch in q_text if ch.isdigit())
        conds = [Driver.name.ilike(f"%{q_text}%")]
        if digits:
            conds.append(Driver.wa_id.ilike(f"%{digits}%"))
        q = q.join(Driver, Ride.driver_id == Driver.id).filter(or_(*conds))
    rides = q.order_by(Ride.created_at.desc()).limit(200).all()
    counts = {t[0]: Ride.query.filter(Ride.status.in_(t[2])).count() if t[2] else Ride.query.count() for t in STATUS_TABS}
    return render_template(
        "rides/index.html",
        rides=rides,
        counts=counts,
        tabs=STATUS_TABS,
        active_tab=tab,
        q_text=q_text,
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
        editable=ride.status in EDITABLE_STATUSES,
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


@rides_bp.route("/<int:ride_id>/admin-edit", methods=["POST"])
@login_required
def admin_edit(ride_id: int):
    """Change price + dropoff text on an active trip.

    Reaches all four active statuses (broadcasting, queued, assigned, started)
    so the office can correct a paste even after a captain has already been
    matched. Terminal rides stay frozen.
    """
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("rides.show", ride_id=ride_id))

    ride = Ride.query.get_or_404(ride_id)
    if ride.status not in EDITABLE_STATUSES:
        flash("مش هينفع تعدل رحلة خلصت أو اتلغت.", "error")
        return redirect(url_for("rides.show", ride_id=ride_id))

    new_price_raw = (request.form.get("price_egp") or "").strip()
    new_dropoff = (request.form.get("dropoff_address") or "").strip()

    before = {
        "price_egp": str(ride.price_egp) if ride.price_egp is not None else None,
        "commission_egp": str(ride.commission_egp) if ride.commission_egp is not None else None,
        "dropoff_address": ride.dropoff_address,
    }

    changed_fields: list[str] = []

    if new_price_raw:
        try:
            new_price = Decimal(new_price_raw).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            flash("سعر مش صحيح.", "error")
            return redirect(url_for("rides.show", ride_id=ride_id))
        if new_price <= 0:
            flash("السعر لازم يكون أكبر من صفر.", "error")
            return redirect(url_for("rides.show", ride_id=ride_id))
        if ride.price_egp != new_price:
            rate = settings_svc.get_pricing()["commission_rate"]
            ride.price_egp = new_price
            ride.commission_egp = (new_price * rate).quantize(Decimal("0.01"))
            changed_fields.append("price_egp")

    if new_dropoff and new_dropoff != (ride.dropoff_address or ""):
        ride.dropoff_address = new_dropoff
        changed_fields.append("dropoff_address")

    if not changed_fields:
        flash("مفيش تغييرات.", "info")
        return redirect(url_for("rides.show", ride_id=ride_id))

    db.session.add(
        RideStatusEvent(
            ride_id=ride.id,
            event="admin_edited",
            actor="admin",
            payload_json=json.dumps(
                {"fields": changed_fields,
                 "price_egp": str(ride.price_egp),
                 "dropoff_address": ride.dropoff_address},
                ensure_ascii=False,
            ),
        )
    )
    audit.record(
        "ride.admin_edit",
        target_kind="ride",
        target_id=ride.id,
        before=before,
        after={
            "price_egp": str(ride.price_egp),
            "commission_egp": str(ride.commission_egp),
            "dropoff_address": ride.dropoff_address,
        },
    )
    db.session.commit()

    _notify_edit(ride, changed_fields)
    flash("تمّ التعديل وابتعت للكابتن والعميل.", "success")
    return redirect(url_for("rides.show", ride_id=ride_id))


def _notify_edit(ride: Ride, changed_fields: list[str]) -> None:
    """Push the new price/destination to everyone who cares.

    Captain app: `ride_updated` socket event + a data-only FCM (in case
    he's backgrounded).
    Customer app: `trip_status_changed` — the existing handler already
    merges the ride, so price + address refresh without a new listener.
    WhatsApp/office customer: text summary of what changed.
    """
    from flask import current_app

    ride_payload = ride.to_dict()
    price_txt = f"{float(ride.price_egp or 0):.0f}"
    dropoff_txt = ride.dropoff_address or "—"

    # Captain socket + data-push
    if ride.driver_id:
        try:
            socketio.emit(
                "ride_updated",
                {"ride": ride_payload, "fields": changed_fields},
                namespace="/driver",
                room=f"driver:{ride.driver_id}",
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("ride_updated emit failed: %s", e)
        try:
            from app.services import push_notifications as push
            bits = []
            if "price_egp" in changed_fields:
                bits.append(f"السعر: {price_txt} ج.م")
            if "dropoff_address" in changed_fields:
                bits.append(f"الوجهة: {dropoff_txt}")
            push.send_to_driver(
                ride.driver_id,
                title="تعديل على الرحلة",
                body=" · ".join(bits) or "تم تعديل الرحلة",
                data={"kind": "ride_updated", "ride_id": ride.id},
                collapse_key=f"ride_updated:{ride.id}",
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("ride_updated push failed: %s", e)

    # Customer socket — piggy-back on the event the app already merges.
    if ride.customer_id:
        try:
            socketio.emit(
                "trip_status_changed",
                {"ride": ride_payload},
                namespace="/customer",
                room=f"customer:{ride.customer_id}",
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("customer edit emit failed: %s", e)

    # Office / WhatsApp customer: send a text update.
    if ride.source == "office" and ride.office_wa_id:
        _wa_edit_notice(ride.office_wa_id, ride, changed_fields, price_txt, dropoff_txt)
    elif ride.source == "whatsapp" and ride.customer and ride.customer.wa_id:
        _wa_edit_notice(ride.customer.wa_id, ride, changed_fields, price_txt, dropoff_txt)


def _wa_edit_notice(wa_id: str, ride: Ride, changed_fields: list[str],
                    price_txt: str, dropoff_txt: str) -> None:
    from flask import current_app
    from app.services import whatsapp
    lines = [f"تعديل على رحلة #{ride.id}"]
    if "price_egp" in changed_fields:
        lines.append(f"السعر الجديد: {price_txt} ج.م")
    if "dropoff_address" in changed_fields:
        lines.append(f"الوجهة الجديدة: {dropoff_txt}")
    try:
        whatsapp.send_text(wa_id, "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("edit WhatsApp notice failed: %s", e)
