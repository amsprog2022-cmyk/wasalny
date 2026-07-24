"""Real-time Socket.IO namespace for the captain Flutter app.

Auth: the client passes a JWT in the query string (?token=…) or the
Socket.IO `auth` payload. We validate it with flask-jwt-extended.

Events (client → server):
  driver:online       {zone_id}          → mark online in zone
  driver:offline      {}                 → mark offline
  driver:heartbeat    {}                 → keep-alive (every 15s)
  driver:available    {available: bool}  → manual busy/available toggle
  driver:zone         {zone_id}          → captain updated their current zone

Events (server → client):
  driver:presence     {online, available, zone_id, last_hb}
  driver:error        {message}
"""
from __future__ import annotations

from flask import current_app, request
from flask_socketio import Namespace, emit, disconnect, join_room
from flask_jwt_extended import decode_token

from app import socketio, db
from app.models.driver import Driver
from app.services import availability as av


NAMESPACE = "/driver"


def _driver_id_from_token() -> int | None:
    token = None
    if request.args.get("token"):
        token = request.args.get("token")
    else:
        auth = getattr(request, "auth", None) or {}
        if isinstance(auth, dict):
            token = auth.get("token")
    if not token:
        return None
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if payload.get("kind") != "driver":
        return None
    sub = payload.get("sub") or ""
    # Identity is "driver:{id}" per api/v1.py convention
    if isinstance(sub, str) and sub.startswith("driver:"):
        try:
            return int(sub.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


class DriverNamespace(Namespace):
    def on_connect(self):
        driver_id = _driver_id_from_token()
        if not driver_id:
            emit("driver:error", {"message": "unauthenticated"})
            disconnect()
            return
        driver = db.session.get(Driver, driver_id)
        if driver is None or not driver.is_active:
            emit("driver:error", {"message": "driver not found or inactive"})
            disconnect()
            return
        join_room(f"driver:{driver_id}")
        emit("driver:presence", av.get_presence(driver_id).__dict__)

    def on_disconnect(self, reason=None):
        # We don't force-offline here — the heartbeat timeout does it.
        # Prevents flapping when the captain briefly loses signal.
        # reason arg added in python-socketio 5.12+.
        pass

    def on_driver_online(self, data):
        driver_id = _driver_id_from_token()
        if not driver_id:
            return
        zone_id = int((data or {}).get("zone_id") or 0)
        if not zone_id:
            emit("driver:error", {"message": "zone_id required"})
            return
        av.set_online(driver_id, zone_id)
        emit("driver:presence", av.get_presence(driver_id).__dict__)

    def on_driver_offline(self, data):
        driver_id = _driver_id_from_token()
        if not driver_id:
            return
        av.set_offline(driver_id)
        # set_offline already zrems the GEO entry, but do it explicitly here
        # too so the intent is obvious to whoever reads this handler next.
        av.clear_position(driver_id)
        emit("driver:presence", av.get_presence(driver_id).__dict__)
        # Tell the admin live map to drop this captain's dot instantly.
        try:
            socketio.emit(
                "driver_position_removed",
                {"driver_id": driver_id},
                namespace="/inbox",
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("driver_position_removed emit failed: %s", e)

    def on_driver_heartbeat(self, data):
        driver_id = _driver_id_from_token()
        if not driver_id:
            return
        av.heartbeat(driver_id)

    def on_driver_position(self, data):
        """Live GPS from the captain app (phase 1).

        Captain-driven events use underscores — Flask-SocketIO's
        `on_driver_position(self)` handler matches an emitted event named
        `driver_position`. Payload: {lat: float, lng: float}. Silently
        drops malformed / out-of-range values so a bad phone can't crash
        the pipeline.
        """
        driver_id = _driver_id_from_token()
        if not driver_id:
            return
        payload = data or {}
        try:
            lat = float(payload.get("lat"))
            lng = float(payload.get("lng"))
        except (TypeError, ValueError):
            return
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            return
        av.set_position(driver_id, lat, lng)

        # Fan out to any admin dashboards watching the live map so the dot
        # moves in real time. Best-effort: swallow any db lookup issue so a
        # broken emit never affects the tracking pipeline.
        try:
            from app.models.ride import Ride
            driver = db.session.get(Driver, driver_id)
            active_ride = Ride.query.filter(
                Ride.driver_id == driver_id,
                Ride.status.in_(("assigned", "started")),
            ).first()
            socketio.emit(
                "driver_position_update",
                {
                    "driver_id": driver_id,
                    "name": (driver.name if driver else None),
                    "wa_id": (driver.wa_id if driver else None),
                    "lat": lat,
                    "lng": lng,
                    "on_trip_ride_id": (active_ride.id if active_ride else None),
                },
                namespace="/inbox",
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("driver_position broadcast failed: %s", e)

    def on_driver_available(self, data):
        driver_id = _driver_id_from_token()
        if not driver_id:
            return
        available = bool((data or {}).get("available"))
        av.set_available(driver_id, available)
        emit("driver:presence", av.get_presence(driver_id).__dict__)

    def on_driver_zone(self, data):
        driver_id = _driver_id_from_token()
        if not driver_id:
            return
        zone_id = int((data or {}).get("zone_id") or 0)
        if not zone_id:
            return
        av.change_zone(driver_id, zone_id)
        emit("driver:presence", av.get_presence(driver_id).__dict__)


socketio.on_namespace(DriverNamespace(NAMESPACE))
