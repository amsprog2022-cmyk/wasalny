"""Ride REST API used by the customer + captain Flutter apps.

Blueprint mounted under /api/v1 alongside app/api/v1.py.

Endpoints:
  POST /api/v1/customer/login           phone-only auth (Decision #6)
  GET  /api/v1/zones                    active zones
  POST /api/v1/rides/quote              price + pending fees preview
  POST /api/v1/rides                    create a booking → matching starts
  GET  /api/v1/rides/<id>               ride state (customer or assigned captain)
  POST /api/v1/rides/<id>/cancel        customer or admin cancel
  POST /api/v1/rides/<id>/accept        captain claims a broadcast (atomic)
  POST /api/v1/rides/<id>/start         captain starts the trip
  POST /api/v1/rides/<id>/complete      captain marks trip done
  POST /api/v1/rides/<id>/no-show       captain reports customer no-show (5 min gate)
"""
from __future__ import annotations

import time

from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import (
    create_access_token,
    jwt_required,
    get_jwt,
    get_jwt_identity,
)

from app import db
from app.extensions import get_redis
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.ride import Ride
from app.models.zone import Zone
from app.services import pricing as pricing_svc
from app.services import ride_lifecycle
from app.services import matching


rides_api_bp = Blueprint("rides_api", __name__, url_prefix="/api/v1")


# ---------- helpers ----------

