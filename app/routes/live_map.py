"""Live map — real-time view of every online captain in Benha.

Renders a Google Maps view that streams captain positions via the existing
/inbox Socket.IO namespace. The GET /live-map/data endpoint returns the
current snapshot at page-load; sockets take over from there.

Auth: standard Flask-Login session — same as every other admin page.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import login_required

from app.models.driver import Driver
from app.models.ride import Ride
from app.services import availability as av


live_map_bp = Blueprint("live_map", __name__, url_prefix="/live-map")


@live_map_bp.route("/")
@login_required
def index():
    """Render the map page. The browser key and Map ID are injected into the
    template so the Google Maps loader can boot without a second request."""
    return render_template(
        "live_map/index.html",
        google_maps_key=current_app.config.get("GOOGLE_MAPS_BROWSER_KEY", ""),
        google_maps_map_id=current_app.config.get("GOOGLE_MAPS_MAP_ID", ""),
    )


@live_map_bp.route("/data")
@login_required
def data():
    """Initial snapshot for the map + sidebar.

    Returns every captain currently in the Redis GEO index (skipping those
    with no live position) + every in-flight ride (broadcasting / assigned
    / started), capped at 50 rides so the payload stays small.
    """
    drivers = (
        Driver.query
        .filter(Driver.is_active.is_(True))
        .filter(Driver.deleted_at.is_(None))
        .all()
    )

    # Look up active rides once so we can annotate each captain with their
    # current ride id (used to colour the marker green vs orange).
    active_rides_all = (
        Ride.query
        .filter(Ride.status.in_(("assigned", "started")))
        .all()
    )
    on_trip_by_driver = {r.driver_id: r for r in active_rides_all if r.driver_id is not None}

    captains_out = []
    for d in drivers:
        pos = av.get_position(d.id)
        if pos is None:
            continue    # no known location — nothing to draw
        # Show everyone with a position. The frontend colours them by
        # state (green / orange / grey) so we can visually diagnose when
        # a captain thinks they're online but the server disagrees.
        # Matching engine has its own is_live filter — nothing here can
        # accidentally route a ride to a ghost.
        presence = av.get_presence(d.id)
        lat, lng = pos
        ride = on_trip_by_driver.get(d.id)
        captains_out.append({
            # Emitted as `driver_id` so the frontend can key markers with
            # the same field the socket's driver_position_update sends —
            # otherwise the initial snapshot markers can't be updated in
            # place and duplicate every ping. `id` kept as an alias.
            "id": d.id,
            "driver_id": d.id,
            "name": d.name,
            "wa_id": d.wa_id,
            "lat": lat,
            "lng": lng,
            "online": presence.online,
            "available": presence.available,
            # is_live == online AND heartbeat within 60s. False here means
            # the captain's Redis row is stale (app force-quit, phone died
            # etc). Frontend colours these as "ghost" so the admin doesn't
            # try to assign a ride to a dead session.
            "is_live": presence.is_live,
            "on_trip_ride_id": (ride.id if ride else None),
        })

    rides_out = []
    rides_query = (
        Ride.query
        .filter(Ride.status.in_(("broadcasting", "assigned", "started")))
        .order_by(Ride.id.desc())
        .limit(50)
        .all()
    )
    for r in rides_query:
        rides_out.append({
            "id": r.id,
            "status": r.status,
            "source": r.source,
            "from_zone_ar": r.from_zone.name_ar if r.from_zone else None,
            "to_zone_ar":   r.to_zone.name_ar if r.to_zone else None,
            "driver_id": r.driver_id,
            "driver_name": (r.driver.name if r.driver else None),
            "customer_wa_id": (r.customer.wa_id if r.customer else None),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    return jsonify({"captains": captains_out, "rides": rides_out})


@live_map_bp.route("/search-places")
@login_required
def search_places():
    """Admin-session proxy over the existing Nominatim forward geocoder
    (services/reverse_geocode.search_places). Powers the place-search box
    on the live map — same as customer app's destination search but
    without a customer JWT."""
    from app.services import reverse_geocode as rg
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify([])
    return jsonify(rg.search_places(q, limit=6))


@live_map_bp.route("/place-name")
@login_required
def place_name():
    """Admin-session reverse geocoder for map-click pins. The customer app's
    /api/v1/rides/place-name is @jwt_required, so an admin session cookie gets
    a 401 there and every pin silently degraded to raw coordinates."""
    from app.services import reverse_geocode as rg
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat/lng required"}), 400
    return jsonify({"label": rg.resolve_place_name(lat, lng)})


@live_map_bp.route("/nearest-captains")
@login_required
def nearest_captains():
    """Rank live, available captains by distance to a pickup point.

    Query: ?lat=&lng=&limit=5&radius_km=10
    Filters:
      - presence.is_live (online + heartbeat within 60s)
      - not currently on a trip
      - approved (if the column exists)
    Response: [{driver_id, name, wa_id, car_model, car_plate, car_color,
                distance_km, lat, lng}]
    """
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat_lng_required"}), 400
    limit = int(request.args.get("limit") or 5)
    radius_km = float(request.args.get("radius_km") or 10)
    # Optional service_kind filter — the admin service_request assign
    # modal passes this so a سوزوكي request only sees suzuki drivers.
    service_kind = (request.args.get("service_kind") or "").strip().lower() or None

    rows = av.nearest_drivers(lat, lng, radius_km=radius_km, limit=max(limit * 3, 15))
    if not rows:
        return jsonify([])

    driver_ids = [did for did, _dist, _pt in rows]
    drivers_by_id = {
        d.id: d for d in Driver.query.filter(Driver.id.in_(driver_ids)).all()
    }
    # Filter out captains that are on a live trip so we don't offer a busy one.
    busy_ids = {
        r.driver_id for r in Ride.query
        .filter(Ride.driver_id.in_(driver_ids))
        .filter(Ride.status.in_(("assigned", "started")))
        .with_entities(Ride.driver_id).all()
    }

    out = []
    for did, dist, (dlat, dlng) in rows:
        d = drivers_by_id.get(did)
        if d is None or not d.is_active or getattr(d, "deleted_at", None):
            continue
        if hasattr(d, "approval_status") and d.approval_status != "approved":
            continue
        if service_kind and getattr(d, "service_kind", "private") != service_kind:
            continue
        if did in busy_ids:
            continue
        if not av.get_presence(did).is_live:
            continue
        out.append({
            "driver_id": did,
            "name": d.name,
            "wa_id": d.wa_id,
            "car_model": getattr(d, "car_model", None),
            "car_color": getattr(d, "car_color", None),
            "car_plate": getattr(d, "car_plate", None),
            "distance_km": round(dist, 2),
            "lat": dlat,
            "lng": dlng,
        })
        if len(out) >= limit:
            break
    return jsonify(out)


@live_map_bp.route("/quote")
@login_required
def quote():
    """Wrap pricing.quote_by_coords so the admin create-ride modal can
    show a suggested fare. Returns {price_egp, distance_km}. Admin-
    editable pricing knobs (Setting table) are already read by pricing."""
    try:
        pl = float(request.args.get("pickup_lat"))
        plng = float(request.args.get("pickup_lng"))
        dl = float(request.args.get("dropoff_lat"))
        dlng = float(request.args.get("dropoff_lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "coords_required"}), 400
    # Reuse quote_by_coords — customer_id=0 is fine, only used for pending
    # fees (walk-ins have none).
    from app.services import pricing as pricing_svc
    q = pricing_svc.quote_by_coords(
        customer_id=0,
        pickup_lat=pl, pickup_lng=plng,
        dropoff_lat=dl, dropoff_lng=dlng,
    )
    return jsonify({
        "price_egp": float(q.ride_price_egp),
        "distance_km": q.distance_km,
    })


@live_map_bp.route("/create-ride", methods=["POST"])
@login_required
def create_ride():
    """Admin-created walk-in ride: pick customer by wa_id, pin pickup +
    dropoff on the map, pick a live captain, dispatch directly. Skips
    the broadcast auction — captain gets a direct assignment push.

    Body JSON:
      customer_wa_id : str  required
      customer_name  : str  optional (only used when creating the row)
      pickup_lat/lng : float required
      dropoff_lat/lng: float required
      price_egp      : float optional (overrides the coord-based quote)
      driver_id      : int  required (must be live + not busy)
    """
    from app import db
    from app.models.customer import Customer
    from app.services import ride_lifecycle
    from app.services import dispatch_notifications
    from decimal import Decimal

    data = request.get_json(silent=True) or {}
    wa_id = (data.get("customer_wa_id") or "").strip()
    if not wa_id:
        return jsonify({"error": "customer_wa_id_required"}), 400
    try:
        pl  = float(data["pickup_lat"])
        plng = float(data["pickup_lng"])
        dl  = float(data["dropoff_lat"])
        dlng = float(data["dropoff_lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "coords_required"}), 400
    try:
        driver_id = int(data.get("driver_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "driver_id_required"}), 400

    driver = Driver.query.get(driver_id)
    if driver is None or not driver.is_active:
        return jsonify({"error": "unknown_driver"}), 400
    if not av.get_presence(driver.id).is_live:
        return jsonify({
            "error": "captain_not_reachable",
            "message": "الكابتن ده مش متصل بالسيرفر دلوقتي، اختار كابتن تاني.",
        }), 409
    # Reject if the captain already has a live trip in Postgres.
    busy = Ride.query.filter(
        Ride.driver_id == driver.id,
        Ride.status.in_(("assigned", "started")),
    ).first()
    if busy is not None:
        return jsonify({
            "error": "captain_busy",
            "message": "الكابتن ده على رحلة تانية دلوقتي، اختار كابتن تاني.",
        }), 409

    # Find or create the customer. Walk-ins have no password — they can
    # still be looked up by phone if they ever install the app later.
    customer = Customer.query.filter_by(wa_id=wa_id).first()
    if customer is None:
        customer = Customer(
            wa_id=wa_id,
            name=(data.get("customer_name") or "").strip() or None,
        )
        db.session.add(customer)
        db.session.commit()

    try:
        ride, pending_ids = ride_lifecycle.create_ride(
            customer_id=customer.id,
            pickup_lat=pl, pickup_lng=plng,
            dropoff_lat=dl, dropoff_lng=dlng,
            source="admin",
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Override the auto-quote if admin typed a specific price.
    price_raw = data.get("price_egp")
    if price_raw not in (None, ""):
        try:
            new_price = float(price_raw)
            ride.price_egp = Decimal(f"{new_price:.2f}")
            from app.services import settings as _settings_svc
            rate = _settings_svc.get_pricing()["commission_rate"]
            ride.commission_egp = (Decimal(f"{new_price:.2f}") * rate).quantize(Decimal("0.01"))
            db.session.commit()
        except (TypeError, ValueError):
            pass  # keep the auto-quote if input was bad

    try:
        ride_lifecycle.assign(ride, driver_id=driver.id, pending_fee_ids=pending_ids)
    except ValueError as e:
        return jsonify({"error": f"assign_failed: {e}"}), 409

    customer_notified = dispatch_notifications.notify_customer_of_assignment(
        customer, driver
    )
    return jsonify({
        "ride_id": ride.id,
        "driver_id": driver.id,
        "customer_id": customer.id,
        "customer_notified": customer_notified,
    })


@live_map_bp.route("/pending-alerts")
@login_required
def pending_alerts():
    """Return every open `no_driver` AdminAlert with its ride details, so
    the live map can offer a "pick which pending ride to assign" modal
    when the admin clicks a captain marker."""
    from app.models.ai_session import AdminAlert
    from app.models.customer import Customer
    alerts = (
        AdminAlert.query
        .filter_by(status="open", kind="no_driver")
        .order_by(AdminAlert.created_at.desc())
        .limit(50)
        .all()
    )
    ride_ids = {a.ride_id for a in alerts if a.ride_id}
    rides = {r.id: r for r in Ride.query.filter(Ride.id.in_(ride_ids)).all()} if ride_ids else {}
    cust_ids = {a.customer_id for a in alerts if a.customer_id}
    customers = {c.id: c for c in Customer.query.filter(Customer.id.in_(cust_ids)).all()} if cust_ids else {}
    out = []
    for a in alerts:
        r = rides.get(a.ride_id)
        c = customers.get(a.customer_id)
        out.append({
            "alert_id": a.id,
            "ride_id": a.ride_id,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "customer_name": (c.name or c.wa_id) if c else None,
            "customer_wa_id": c.wa_id if c else None,
            "from_zone_id": r.from_zone_id if r else None,
            "from_zone_ar": (r.from_zone.name_ar if r and r.from_zone else None),
            "to_zone_id": r.to_zone_id if r else None,
            "to_zone_ar": (r.to_zone.name_ar if r and r.to_zone else None),
            "pickup_address": r.pickup_address if r else None,
            "dropoff_address": r.dropoff_address if r else None,
            "price_egp": float(r.price_egp) if r and r.price_egp else 0.0,
        })
    return jsonify(out)


