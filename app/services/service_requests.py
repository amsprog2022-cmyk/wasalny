"""Admin dispatch queue for non-private rides.

سوزوكي / دليفري / VIP skip the captain auction entirely — an admin picks
a driver of the matching kind off the /alerts board. Both entry points
(the customer app's POST /rides and the WhatsApp pipeline) funnel through
here so the alert payload stays identical.
"""
from __future__ import annotations

import json

from flask import current_app

from app import db, socketio
from app.models.ai_session import AdminAlert
from app.models.driver import SERVICE_KIND_LABELS_AR
from app.models.ride import Ride
from app.services import ride_lifecycle


def queue_service_request(ride: Ride) -> AdminAlert | None:
    """Flip the ride to `broadcasting` so the customer's waiting screen
    fires, then raise a `service_request` alert for the admin board.

    Best-effort on the alert half — a failed emit must not lose the ride.
    """
    try:
        ride_lifecycle.mark_broadcasting(ride)
    except ValueError:
        pass  # already broadcasting, fine

    try:
        alert = AdminAlert(
            kind="service_request",
            payload_json=json.dumps({
                "service_kind": ride.service_kind,
                "service_kind_ar": SERVICE_KIND_LABELS_AR.get(
                    ride.service_kind, ride.service_kind
                ),
                "pickup_address": ride.pickup_address,
                "dropoff_address": ride.dropoff_address,
                "pickup_lat": ride.pickup_lat,
                "pickup_lng": ride.pickup_lng,
                "customer_wa_id": (ride.customer.wa_id if ride.customer else None),
                "source": ride.source,
            }, ensure_ascii=False),
            customer_id=ride.customer_id,
            ride_id=ride.id,
        )
        db.session.add(alert)
        db.session.commit()
        socketio.emit(
            "service_request_new",
            {
                "id": alert.id,
                "ride_id": ride.id,
                "service_kind": ride.service_kind,
                "customer_id": ride.customer_id,
            },
            namespace="/inbox",
        )
        return alert
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("service_request queue failed: %s", e)
        return None