def _customer_id_from_jwt() -> int | None:
    if get_jwt().get("kind") != "customer":
        return None
    sub = get_jwt_identity() or ""
    if isinstance(sub, str) and sub.startswith("customer:"):
        try:
            return int(sub.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


def _driver_id_from_jwt() -> int | None:
    if get_jwt().get("kind") != "driver":
        return None
    sub = get_jwt_identity() or ""
    if isinstance(sub, str) and sub.startswith("driver:"):
        try:
            return int(sub.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


def _rate_limit_customer(customer_id: int) -> bool:
    """True if the customer is under the per-10-min booking limit."""
    r = get_redis(current_app.config.get("REDIS_URL"))
    key = f"rate_limit:customer:{customer_id}"
    limit = int(current_app.config.get("CUSTOMER_RATE_LIMIT_PER_10MIN", 3))
    n = r.incr(key)
    if n == 1:
        r.expire(key, 600)
    return int(n) <= limit


# ---------- captain profile & discipline ----------

@rides_api_bp.post("/driver/change-password")
@jwt_required()
def driver_change_password():
    """First-login flow: captain must change from the default password."""
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    driver = db.session.get(Driver, did)
    if driver is None:
        return jsonify({"error": "not_found"}), 404
    data = request.json or {}
    current = data.get("current_password") or ""
    new_pw = (data.get("new_password") or "").strip()
    if len(new_pw) < 6:
        return jsonify({"error": "password_too_short", "message_ar": "كلمة السر لازم ٦ حروف على الأقل."}), 400
    if driver.password_hash and not driver.check_password(current):
        return jsonify({"error": "wrong_current_password"}), 401
    driver.set_password(new_pw)
    driver.must_change_password = False
    db.session.commit()
    return jsonify({"changed": True})


@rides_api_bp.get("/driver/earnings")
@jwt_required()
def driver_earnings():
    """Today / this week / this month summary for the captain home dashboard."""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403

    now = datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())  # Monday
    month_start = day_start.replace(day=1)

    def _bucket(start: datetime) -> dict:
        rows = (
            Ride.query.filter(
                Ride.driver_id == did,
                Ride.status == "completed",
                Ride.completed_at >= start,
            )
            .with_entities(
                func.count(Ride.id),
                func.sum(Ride.price_egp),
                func.sum(Ride.commission_egp),
            )
            .first()
        )
        trips = int(rows[0] or 0)
        gross = float(rows[1] or 0)
        commission = float(rows[2] or 0)
        return {
            "trips": trips,
            "gross_egp": gross,
            "commission_egp": commission,
            "net_egp": round(gross - commission, 2),
        }

    return jsonify(
        {
            "today": _bucket(day_start),
            "this_week": _bucket(week_start),
            "this_month": _bucket(month_start),
            "commission_rate": float(current_app.config.get("WASSALNY_COMMISSION_RATE", "0.15")),
        }
    )


@rides_api_bp.get("/driver/discipline")
@jwt_required()
def driver_discipline():
    """Rejection count + warning state — powers the yellow banner in the captain app."""
    from app.services import captain_discipline
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    return jsonify(captain_discipline.get_state(did))


@rides_api_bp.post("/driver/fcm-token")
@jwt_required()
def driver_fcm_token():
    """Store the captain's FCM token for background trip-offer push notifications."""
    from app.services import push_notifications
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    data = request.json or {}
    token = (data.get("token") or "").strip()
    platform = (data.get("platform") or "").strip() or "unknown"
    if not token:
        return jsonify({"error": "token_required"}), 400
    push_notifications.register_driver_token(did, token, platform)
    return jsonify({"stored": True})


# ---------- captain availability (mirror of the /driver socket, over HTTP) ----------
# The Flutter app uses the WebSocket for low latency, but HTTP is available for
# load tests and captains on poor connections.

@rides_api_bp.post("/driver/availability")
@jwt_required()
def driver_availability():
    from app.services import availability as av
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    data = request.json or {}
    action = data.get("action")
    if action == "online":
        zone_id = int(data.get("zone_id") or 0)
        if not zone_id:
            return jsonify({"error": "zone_id required"}), 400
        av.set_online(did, zone_id)
    elif action == "offline":
        av.set_offline(did)
    elif action == "available":
        av.set_available(did, bool(data.get("available", True)))
    elif action == "zone":
        zone_id = int(data.get("zone_id") or 0)
        if not zone_id:
            return jsonify({"error": "zone_id required"}), 400
        av.change_zone(did, zone_id)
    elif action == "heartbeat":
        av.heartbeat(did)
    else:
        return jsonify({"error": "unknown_action"}), 400
    return jsonify(av.get_presence(did).__dict__)


# ---------- customer profile ----------

@rides_api_bp.get("/customer/me")
@jwt_required()
def customer_me():
    from sqlalchemy import func
    from app.models.ride import CustomerPendingFee
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    c = db.session.get(Customer, cid)
    if c is None:
        return jsonify({"error": "not_found"}), 404
    completed = Ride.query.filter_by(customer_id=cid, status="completed")
    stats = completed.with_entities(
        func.count(Ride.id), func.sum(Ride.price_egp)
    ).first()
    pending_egp = (
        CustomerPendingFee.query.filter_by(
            customer_id=cid, applied_to_ride_id=None, waived_at=None
        )
        .with_entities(func.sum(CustomerPendingFee.amount_egp))
        .scalar()
        or 0
    )
    return jsonify(
        {
            "id": c.id,
            "wa_id": c.wa_id,
            "name": c.name,
            "total_trips": int(stats[0] or 0),
            "total_spent_egp": float(stats[1] or 0),
            "pending_fees_egp": float(pending_egp),
            "created_at": c.created_at.isoformat() if getattr(c, "created_at", None) else None,
        }
    )


@rides_api_bp.patch("/customer/me")
@jwt_required()
def customer_me_update():
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    c = db.session.get(Customer, cid)
    if c is None:
        return jsonify({"error": "not_found"}), 404
    data = request.json or {}
    new_name = (data.get("name") or "").strip()
    if new_name:
        c.name = new_name[:120]
        db.session.commit()
    return jsonify({"id": c.id, "wa_id": c.wa_id, "name": c.name})


@rides_api_bp.get("/customer/rides")
@jwt_required()
def customer_rides():
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    limit = min(int(request.args.get("limit", 20)), 100)
    rides = (
        Ride.query.filter_by(customer_id=cid)
        .order_by(Ride.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify([r.to_dict() for r in rides])


@rides_api_bp.post("/rides/<int:ride_id>/rate")
@jwt_required()
def rides_rate(ride_id: int):
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None or ride.customer_id != cid:
        return jsonify({"error": "forbidden"}), 403
    if ride.status != "completed":
        return jsonify({"error": "not_completed"}), 409
    if ride.rating is not None:
        return jsonify({"error": "already_rated"}), 409
    data = request.json or {}
    try:
        stars = int(data.get("stars"))
    except (TypeError, ValueError):
        return jsonify({"error": "stars_required"}), 400
    if not 1 <= stars <= 5:
        return jsonify({"error": "stars_out_of_range"}), 400
    ride.rating = stars
    ride.rating_comment = (data.get("comment") or "").strip()[:500] or None

    # Update driver's rolling rating (simple avg over last 50 completed rides)
    from sqlalchemy import func
    if ride.driver_id:
        rows = (
            Ride.query.filter(
                Ride.driver_id == ride.driver_id,
                Ride.rating.isnot(None),
                Ride.status == "completed",
            )
            .order_by(Ride.completed_at.desc())
            .limit(50)
            .all()
        )
        if rows:
            avg = sum(r.rating for r in rows) / len(rows)
            drv = db.session.get(Driver, ride.driver_id)
            if drv is not None:
                drv.rating = round(avg, 2)
    db.session.commit()
    return jsonify(ride.to_dict())


@rides_api_bp.post("/customer/fcm-token")
@jwt_required()
def customer_fcm_token():
    """Store the customer's FCM token so we can push trip updates when the
    app is in background/killed. Firebase-Admin sends real messages when
    FIREBASE_SERVICE_ACCOUNT_JSON is configured on the backend.
    """
    from app.services import push_notifications
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    data = request.json or {}
    token = (data.get("token") or "").strip()
    platform = (data.get("platform") or "").strip() or "unknown"
    if not token:
        return jsonify({"error": "token_required"}), 400
    push_notifications.register_customer_token(cid, token, platform)
    return jsonify({"stored": True})


# ---------- customer auth (Decision #6: phone-only, no OTP) ----------

def _issue_customer_token(customer):
    return create_access_token(
        identity=f"customer:{customer.id}",
        additional_claims={"kind": "customer"},
    )


def _customer_payload(customer, *, needs_password_setup=False):
    return {
        "id": customer.id,
        "wa_id": customer.wa_id,
        "name": customer.name,
        "needs_password_setup": needs_password_setup,
    }


@rides_api_bp.post("/customer/register")
def customer_register():
    data = request.json or {}
    wa_id = (data.get("wa_id") or "").strip().lstrip("+")
    name = (data.get("name") or "").strip() or None
    password = (data.get("password") or "").strip()

    if not wa_id or not name or not password:
        return jsonify({"error": "wa_id, name, password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    existing = Customer.query.filter_by(wa_id=wa_id).first()
    if existing is not None:
        if existing.deleted_at is not None:
            return jsonify({"error": "account_deleted"}), 403
        return jsonify({"error": "phone_already_registered"}), 409

    customer = Customer(wa_id=wa_id, name=name)
    customer.set_password(password)
    db.session.add(customer)
    db.session.commit()

    return jsonify({
        "access_token": _issue_customer_token(customer),
        "customer": _customer_payload(customer),
    })


@rides_api_bp.post("/customer/login")
def customer_login():
    data = request.json or {}
    wa_id = (data.get("wa_id") or "").strip().lstrip("+")
    password = (data.get("password") or "").strip()

    if not wa_id:
        return jsonify({"error": "wa_id required"}), 400

    customer = Customer.query.filter_by(wa_id=wa_id).first()
    if customer is None:
        return jsonify({"error": "not_registered"}), 404
    if customer.deleted_at is not None:
        return jsonify({"error": "account_deleted"}), 403

    # Legacy account created before we required passwords — issue a token
    # so the app can prompt the user to set one on the next screen.
    if not customer.password_hash:
        return jsonify({
            "access_token": _issue_customer_token(customer),
            "customer": _customer_payload(customer, needs_password_setup=True),
        })

    if not password:
        return jsonify({"error": "password required"}), 400
    if not customer.check_password(password):
        return jsonify({"error": "invalid_credentials"}), 401

    return jsonify({
        "access_token": _issue_customer_token(customer),
        "customer": _customer_payload(customer),
    })


@rides_api_bp.post("/customer/delete-account")
@jwt_required()
def customer_delete_account():
    """In-app account deletion (required by App Store + Play Store policies).

    Soft-deletes: sets deleted_at, clears PII (name → 'محذوف'), removes FCM
    token, but preserves trip history so captain earnings + admin reports
    remain intact. Login is blocked from this point on.
    """
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    customer = db.session.get(Customer, cid)
    if customer is None:
        return jsonify({"error": "not_found"}), 404
    if customer.deleted_at is not None:
        return jsonify({"error": "already_deleted"}), 400

    from datetime import datetime
    customer.deleted_at = datetime.utcnow()
    customer.name = "محذوف"
    customer.fcm_token = None
    customer.opted_in = False
    db.session.commit()
    return jsonify({"deleted": True})


@rides_api_bp.post("/driver/delete-account")
@jwt_required()
def driver_delete_account():
    """In-app account deletion for captains.

    Same soft-delete strategy as customers: clears PII + FCM + name → 'محذوف',
    keeps earnings history for admin reconciliation. Also marks driver
    inactive so they cannot be assigned rides.
    """
    from datetime import datetime
    identity = get_jwt_identity()
    if not identity or not identity.startswith("driver:"):
        return jsonify({"error": "driver_token_required"}), 403
    did = int(identity.split(":", 1)[1])
    driver = db.session.get(Driver, did)
    if driver is None:
        return jsonify({"error": "not_found"}), 404
    if driver.deleted_at is not None:
        return jsonify({"error": "already_deleted"}), 400

    driver.deleted_at = datetime.utcnow()
    driver.name = "محذوف"
    driver.fcm_token = None
    driver.is_active = False
    driver.discipline_status = "banned"
    db.session.commit()
    return jsonify({"deleted": True})


@rides_api_bp.post("/customer/set-password")
@jwt_required()
def customer_set_password():
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    data = request.json or {}
    password = (data.get("password") or "").strip()
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    customer = db.session.get(Customer, cid)
    if customer is None:
        return jsonify({"error": "not_found"}), 404
    customer.set_password(password)
    db.session.commit()
    return jsonify({"customer": _customer_payload(customer)})


# ---------- public zone list ----------

@rides_api_bp.get("/zones")
def list_zones():
    zones = Zone.query.filter_by(is_active=True).order_by(Zone.id.asc()).all()
    return jsonify([z.to_dict() for z in zones])


# ---------- quote ----------

@rides_api_bp.post("/rides/quote")
@jwt_required()
def rides_quote():
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    data = request.json or {}
    try:
        from_zone_id = int(data.get("from_zone_id"))
        to_zone_id = int(data.get("to_zone_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "from_zone_id and to_zone_id required"}), 400
    q = pricing_svc.quote(cid, from_zone_id, to_zone_id)
    if q is None:
        return jsonify({"error": "no_pricing_for_pair"}), 404
    return jsonify(q.to_dict())


# ---------- create ----------

@rides_api_bp.post("/rides")
@jwt_required()
def rides_create():
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    data = request.json or {}
    try:
        from_zone_id = int(data.get("from_zone_id"))
        to_zone_id = int(data.get("to_zone_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "from_zone_id and to_zone_id required"}), 400

    if not _rate_limit_customer(cid):
        return jsonify({"error": "rate_limited", "retry_after_seconds": 600}), 429

    try:
        ride, pending_ids = ride_lifecycle.create_ride(
            customer_id=cid,
            from_zone_id=from_zone_id,
            to_zone_id=to_zone_id,
            source="app",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Kick off matching asynchronously
    matching.start_matching(ride.id, pending_fee_ids=pending_ids)
    return jsonify(ride.to_dict()), 201


# ---------- read ----------

@rides_api_bp.get("/rides/<int:ride_id>")
@jwt_required()
def rides_read(ride_id: int):
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    cid = _customer_id_from_jwt()
    did = _driver_id_from_jwt()
    if cid is not None and ride.customer_id == cid:
        pass
    elif did is not None and ride.driver_id == did:
        pass
    else:
        return jsonify({"error": "forbidden"}), 403
    # When the caller IS the assigned driver, expose the customer's phone
    # so the captain app can render a tap-to-call button.
    payload = ride.to_dict(include_customer_contact=(did is not None and ride.driver_id == did))
    if ride.driver_id:
        d = db.session.get(Driver, ride.driver_id)
        payload["driver"] = d.to_dict() if d else None
    return jsonify(payload)


# ---------- customer cancel ----------

@rides_api_bp.post("/rides/<int:ride_id>/cancel")
@jwt_required()
def rides_cancel(ride_id: int):
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    cid = _customer_id_from_jwt()
    if cid is None or ride.customer_id != cid:
        return jsonify({"error": "forbidden"}), 403
    reason = (request.json or {}).get("reason") or "customer_cancelled"
    try:
        ride_lifecycle.cancel(ride, actor="customer", reason=reason)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(ride.to_dict(include_customer_contact=True))


# ---------- captain: accept / start / complete / no_show ----------

@rides_api_bp.post("/rides/<int:ride_id>/accept")
@jwt_required()
def rides_accept(ride_id: int):
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    if ride.status != "broadcasting":
        return jsonify({"error": "not_broadcasting", "status": ride.status}), 409
    if matching.try_claim(ride_id, did):
        return jsonify({"claimed": True, "ride_id": ride_id})
    return jsonify({"claimed": False, "error": "already_taken"}), 409


@rides_api_bp.post("/rides/<int:ride_id>/arrived")
@jwt_required()
def rides_arrived(ride_id: int):
    """Captain has arrived at the pickup location — notify customer."""
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    try:
        ride_lifecycle.arrived(ride, did)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(ride.to_dict(include_customer_contact=True))


@rides_api_bp.post("/rides/<int:ride_id>/start")
@jwt_required()
def rides_start(ride_id: int):
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    try:
        ride_lifecycle.start(ride, did)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(ride.to_dict(include_customer_contact=True))


@rides_api_bp.post("/rides/<int:ride_id>/complete")
@jwt_required()
def rides_complete(ride_id: int):
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    try:
        ride_lifecycle.complete(ride, did)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(ride.to_dict(include_customer_contact=True))


@rides_api_bp.patch("/rides/<int:ride_id>/price")
@jwt_required()
def rides_update_price(ride_id: int):
    """Captain silently overrides the ride price mid-trip.

    Use case: customer doesn't have exact change / negotiates on-the-spot.
    Only the driver assigned to the ride can call this, and only while the
    trip is assigned or started (not after completion). Broadcasts a
    `ride_price_updated` socket event to the customer so their app shows
    the new price without needing a reload.
    """
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    if ride.driver_id != did:
        return jsonify({"error": "not_your_ride"}), 403
    if ride.status not in ("assigned", "started"):
        return jsonify({"error": "cannot_edit_after_completion"}), 409

    data = request.json or {}
    try:
        new_price = float(data.get("price_egp"))
    except (TypeError, ValueError):
        return jsonify({"error": "price_egp must be a number"}), 400
    if new_price <= 0 or new_price > 10000:
        return jsonify({"error": "price out of range (0, 10000)"}), 400

    from decimal import Decimal
    old_price = float(ride.price_egp)
    ride.price_egp = Decimal(f"{new_price:.2f}")
    db.session.commit()

    # Notify customer in real time
    try:
        from app import socketio
        socketio.emit(
            "ride_price_updated",
            {"ride_id": ride.id, "old_price_egp": old_price, "new_price_egp": new_price},
            namespace="/customer",
            room=f"customer:{ride.customer_id}",
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("price update socket emit failed: %s", e)

    # Also send push so the customer sees it if the app is backgrounded
    try:
        from app.services import push_notifications as push
        push.send_to_customer(
            ride.customer_id,
            title="السعر اتعدل",
            body=f"الكابتن عدّل سعر الرحلة إلى {new_price:.0f} ج.م",
            data={"kind": "price_updated", "ride_id": ride.id, "price_egp": new_price},
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("price update push failed: %s", e)

    return jsonify(ride.to_dict(include_customer_contact=True))


@rides_api_bp.get("/rides/<int:ride_id>/messages")
@jwt_required()
def rides_list_messages(ride_id: int):
    """List trip chat messages. Marks all as read for the caller in one shot."""
    from datetime import datetime as _dt
    from app.models.trip_chat import TripChatMessage
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    cid = _customer_id_from_jwt()
    did = _driver_id_from_jwt()
    is_customer = cid is not None and ride.customer_id == cid
    is_driver = did is not None and ride.driver_id == did
    if not (is_customer or is_driver):
        return jsonify({"error": "forbidden"}), 403

    since_id = request.args.get("since", type=int)
    q = TripChatMessage.query.filter_by(ride_id=ride_id)
    if since_id:
        q = q.filter(TripChatMessage.id > since_id)
    messages = q.order_by(TripChatMessage.id.asc()).limit(200).all()

    # Mark messages from the OTHER side as read (best-effort; not per-message)
    now = _dt.utcnow()
    if is_customer:
        for m in messages:
            if m.sender_kind == "driver" and m.read_by_customer_at is None:
                m.read_by_customer_at = now
    else:
        for m in messages:
            if m.sender_kind == "customer" and m.read_by_driver_at is None:
                m.read_by_driver_at = now
    db.session.commit()

    return jsonify({"messages": [m.to_dict() for m in messages]})


@rides_api_bp.post("/rides/<int:ride_id>/messages")
@jwt_required()
def rides_send_message(ride_id: int):
    """Customer or driver sends a chat message within an active ride."""
    from app.models.trip_chat import TripChatMessage
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    cid = _customer_id_from_jwt()
    did = _driver_id_from_jwt()
    if cid is not None and ride.customer_id == cid:
        sender_kind = "customer"
        sender_id = cid
    elif did is not None and ride.driver_id == did:
        sender_kind = "driver"
        sender_id = did
    else:
        return jsonify({"error": "forbidden"}), 403

    # Only allow chat during active statuses — no cold messages before broadcast
    # or after complete/cancel.
    if ride.status not in ("assigned", "started"):
        return jsonify({"error": "chat_only_during_active_ride"}), 409

    body = ((request.json or {}).get("body") or "").strip()
    if not body:
        return jsonify({"error": "body required"}), 400
    if len(body) > 500:
        return jsonify({"error": "body too long (max 500)"}), 400

    msg = TripChatMessage(
        ride_id=ride_id,
        sender_kind=sender_kind,
        sender_id=sender_id,
        body=body,
    )
    db.session.add(msg)
    db.session.commit()

    payload = {"ride_id": ride_id, "message": msg.to_dict()}

    # Fan out via Socket.IO — recipient gets real-time delivery
    from app import socketio
    try:
        if sender_kind == "customer" and ride.driver_id:
            socketio.emit("chat_message_new", payload,
                          namespace="/driver", room=f"driver:{ride.driver_id}")
        elif sender_kind == "driver":
            socketio.emit("chat_message_new", payload,
                          namespace="/customer", room=f"customer:{ride.customer_id}")
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("chat socket emit failed: %s", e)

    # Push notification for backgrounded apps
    try:
        from app.services import push_notifications as push
        preview = body[:80]
        if sender_kind == "customer" and ride.driver_id:
            customer_name = ride.customer.name if ride.customer else "العميل"
            push.send_to_driver(
                ride.driver_id,
                title=f"💬 {customer_name}",
                body=preview,
                data={"kind": "chat_message_new", "ride_id": ride_id},
                collapse_key=f"chat:{ride_id}",
            )
        elif sender_kind == "driver":
            driver_name = ride.driver.name if ride.driver else "الكابتن"
            push.send_to_customer(
                ride.customer_id,
                title=f"💬 {driver_name}",
                body=preview,
                data={"kind": "chat_message_new", "ride_id": ride_id},
                collapse_key=f"chat:{ride_id}",
            )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("chat push failed: %s", e)

    return jsonify(msg.to_dict()), 201


@rides_api_bp.post("/rides/<int:ride_id>/sos")
@jwt_required()
def rides_sos(ride_id: int):
    """Customer taps SOS during an active trip."""
    cid = _customer_id_from_jwt()
    if cid is None:
        return jsonify({"error": "customer_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None or ride.customer_id != cid:
        return jsonify({"error": "forbidden"}), 403
    from app.models.ops import SosAlert
    from app import socketio
    data = request.json or {}
    alert = SosAlert(
        ride_id=ride.id,
        customer_id=ride.customer_id,
        driver_id=ride.driver_id,
        message=(data.get("message") or "")[:1000] or None,
    )
    db.session.add(alert)
    db.session.commit()
    # Push a live popup to the admin dashboard
    socketio.emit(
        "sos_alert_new",
        {"id": alert.id, "ride_id": ride.id},
        namespace="/inbox",
    )
    return jsonify({"id": alert.id, "status": "open"}), 201


@rides_api_bp.post("/rides/<int:ride_id>/complaint")
@jwt_required()
def rides_complaint_ride(ride_id: int):
    """Customer or captain files a complaint tied to a specific ride."""
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    cid = _customer_id_from_jwt()
    did = _driver_id_from_jwt()
    if cid is not None and ride.customer_id == cid:
        kind, filer_id = "customer", cid
    elif did is not None and ride.driver_id == did:
        kind, filer_id = "driver", did
    else:
        return jsonify({"error": "forbidden"}), 403
    from app.services import complaints as complaints_svc
    data = request.json or {}
    c = complaints_svc.file_complaint(
        filed_by_kind=kind,
        filed_by_id=filer_id,
        subject=(data.get("subject") or "Report from app")[:200],
        description=data.get("description"),
        category=(data.get("category") or "other"),
        ride_id=ride.id,
    )
    return jsonify({"id": c.id, "status": c.status}), 201


@rides_api_bp.post("/rides/<int:ride_id>/reject")
@jwt_required()
def rides_reject(ride_id: int):
    """Captain rejects a trip offer. Increments daily rejection counter and
    applies discipline (warn @ 5, suspend @ 10) per Decision #12.

    Also removes this driver from the current broadcast's `offered_to` set so
    the matching engine can move on.
    """
    from app.services import captain_discipline
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    # Only reject if the offer is still active (broadcasting) and the driver was in the offer set
    r = get_redis(current_app.config.get("REDIS_URL"))
    key = f"broadcast:{ride_id}:offered_to"
    r.srem(key, str(did))

    summary = captain_discipline.register_rejection(did)
    return jsonify({"rejected": True, **summary})


@rides_api_bp.post("/rides/<int:ride_id>/rate-customer")
@jwt_required()
def rides_rate_customer(ride_id: int):
    """Captain rates the customer 1-5 stars after a completed trip."""
    from app.models.captain_rating import CaptainRatingOfCustomer
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None or ride.driver_id != did:
        return jsonify({"error": "forbidden"}), 403
    if ride.status != "completed":
        return jsonify({"error": "not_completed"}), 409

    # Reject duplicates
    existing = CaptainRatingOfCustomer.query.filter_by(ride_id=ride_id, driver_id=did).first()
    if existing:
        return jsonify({"error": "already_rated"}), 409

    data = request.json or {}
    try:
        stars = int(data.get("stars"))
    except (TypeError, ValueError):
        return jsonify({"error": "stars_required"}), 400
    if not 1 <= stars <= 5:
        return jsonify({"error": "stars_out_of_range"}), 400

    row = CaptainRatingOfCustomer(
        ride_id=ride_id,
        driver_id=did,
        customer_id=ride.customer_id,
        stars=stars,
        comment=(data.get("comment") or "").strip()[:500] or None,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({"rated": True, "stars": stars})


@rides_api_bp.post("/rides/<int:ride_id>/no-show")
@jwt_required()
def rides_no_show(ride_id: int):
    did = _driver_id_from_jwt()
    if did is None:
        return jsonify({"error": "driver_token_required"}), 403
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return jsonify({"error": "not_found"}), 404
    try:
        ride_lifecycle.no_show(ride, did)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 403
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(ride.to_dict(include_customer_contact=True))
