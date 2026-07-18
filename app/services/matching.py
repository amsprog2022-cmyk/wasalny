"""Broadcast matching engine (PLAN §11).

Runs as an eventlet greenlet spawned when a ride is created. Because we're on
eventlet, `time.sleep` is cooperative and won't block the web worker.

Redis contract (all TTL'd, ephemeral):

  broadcast:{ride_id}:offered_to   SET     driver_ids currently seeing the offer
  ride:{ride_id}:lock              STRING  atomic reservation lock (SET NX)

Winner protocol:
  - Captain app POSTs /api/v1/rides/{id}/accept.
  - The endpoint runs SET NX on the lock. First one wins.
  - If it wins, the endpoint publishes to `ride:{id}:accepted` with the driver_id.
  - This greenlet subscribes to that channel and returns when it fires.
  - If timeout expires, we move on to the next zone.

Fairness:
  - Captains are read from `zone:{id}:available_drivers` sorted by last-activity
    ascending — whoever's been idle longest sits at the top of the offer.

Fully-connected adjacency (Decision #15):
  - Any zone can be an expansion of any other, so the "expand" step just picks
    the zone with the most available drivers among ones we haven't tried yet.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Iterable, Optional

import eventlet

from flask import current_app

from app import db, socketio
from app.extensions import get_redis
from app.models.ride import Ride, Broadcast
from app.models.zone import Zone
from app.services import availability as av
from app.services import ride_lifecycle


def _r():
    return get_redis(current_app.config.get("REDIS_URL"))


def _accept_window() -> int:
    return int(current_app.config.get("BROADCAST_ACCEPT_WINDOW_SECONDS", 10))


def _max_rounds() -> int:
    return int(current_app.config.get("MATCHING_MAX_ROUNDS", 3))


# ---------- lock / accept protocol ----------

def try_claim(ride_id: int, driver_id: int, ttl_seconds: int = 15) -> bool:
    """Called from POST /rides/{id}/accept.

    Two atomic locks must both succeed:
      1. `driver:{id}:current_ride` — this driver has no other active trip.
      2. `ride:{id}:lock` — nobody else already claimed this ride.

    Returns True only if we win BOTH. If we lose the ride lock after winning
    the driver lock, we release the driver lock so they can try the next offer.
    """
    r = _r()

    # 1) Try to lock the DRIVER first. Stops one captain from winning two rides.
    driver_lock_key = f"driver:{driver_id}:current_ride"
    driver_locked = r.set(driver_lock_key, str(ride_id), nx=True, ex=ttl_seconds)
    if not driver_locked:
        return False

    # 2) Try to lock the RIDE. Stops two captains from winning the same ride.
    ride_lock_key = f"ride:{ride_id}:lock"
    ride_locked = r.set(ride_lock_key, str(driver_id), nx=True, ex=ttl_seconds)
    if not ride_locked:
        # Release my driver lock so I can accept the next offer
        current = r.get(driver_lock_key)
        if current == str(ride_id):
            r.delete(driver_lock_key)
        return False

    # Won both — tell the matching greenlet
    r.publish(f"ride:{ride_id}:accepted", str(driver_id))
    return True


def _wait_for_accept(ride_id: int, timeout_s: int) -> Optional[int]:
    """Block up to `timeout_s` waiting for a driver to win the lock.

    Uses Redis pubsub. Falls back to polling the lock key so it still works
    even if pubsub messages are dropped.
    """
    r = _r()
    channel = f"ride:{ride_id}:accepted"
    pubsub = r.pubsub()
    pubsub.subscribe(channel)

    deadline = time.time() + timeout_s
    winner: Optional[int] = None
    try:
        while time.time() < deadline:
            msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.25)
            if msg and msg.get("type") == "message":
                winner = int(msg["data"])
                break
            # Cooperative yield to eventlet
            eventlet.sleep(0)
            # Fallback: poll the lock key in case pubsub was missed
            v = r.get(f"ride:{ride_id}:lock")
            if v:
                try:
                    winner = int(v)
                except (TypeError, ValueError):
                    winner = None
                if winner is not None:
                    break
    finally:
        try:
            pubsub.unsubscribe(channel)
            pubsub.close()
        except Exception:
            pass
    return winner


# ---------- zone expansion (fully-connected adjacency, Decision #15) ----------

def _pick_next_zone(current_ride: Ride, tried: set[int]) -> Optional[Zone]:
    """Return the untried active zone with the most available captains."""
    candidates = (
        Zone.query.filter(Zone.is_active.is_(True), ~Zone.id.in_(tried))
        .all()
    )
    if not candidates:
        return None
    counts = av.zone_counts([z.id for z in candidates])
    return max(candidates, key=lambda z: counts.get(z.id, 0))


# ---------- offer emission ----------

def _emit_offer(ride: Ride, driver_ids: Iterable[int]) -> None:
    payload = {
        "ride": ride.to_dict(),
        "expires_in_seconds": _accept_window(),
    }
    for did in driver_ids:
        socketio.emit(
            "trip_offered",
            payload,
            namespace="/driver",
            room=f"driver:{did}",
        )


# ---------- entry point ----------

def match_ride(ride_id: int, pending_fee_ids: list[int] | None = None) -> None:
    """Attempt to assign a captain to the ride.

    Mutates the ride status through broadcasting → assigned (on win) or
    cancelled (if no captain accepts in any expanded zone).
    """
    r = _r()
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return
    if ride.status in ("assigned", "started", "completed", "cancelled", "cancelled_no_show"):
        return

    ride_lifecycle.mark_broadcasting(ride)

    tried: set[int] = set()
    current_zone: Optional[Zone] = ride.from_zone
    winner: Optional[int] = None

    round_no = 0
    while round_no < _max_rounds():
        if current_zone is None:
            break
        tried.add(current_zone.id)

        driver_ids = av.available_drivers_in_zone(current_zone.id)
        b = Broadcast(
            ride_id=ride.id,
            zone_id=current_zone.id,
            driver_ids_json=json.dumps(driver_ids),
        )
        db.session.add(b)
        db.session.commit()

        if not driver_ids:
            b.outcome = "no_drivers"
            b.ended_at = datetime.utcnow()
            db.session.commit()
            current_zone = _pick_next_zone(ride, tried)
            round_no += 1
            continue

        # Mark broadcast state in Redis
        r.sadd(f"broadcast:{ride.id}:offered_to", *[str(x) for x in driver_ids])
        r.expire(f"broadcast:{ride.id}:offered_to", _accept_window() + 5)

        _emit_offer(ride, driver_ids)

        winner = _wait_for_accept(ride.id, _accept_window())

        b.ended_at = datetime.utcnow()
        if winner:
            b.outcome = "accepted"
            b.accepted_by_driver_id = winner
            db.session.commit()
            break
        else:
            b.outcome = "timeout"
            db.session.commit()
            # Tell captains the offer window closed
            for did in driver_ids:
                socketio.emit(
                    "trip_offer_expired",
                    {"ride_id": ride.id},
                    namespace="/driver",
                    room=f"driver:{did}",
                )
            current_zone = _pick_next_zone(ride, tried)
            round_no += 1

    # Cleanup Redis broadcast key regardless of outcome
    r.delete(f"broadcast:{ride.id}:offered_to")

    if winner:
        # Re-load in case of session state drift, then assign.
        db.session.refresh(ride)
        ride_lifecycle.assign(ride, winner, pending_fee_ids=pending_fee_ids)
    else:
        db.session.refresh(ride)
        # Release the lock in case someone won after we gave up (rare).
        r.delete(f"ride:{ride.id}:lock")
        ride_lifecycle.cancel(ride, actor="system", reason="no_driver_available")


def start_matching(ride_id: int, pending_fee_ids: list[int] | None = None) -> None:
    """Spawn the matching greenlet.

    Wrapped so the caller (a Flask request handler) can return immediately
    while matching runs in the background.
    """
    app = current_app._get_current_object()
    pending_fee_ids = pending_fee_ids or []

    def _worker():
        with app.app_context():
            try:
                match_ride(ride_id, pending_fee_ids=pending_fee_ids)
            except Exception as e:
                app.logger.exception("matching failed for ride %s: %s", ride_id, e)

    eventlet.spawn_n(_worker)
