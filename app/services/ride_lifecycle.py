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
from app.services import push_notifications as push


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


def _emit_inbox(ride: Ride, event: str) -> None:
    """Broadcast a ride lifecycle transition to the admin dashboard's
    /inbox socket. Best-effort — a failed emit never affects the trip.

    Used by the live map sidebar to insert/update/remove ride rows without
    a page reload. Includes driver name so the map can render it inline.
    """
    try:
        payload = {
            "ride": ride.to_dict(include_customer_contact=True),
            "event": event,
            "driver_name": (ride.driver.name if ride.driver else None),
        }
        socketio.emit("ride_lifecycle_update", payload, namespace="/inbox")
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("ride_lifecycle_update emit failed: %s", e)


def _no_show_fee() -> Decimal:
    return Decimal(str(current_app.config.get("NO_SHOW_FEE_EGP", "10")))


def _no_show_enable_after_minutes() -> int:
    return int(current_app.config.get("NO_SHOW_ENABLE_AFTER_MINUTES", 5))


# ---------- public API ----------

def create_ride(
    *,
    customer_id: int,
    from_zone_id: int,
    to_zone_id: int | None = None,
    source: str = "app",
) -> tuple[Ride, list[int]]:
    """Create a `new` ride.

    App bookings must specify to_zone_id and get a pre-computed price.
    WhatsApp bookings can omit to_zone_id — captain sets destination + price
    on arrival via PATCH /rides/<id>/price with a new to_zone_id.

    Returns (ride, pending_fee_ids_that_were_attached).
    """
    if to_zone_id is not None:
        q = pricing_svc.quote(customer_id, from_zone_id, to_zone_id)
        if q is None:
            raise ValueError("No pricing for that zone pair.")
        price = q.ride_price_egp
        commission = q.commission_egp
        pending_fees = q.pending_fees_egp
        pending_fee_ids = q.pending_fee_ids
        quote_dict = q.to_dict()
    else:
        # WhatsApp / captain-priced flow — price gets set on arrival.
        from decimal import Decimal as _D
        price = _D("0")
        commission = _D("0")
        pending_fees = _D("0")
        pending_fee_ids = []
        quote_dict = {"deferred": True}

    ride = Ride(
        customer_id=customer_id,
        from_zone_id=from_zone_id,
        to_zone_id=to_zone_id,
        price_egp=price,
        commission_egp=commission,
        no_show_fee_egp=pending_fees,
        status="new",
        source=source,
    )
    db.session.add(ride)
    db.session.flush()
    _record(ride.id, "created", "customer", {"quote": quote_dict, "source": source})
    if pending_fee_ids:
        pricing_svc.apply_pending_fees(ride.id, pending_fee_ids)
    db.session.commit()
    return ride, pending_fee_ids


def mark_broadcasting(ride: Ride) -> None:
    if ride.status != "new" and ride.status != "broadcasting":
        raise ValueError(f"Cannot broadcast a ride in status '{ride.status}'.")
    if ride.status == "new":
        ride.status = "broadcasting"
        _record(ride.id, "broadcast_started", "system")
        db.session.commit()
        # Two events: generic status change AND the "searching driver" signal
        # that the customer app uses to trigger its radar animation.
        _emit_customer(ride, "trip_status_changed")
        _emit_customer(ride, "broadcast_started")
        _emit_inbox(ride, "broadcasting")


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
    _emit_inbox(ride, "assigned")

    # Push notifications — arrive even when apps are backgrounded or killed.
    zone_from = ride.from_zone.name_ar if ride.from_zone else ""
    zone_to = ride.to_zone.name_ar if ride.to_zone else ""
    driver_name = driver.name if driver else "الكابتن"
    car_plate = (driver.car_plate if driver else "") or ""
    push.send_to_customer(
        ride.customer_id,
        title="🚗 كابتن جاي!",
        body=f"{driver_name} · {car_plate}" if car_plate else driver_name,
        data={"kind": "trip_assigned", "ride_id": ride.id},
        collapse_key=f"ride:{ride.id}",
    )
    push.send_to_driver(
        driver_id,
        title="✅ رحلة اتوزعت عليك",
        body=f"{zone_from} ← {zone_to}",
        data={"kind": "trip_confirmed", "ride_id": ride.id},
        collapse_key=f"ride:{ride.id}",
    )

    # WhatsApp customers don't have our app — send them a real WhatsApp text
    # so they see the captain's name and phone as well.
    if ride.source == "whatsapp" and ride.customer is not None:
        try:
            from app.services import whatsapp as _wa
            from app.services.whatsapp import WhatsAppError
            captain_wa = (driver.wa_id if driver else "") or ""
            plate_part = f" · {car_plate}" if car_plate else ""
            body = (
                f"🚗 كابتن جاي: {driver_name}{plate_part}\n"
                f"رقمه: +{captain_wa}\n"
                f"لو محتاج تكلمه اضغط على الرقم."
            )
            _wa.send_text(ride.customer.wa_id, body)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("WhatsApp captain-assigned notify failed: %s", e)


