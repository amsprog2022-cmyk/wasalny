"""Live map — real-time view of every online captain in Benha.

Renders a MapLibre map that streams captain positions via the existing
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
    """Render the map page. MapTiler key is injected into the template so
    the browser can build the tile-server URL without a second request."""
    return render_template(
        "live_map/index.html",
        maptiler_key=current_app.config.get("MAPTILER_KEY", ""),
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


@live_map_bp.route("/debug", strict_slashes=False)
@live_map_bp.route("/debug/", strict_slashes=False)
@login_required
def debug_state():
    """Per-driver raw state dump so admins can see WHY a captain isn't
    showing on the map. Answers questions like:
      - Did their GPS ping ever reach Redis? (has_position)
      - Did they tap Go Online? (online)
      - When was their last heartbeat? (seconds_since_hb)
    """
    import time as _time
    drivers = (
        Driver.query
        .filter(Driver.is_active.is_(True))
        .filter(Driver.deleted_at.is_(None))
        .all()
    )
    now = _time.time()
    # By default filter to "interesting" drivers only — anyone whose Redis
    # state suggests they might have been online recently. Pass ?all=1 to
    # see the full list (noisy when the fleet is big).
    show_all = request.args.get("all") == "1"
    q_wa = (request.args.get("wa_id") or "").strip()

    out = []
    total_pos = 0
    total_online = 0
    for d in drivers:
        pos = av.get_position(d.id)
        presence = av.get_presence(d.id)
        if pos is not None:
            total_pos += 1
        if presence.online:
            total_online += 1
        interesting = (
            pos is not None
            or presence.online
            or presence.available
            or presence.last_hb is not None
        )
        if not show_all and not interesting and not q_wa:
            continue
        if q_wa and q_wa not in (d.wa_id or ""):
            continue
        out.append({
            "driver_id": d.id,
            "name": d.name,
            "wa_id": d.wa_id,
            "approved": getattr(d, "approval_status", "unknown"),
            "has_position": pos is not None,
            "lat": pos[0] if pos else None,
            "lng": pos[1] if pos else None,
            "presence_online": presence.online,
            "presence_available": presence.available,
            "presence_zone_id": presence.zone_id,
            "last_hb": presence.last_hb,
            "seconds_since_hb":
                (round(now - presence.last_hb, 1) if presence.last_hb else None),
            "is_live": presence.is_live,
        })
    return jsonify({
        "total_active_drivers": len(drivers),
        "drivers_with_position": total_pos,
        "drivers_online": total_online,
        "shown": len(out),
        "hint": "pass ?all=1 to dump every driver, or ?wa_id=201... to filter by phone",
        "drivers": out,
    })
