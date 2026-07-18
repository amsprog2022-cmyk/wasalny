"""Ride lifecycle — every state transition goes through this module.

The state machine (PLAN §10):

    new ─► broadcasting ─► assigned ─► started ─► completed
                │             │           │
                │             │           └─► cancelled (any)
                │             └─► cancelled_no_show (after 5 min)
                └─► cancelled (no driver found in 3 rounds)

Every transition:
  1. Validates the current status is allowed to move to the new one.
  2. Updates the Ride row inside the current session.
  3. Records a RideStatusEvent for audit / dispute resolution.
  4. Emits a Socket.IO event to any interested client (customer/driver/admin).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from flask import current_app

from app import db, socketio
from app.models.driver import Driver
from app.models.ride import Ride, RideStatusEvent, CustomerPendingFee
from app.models.zone import Zone
from app.services import availability as av
from app.services import pricing as pricing_svc


# ---------- helpers ----------

def _record(ride_id: int, event: str, actor: str, payload: dict | None = None) -> None:
    db.session.add(
        RideStatusEvent(
            ride_id=ride_id,
            event=event,
            actor=actor,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
        )
    )


def _emit_customer(ride: Ride, event: str, data: dict | None = None) -> None:
    socketio.emit(
        event,
        {"ride": ride.to_dict(), **(data or {})},
        namespace="/customer",
        room=f"customer:{ride.customer_id}",
    )


def _emit_driver(driver_id: int, event: str, data: dict) -> None:
    socketio.emit(event, data, namespace="/driver", room=f"driver:{driver_id}")


def _no_show_fee() -> Decimal:
    return Decimal(str(current_app.config.get("NO_SHOW_FEE_EGP", "10")))


def _no_show_enable_after_minutes() -> int:
    return int(current_app.config.get("NO_SHOW_ENABLE_AFTER_MINUTES", 5))


# ---------- public API ----------

def create_ride(
    *,
    customer_id: int,
    from_zone_id: int,
    to_zone_id: int,
    source: str = "app",
) -> tuple[Ride, list[int]]:
    """Create a `new` ride at the current quoted price.

    Returns (ride, pending_fee_ids_that_were_attached).
    """
    q = pricing_svc.quote(customer_id, from_zone_id, to_zone_id)
    if q is None:
        raise ValueError("No pricing for that zone pair.")

    ride = Ride(
        customer_id=customer_id,
        from_zone_id=from_zone_id,
        to_zone_id=to_zone_id,
        price_egp=q.ride_price_egp,
        commission_egp=q.commission_egp,
        no_show_fee_egp=q.pending_fees_egp,
        status="new",
        source=source,
    )
    db.session.add(ride)
    db.session.flush()   # need ride.id for the audit row + fee application
    _record(ride.id, "created", "customer", {"quote": q.to_dict(), "source": source})
    # Attach the pending fees to THIS ride immediately. Semantics:
    # the fee is now carried by this ride's record and won't reappear on the
    # customer's next quote — even if this ride is cancelled or never assigned.
    pricing_svc.apply_pending_fees(ride.id, q.pending_fee_ids)
    db.session.commit()
    return ride, q.pending_fee_ids


def mark_broadcasting(ride: Ride) -> None:
    if ride.status != "new" and ride.status != "broadcasting":
        raise ValueError(f"Cannot broadcast a ride in status '{ride.status}'.")
    if ride.status == "new":
        ride.status = "broadcasting"
        _record(ride.id, "broadcast_started", "system")
        db.session.commit()
        _emit_customer(ride, "trip_status_changed")


def assign(ride: Ride, driver_id: int, pending_fee_ids: list[int] | None = None) -> None:
    """Broadcast winner: driver_id has already reserved the ride lock in Redis."""
    if ride.status not in ("broadcasting", "new"):
        raise ValueError(f"Cannot assign a ride in status '{ride.status}'.")
    ride.driver_id = driver_id
    ride.status = "assigned"
    ride.assigned_at = datetime.utcnow()
    _record(ride.id, "assigned", "system", {"driver_id": driver_id})
    # Note: pending fees are already applied at create_ride time. This param is
    # kept for backward compat but is now a no-op — safe to remove after Phase 3.

    # Mark driver busy in Redis — they can't take more offers.
    av.set_available(driver_id, False)

    db.session.commit()

    driver = db.session.get(Driver, driver_id)
    driver_payload = driver.to_dict() if driver else None

    _emit_customer(ride, "trip_assigned", {"driver": driver_payload})
    _emit_driver(driver_id, "trip_confirmed", {"ride": ride.to_dict()})


def start(ride: Ride, actor_driver_id: int) -> None:
    if ride.status != "assigned":
        raise ValueError(f"Cannot start a ride in status '{ride.status}'.")
    if ride.driver_id != actor_driver_id:
        raise PermissionError("Only the assigned captain can start the ride.")
    ride.status = "started"
    ride.started_at = datetime.utcnow()
    _record(ride.id, "started", "driver")
    db.session.commit()
    _emit_customer(ride, "trip_status_changed")


def complete(ride: Ride, actor_driver_id: int) -> None:
    if ride.status != "started":
        raise ValueError(f"Cannot complete a ride in status '{ride.status}'.")
    if ride.driver_id != actor_driver_id:
        raise PermissionError("Only the assigned captain can complete the ride.")
    ride.status = "completed"
    ride.completed_at = datetime.utcnow()
    _record(ride.id, "completed", "driver")

    # Release the driver's ride lock in Redis so they can take new offers.
    from app.extensions import get_redis
    r = get_redis(current_app.config.get("REDIS_URL"))
    r.delete(f"driver:{actor_driver_id}:current_ride")

    # Captain's new current zone = trip destination; make them available again.
    av.change_zone(actor_driver_id, ride.to_zone_id)
    av.set_available(actor_driver_id, True)

    # Update driver stats
    driver = db.session.get(Driver, actor_driver_id)
    if driver is not None:
        driver.total_trips = (driver.total_trips or 0) + 1

    db.session.commit()
    _emit_customer(ride, "trip_status_changed")
    _emit_driver(actor_driver_id, "trip_completed_ack", {"ride": ride.to_dict()})


def cancel(ride: Ride, *, actor: str, reason: str) -> None:
    if ride.status in ("completed", "cancelled", "cancelled_no_show"):
        raise ValueError(f"Ride already terminal: '{ride.status}'.")
    ride.status = "cancelled"
    ride.cancelled_at = datetime.utcnow()
    ride.cancel_reason = reason
    _record(ride.id, "cancelled", actor, {"reason": reason})

    if ride.driver_id:
        from app.extensions import get_redis
        r = get_redis(current_app.config.get("REDIS_URL"))
        r.delete(f"driver:{ride.driver_id}:current_ride")
        av.set_available(ride.driver_id, True)

    db.session.commit()
    _emit_customer(ride, "trip_cancelled", {"reason": reason})
    if ride.driver_id:
        _emit_driver(ride.driver_id, "trip_cancelled", {"ride": ride.to_dict(), "reason": reason})


def no_show(ride: Ride, actor_driver_id: int) -> None:
    """Captain reports customer no-show. Enabled 5 min after assignment (Decision #14)."""
    if ride.status != "assigned":
        raise ValueError(f"Cannot mark no-show in status '{ride.status}'.")
    if ride.driver_id != actor_driver_id:
        raise PermissionError("Only the assigned captain can mark no-show.")
    if not ride.assigned_at or datetime.utcnow() - ride.assigned_at < timedelta(
        minutes=_no_show_enable_after_minutes()
    ):
        raise ValueError("No-show button unlocks 5 minutes after assignment.")

    ride.status = "cancelled_no_show"
    ride.cancelled_at = datetime.utcnow()
    ride.cancel_reason = "customer_no_show"
    _record(ride.id, "no_show", "driver")

    # Attach a pending fee to the customer's next trip.
    db.session.add(
        CustomerPendingFee(
            customer_id=ride.customer_id,
            reason="no_show",
            amount_egp=_no_show_fee(),
            from_ride_id=ride.id,
        )
    )

    from app.extensions import get_redis
    r = get_redis(current_app.config.get("REDIS_URL"))
    r.delete(f"driver:{actor_driver_id}:current_ride")
    av.set_available(actor_driver_id, True)
    db.session.commit()

    _emit_customer(ride, "trip_cancelled", {"reason": "customer_no_show"})
    _emit_driver(actor_driver_id, "trip_no_show_ack", {"ride": ride.to_dict()})