def arrived(ride: Ride, actor_driver_id: int) -> None:
    """Captain has reached the pickup location — notify customer.

    Doesn't change ride status (stays 'assigned') because the trip hasn't
    actually started yet — customer still needs to get in the car. This just
    fires a socket + push notification so the customer knows to come out.
    """
    if ride.status != "assigned":
        raise ValueError(f"Cannot mark arrived on ride in status '{ride.status}'.")
    if ride.driver_id != actor_driver_id:
        raise PermissionError("Only the assigned captain can mark arrived.")

    _record(ride.id, "arrived", "driver")
    db.session.commit()

    _emit_customer(ride, "captain_arrived", {"ride": ride.to_dict()})

    driver = db.session.get(Driver, actor_driver_id)
    driver_name = driver.name if driver else "الكابتن"
    car_plate = (driver.car_plate if driver else "") or ""
    push.send_to_customer(
        ride.customer_id,
        title="🚗 الكابتن وصل!",
        body=f"{driver_name} · {car_plate} — انزل ياكابتن" if car_plate else f"{driver_name} — الكابتن مستنيك",
        data={"kind": "captain_arrived", "ride_id": ride.id},
        collapse_key=f"ride:{ride.id}",
    )

    # WhatsApp customer arrival ping.
    if ride.source == "whatsapp" and ride.customer is not None:
        try:
            from app.services import whatsapp as _wa
            plate_part = f" · {car_plate}" if car_plate else ""
            body = f"🚗 الكابتن وصل! {driver_name}{plate_part} — انزل ياكابتن."
            _wa.send_text(ride.customer.wa_id, body)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("WhatsApp captain-arrived notify failed: %s", e)


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
    _emit_inbox(ride, "started")
    push.send_to_customer(
        ride.customer_id,
        title="🚦 الرحلة ابتدت",
        body="رحلة سعيدة!",
        data={"kind": "trip_started", "ride_id": ride.id},
        collapse_key=f"ride:{ride.id}",
    )


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
    _emit_inbox(ride, "completed")
    push.send_to_customer(
        ride.customer_id,
        title="✅ وصلت بأمان",
        body=f"قيّم رحلتك — {float(ride.price_egp):.0f} ج.م",
        data={"kind": "trip_completed", "ride_id": ride.id},
        collapse_key=f"ride:{ride.id}",
    )


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
    _emit_inbox(ride, "cancelled")

    # Human-readable Arabic reason for the push body
    reason_ar = {
        "no_driver_available": "معلش، مفيش كباتن متاحين. جرب تاني.",
        "customer_cancelled": "اتلغت من العميل.",
        "admin_override": "الإدارة ألغت الرحلة.",
    }.get(reason, "الرحلة اتلغت.")

    push.send_to_customer(
        ride.customer_id,
        title="❌ الرحلة اتلغت",
        body=reason_ar,
        data={"kind": "trip_cancelled", "ride_id": ride.id, "reason": reason},
        collapse_key=f"ride:{ride.id}",
    )
    if ride.driver_id:
        push.send_to_driver(
            ride.driver_id,
            title="❌ الرحلة اتلغت",
            body=reason_ar,
            data={"kind": "trip_cancelled", "ride_id": ride.id, "reason": reason},
            collapse_key=f"ride:{ride.id}",
        )


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
    _emit_inbox(ride, "cancelled_no_show")
    push.send_to_customer(
        ride.customer_id,
        title="⚠️ رحلة اتلغت",
        body=f"الكابتن حضر وانت ماحضرتش. غرامة {float(_no_show_fee()):.0f} ج.م هتتحسب في رحلتك الجاية.",
        data={"kind": "trip_no_show", "ride_id": ride.id},
        collapse_key=f"ride:{ride.id}",
    )
