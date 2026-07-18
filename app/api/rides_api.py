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


# ---------- customer auth (Decision #6: phone-only, no OTP) ----------

@rides_api_bp.post("/customer/login")
def customer_login():
    data = request.json or {}
    wa_id = (data.get("wa_id") or "").strip().lstrip("+")
    if not wa_id:
        return jsonify({"error": "wa_id required"}), 400
    name = (data.get("name") or "").strip() or None

    customer = Customer.query.filter_by(wa_id=wa_id).first()
    if customer is None:
        customer = Customer(wa_id=wa_id, name=name)
        db.session.add(customer)
        db.session.commit()
    elif name and customer.name != name:
        customer.name = name
        db.session.commit()

    return jsonify(
        {
            "access_token": create_access_token(
                identity=f"customer:{customer.id}",
                additional_claims={"kind": "customer"},
            ),
            "customer": {"id": customer.id, "wa_id": customer.wa_id, "name": customer.name},
        }
    )


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
    payload = ride.to_dict()
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
    return jsonify(ride.to_dict())


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
    return jsonify(ride.to_dict())


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
    return jsonify(ride.to_dict())


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
    return jsonify(ride.to_dict())
