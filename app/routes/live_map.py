"""Live map — real-time view of every online captain in Benha.

Renders a MapLibre map that streams captain positions via the existing
/inbox Socket.IO namespace. The GET /live-map/data endpoint returns the
current snapshot at page-load; sockets take over from there.

Auth: standard Flask-Login session — same as every other admin page.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template
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


@live_map_bp.route("/debug")
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
    out = []
    for d in drivers:
        pos = av.get_position(d.id)
        presence = av.get_presence(d.id)
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
        "drivers": out,
    })
