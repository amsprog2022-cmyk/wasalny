"""Admin AI-handoff queue.

Shows every AdminAlert with kind=ai_handoff waiting for a human. Agents click
"take over" → alert marked handled → they jump into the customer's WhatsApp
conversation and complete the booking manually. They can also open the
"Assign to captain" modal to book a ride and hand it directly to an online
captain without waiting for the broadcast auction.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from flask import Blueprint, redirect, render_template, url_for, flash, jsonify, request, current_app
from flask_login import login_required, current_user

from app import db
from app.models.ai_session import AdminAlert
from app.models.customer import Customer
from app.models.conversation import Conversation
from app.models.driver import Driver
from app.models.zone import Zone
from app.services import availability as av
from app.services import ride_lifecycle
from app.services import whatsapp
from app.services.whatsapp import WhatsAppError


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


@alerts_bp.route("/api/zones", methods=["GET"])
@login_required
def api_zones():
    """Public zone list for the assign modal — no JWT needed since the user
    is logged into the admin dashboard session."""
    zones = Zone.query.filter_by(is_active=True).order_by(Zone.id.asc()).all()
    return jsonify([{"id": z.id, "name_ar": z.name_ar, "slug": z.slug} for z in zones])


@alerts_bp.route("/api/available-captains", methods=["GET"])
@login_required
def api_available_captains():
    """List captains available to be manually assigned to a ride.

    Filters: is_active, approved, not soft-deleted, presence.online, no
    active ride in Postgres. Returns captains in the pickup zone first,
    then everyone else so the admin can still assign cross-zone if needed.
    """
    from app.models.ride import Ride
    from app.extensions import get_redis

    pickup_zone_id = request.args.get("from_zone_id", type=int)

    q = (
        Driver.query
        .filter(Driver.is_active == True)  # noqa: E712
        .filter(Driver.deleted_at.is_(None))
    )
    # Approved status column exists on Driver in this repo (approval_status)
    if hasattr(Driver, "approval_status"):
        q = q.filter(Driver.approval_status == "approved")

    drivers = q.all()

    # Filter out drivers with an active ride (they're already busy).
    busy_ids = {
        r.driver_id for r in Ride.query
        .filter(Ride.status.in_(("assigned", "started")))
        .with_entities(Ride.driver_id).all()
        if r.driver_id is not None
    }

    r = get_redis(current_app.config.get("REDIS_URL"))
    out = []
    for d in drivers:
        if d.id in busy_ids:
            continue
        presence = av.get_presence(d.id)
        if not presence.online:
            continue
        zone = Zone.query.get(presence.zone_id) if presence.zone_id else None
        out.append({
            "id": d.id,
            "name": d.name,
            "wa_id": d.wa_id,
            "current_zone_id": presence.zone_id,
            "current_zone_ar": zone.name_ar if zone else None,
            "available": presence.available,
            "car_plate": getattr(d, "car_plate", None),
        })

    # Same-zone-as-pickup first so admins tap the obvious choice.
    if pickup_zone_id:
        out.sort(key=lambda x: (0 if x["current_zone_id"] == pickup_zone_id else 1, x["name"] or ""))
    else:
        out.sort(key=lambda x: (x["name"] or ""))
    return jsonify(out)


@alerts_bp.route("/<int:alert_id>/assign", methods=["POST"])
@login_required
def assign(alert_id: int):
    """Create a ride and hand it directly to the selected captain.

    Body JSON:
      { "from_zone_id": int,
        "to_zone_id":   int | null,  # null → WhatsApp-style (captain sets on arrival)
        "price_egp":    float | null,  # required only when to_zone_id is null
        "driver_id":    int }

    Skips the broadcast auction — we go straight to ride_lifecycle.assign()
    since a human just picked the captain. Customer gets a WhatsApp
    confirmation with the captain's name + phone so they know who's coming.
    """
    alert = AdminAlert.query.get_or_404(alert_id)
    if alert.customer_id is None:
        return jsonify({"error": "alert_has_no_customer"}), 400
    if alert.status != "open":
        return jsonify({"error": "alert_already_handled"}), 409

    data = request.get_json(silent=True) or {}
    try:
        from_zone_id = int(data.get("from_zone_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "from_zone_id_required"}), 400
    to_zone_id_raw = data.get("to_zone_id")
    to_zone_id = int(to_zone_id_raw) if to_zone_id_raw else None
    price_raw = data.get("price_egp")
    try:
        driver_id = int(data.get("driver_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "driver_id_required"}), 400

    from_zone = Zone.query.get(from_zone_id)
    if from_zone is None or not from_zone.is_active:
        return jsonify({"error": "unknown_from_zone"}), 400
    if to_zone_id is not None:
        to_zone = Zone.query.get(to_zone_id)
        if to_zone is None or not to_zone.is_active:
            return jsonify({"error": "unknown_to_zone"}), 400

    driver = Driver.query.get(driver_id)
    if driver is None or not driver.is_active:
        return jsonify({"error": "unknown_driver"}), 400

    # Create the ride. Uses source="admin" so it doesn't count against the
    # customer's rate limit and is easy to distinguish in reports.
    try:
        ride, pending_ids = ride_lifecycle.create_ride(
            customer_id=alert.customer_id,
            from_zone_id=from_zone_id,
            to_zone_id=to_zone_id,
            source="admin",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # If the admin also entered a manual price (WhatsApp-style ride), apply it.
    if to_zone_id is None and price_raw not in (None, ""):
        try:
            new_price = float(price_raw)
            ride.price_egp = Decimal(f"{new_price:.2f}")
            rate = Decimal(str(current_app.config.get("WASSALNY_COMMISSION_RATE", "0.15")))
            ride.commission_egp = (Decimal(f"{new_price:.2f}") * rate).quantize(Decimal("0.01"))
            db.session.commit()
        except (TypeError, ValueError):
            pass  # keep the deferred 0 price if input was bad

    # Hand it straight to the picked captain (skip broadcast).
    try:
        ride_lifecycle.assign(ride, driver_id=driver.id, pending_fee_ids=pending_ids)
    except ValueError as e:
        return jsonify({"error": f"assign_failed: {e}"}), 409

    # Mark the alert handled by this admin.
    alert.status = "handled"
    alert.handled_by_user_id = current_user.id
    alert.resolved_at = datetime.utcnow()
    db.session.commit()

    # Confirm to the customer on WhatsApp with captain info.
    customer = Customer.query.get(alert.customer_id)
    if customer is not None:
        plate = f" · {driver.car_plate}" if getattr(driver, "car_plate", None) else ""
        body = (
            f"🚗 كابتن جاي: {driver.name}{plate}\n"
            f"رقم الكابتن: {driver.wa_id}\n"
            f"لو محتاج تكلمه اضغط على الرقم."
        )
        try:
            whatsapp.send_text(customer.wa_id, body)
        except WhatsAppError as e:
            current_app.logger.warning("assign confirm to customer failed: %s", e)

    return jsonify({"ride_id": ride.id, "driver_id": driver.id, "status": ride.status})
