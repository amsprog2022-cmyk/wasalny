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
from datetime import datetime, timedelta
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
    return int(current_app.config.get("BROADCAST_ACCEPT_WINDOW_SECONDS", 30))


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


def release_claim(ride_id: int, driver_id: int) -> None:
    """Undo try_claim's locks when the assign didn't actually land.

    Only deletes keys this driver still owns, so we can't stomp on the
    captain who legitimately won the ride.
    """
    r = _r()
    driver_lock_key = f"driver:{driver_id}:current_ride"
    if r.get(driver_lock_key) == str(ride_id):
        r.delete(driver_lock_key)
    ride_lock_key = f"ride:{ride_id}:lock"
    if r.get(ride_lock_key) == str(driver_id):
        r.delete(ride_lock_key)


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
    """Ping every captain in the offer set — Socket.IO for foreground apps
    AND Firebase Cloud Messaging for backgrounded/killed apps."""
    # Include customer contact so the trip-offer screen can show name +
    # tap-to-call button before the captain even accepts.
    payload = {
        "ride": ride.to_dict(include_customer_contact=True),
        "expires_in_seconds": _accept_window(),
    }
    id_list = list(driver_ids)
    for did in id_list:
        socketio.emit(
            "trip_offered",
            payload,
            namespace="/driver",
            room=f"driver:{did}",
        )

    # FCM fan-out (safe no-op if the app is running in foreground OR if the
    # captain has no fcm_token yet — see push_notifications.send_to_drivers).
    from app.services import push_notifications as push
    from_ar = ride.from_zone.name_ar if ride.from_zone else ""
    to_ar = ride.to_zone.name_ar if ride.to_zone else ""
    # WhatsApp bookings start without a destination or price — captain sets
    # both on arrival. Distinguish the two body copies so captains know what
    # they're accepting.
    if ride.to_zone_id is None:
        body_ar = f"طلب واتساب من {from_ar} · الوجهة والسعر بعد الوصول"
    else:
        net_egp = float(ride.price_egp) * (1 - float(current_app.config.get("WASSALNY_COMMISSION_RATE", "0.15")))
        body_ar = f"من {from_ar} إلى {to_ar} · صافي {net_egp:.0f} ج.م"
    push.send_to_drivers(
        id_list,
        title="🔔 رحلة جديدة",
        body=body_ar,
        data={
            "kind": "trip_offered",
            "ride_id": ride.id,
            "expires_in_seconds": _accept_window(),
        },
        collapse_key=f"ride:{ride.id}",
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
    winner: Optional[int] = None

    # Phase 2: GPS matching — if the ride has pickup coordinates, run a
    # nearest-N radius cascade instead of the zone loop.
    if ride.pickup_lat is not None and ride.pickup_lng is not None:
        winner = _gps_match(ride, tried, r)

    # Only fall through to zone matching if the GPS path found ZERO candidates
    # in every radius (empty GEO index) OR the ride doesn't have GPS coords.
    if winner is None and (ride.pickup_lat is None or ride.pickup_lng is None or not tried):
        winner = _zone_match(ride, tried, r)

    # Cleanup Redis broadcast key regardless of outcome.
    r.delete(f"broadcast:{ride.id}:offered_to")

    if winner:
        db.session.refresh(ride)
        ride_lifecycle.assign(ride, winner, pending_fee_ids=pending_fee_ids)
        return

    db.session.refresh(ride)
    r.delete(f"ride:{ride.id}:lock")
    ride_lifecycle.cancel(ride, actor="system", reason="no_driver_available")
    _emit_no_driver_alert(ride)
    return


def _gps_match(ride: Ride, tried: set[int], r) -> Optional[int]:
    """Phase 2 GPS matching. Escalates through GEO_MATCH_RADII_KM until a
    captain accepts or we run out of radii. Returns the winner's driver_id
    or None if nobody accepts anywhere.
    """
    radii_str = current_app.config.get("GEO_MATCH_RADII_KM", "3,6,10")
    try:
        radii = [float(x.strip()) for x in str(radii_str).split(",") if x.strip()]
    except (TypeError, ValueError):
        radii = [3.0, 6.0, 10.0]
    top_n = int(current_app.config.get("GEO_MATCH_TOP_N", 1))

    for radius_km in radii:
        # Ask for more than top_n so we can filter for available + not-tried
        # without underpicking.
        candidates = av.nearest_drivers(
            ride.pickup_lat, ride.pickup_lng, radius_km=radius_km, limit=top_n * 4,
        )
        picked: list[int] = []
        for driver_id, _distance, _coords in candidates:
            if driver_id in tried:
                continue
            presence = av.get_presence(driver_id)
            if not presence.available:
                continue
            picked.append(driver_id)
            if len(picked) >= top_n:
                break

        # Audit row per radius so ops can see what the matcher tried.
        b = Broadcast(
            ride_id=ride.id,
            zone_id=ride.from_zone_id or 0,
            driver_ids_json=json.dumps(picked),
        )
        db.session.add(b)
        db.session.commit()

        if not picked:
            b.outcome = "no_drivers"
            b.ended_at = datetime.utcnow()
            db.session.commit()
            continue

        tried.update(picked)

        # Reuse the existing Redis broadcast + FCM push + Socket.IO fan-out.
        r.sadd(f"broadcast:{ride.id}:offered_to", *[str(x) for x in picked])
        r.expire(f"broadcast:{ride.id}:offered_to", _accept_window() + 5)
        _emit_offer(ride, picked)

        winner = _wait_for_accept(ride.id, _accept_window())
        b.ended_at = datetime.utcnow()
        if winner:
            b.outcome = "accepted"
            b.accepted_by_driver_id = winner
            db.session.commit()
            return winner

        b.outcome = "timeout"
        db.session.commit()
        for did in picked:
            socketio.emit(
                "trip_offer_expired",
                {"ride_id": ride.id},
                namespace="/driver",
                room=f"driver:{did}",
            )
    return None


def _zone_match(ride: Ride, tried: set[int], r) -> Optional[int]:
    """Legacy zone-based matching. Kept for WhatsApp rides + as fallback
    when the GPS GEO index is empty. Same behaviour as before Phase 2.
    """
    current_zone: Optional[Zone] = ride.from_zone
    winner: Optional[int] = None
    round_no = 0
    while round_no < _max_rounds():
        if current_zone is None:
            break
        tried.add(current_zone.id)

        driver_ids = av.available_drivers_in_zone(current_zone.id)
        # Respect the same TOP_N cap the GPS path uses — otherwise the zone
        # fallback silently fans out to EVERY driver in the zone and two
        # nearby phones both ring. Nuclear guarantee: at most top_n offers
        # per wave, per matcher path.
        top_n = int(current_app.config.get("GEO_MATCH_TOP_N", 1))
        driver_ids = driver_ids[:top_n]
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
    return winner


def _emit_no_driver_alert(ride: Ride) -> None:
    """Create the admin AdminAlert(kind=no_driver) + live-push to /inbox +
    (for WhatsApp rides) message the customer telling them the admin will
    reach out. Best-effort — swallow failures so a broken emit doesn't
    affect the ride cancellation that already happened."""
    try:
        from app.models.ai_session import AdminAlert
        alert = AdminAlert(
            kind="no_driver",
            payload_json=json.dumps(
                {
                    "from_zone_id": ride.from_zone_id,
                    "from_zone_ar": (ride.from_zone.name_ar if ride.from_zone else None),
                    "to_zone_id": ride.to_zone_id,
                    "to_zone_ar": (ride.to_zone.name_ar if ride.to_zone else None),
                    "pickup_lat": ride.pickup_lat,
                    "pickup_lng": ride.pickup_lng,
                    "pickup_address": ride.pickup_address,
                    "dropoff_address": ride.dropoff_address,
                    "source": ride.source,
                    "customer_wa_id": (ride.customer.wa_id if ride.customer else None),
                },
                ensure_ascii=False,
            ),
            customer_id=ride.customer_id,
            ride_id=ride.id,
        )
        db.session.add(alert)
        db.session.commit()
        socketio.emit(
            "no_driver_alert_new",
            {
                "id": alert.id,
                "ride_id": ride.id,
                "customer_id": ride.customer_id,
                "from_zone_ar": ride.from_zone.name_ar if ride.from_zone else None,
            },
            namespace="/inbox",
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("no_driver alert create failed: %s", e)

    # WhatsApp customer escalation — tell them a human will help so they're
    # not left in silence after the last "بندور على كابتن" reply.
    if ride.source == "whatsapp" and ride.customer is not None and ride.customer.wa_id:
        try:
            from app.services import whatsapp as wa
            from app.services import whatsapp_booking
            body = (
                "😔 معلش، مفيش كابتن متاح دلوقتي في المنطقة دي. "
                "الأدمن هيتواصل معاك ويشوفلك حل. "
                "لو مش عايز تكمل، رد بكلمة \"إلغاء\"."
            )
            resp = wa.send_text(ride.customer.wa_id, body)
            wa_msg_id = None
            if isinstance(resp, dict):
                wa_msg_id = (resp.get("messages") or [{}])[0].get("id")
            whatsapp_booking._persist_outbound(
                ride.customer, body, msg_type="text", wa_message_id=wa_msg_id,
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("no_driver customer notice failed: %s", e)


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
                _report_to_sentry(e, ride_id=ride_id, where="match_ride")

    eventlet.spawn_n(_worker)


# ---------- Sentry ----------

def _report_to_sentry(exc: BaseException, **tags) -> None:
    """Best-effort Sentry capture. Silent no-op when the SDK isn't installed
    or SENTRY_DSN isn't set."""
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, v)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass


# ---------- stuck-broadcasting sweeper ----------

STUCK_BROADCAST_AGE_SECONDS = 90
SWEEPER_INTERVAL_SECONDS = 30
SWEEPER_LOCK_KEY = "sweeper:matching:lock"


def sweep_stuck_broadcasting() -> int:
    """Cancel rides wedged in `broadcasting` because their matching greenlet
    died (worker crash, deploy mid-round, etc.). Returns the count cancelled.

    Safe to run from any worker — leader-elected via a Redis SETNX lock, and
    each individual cancel is guarded by ride_lifecycle's own status check
    so double-cancels raise ValueError (which we swallow).
    """
    r = _r()
    # Only one worker sweeps per interval. TTL slightly under interval so
    # we're never blocked across ticks even if a worker crashes mid-sweep.
    if not r.set(SWEEPER_LOCK_KEY, "1", nx=True, ex=SWEEPER_INTERVAL_SECONDS - 5):
        return 0

    cutoff = datetime.utcnow() - timedelta(seconds=STUCK_BROADCAST_AGE_SECONDS)
    stuck = (
        Ride.query.filter(
            Ride.status == "broadcasting",
            Ride.created_at < cutoff,
        )
        .all()
    )
    if not stuck:
        return 0

    cancelled = 0
    for ride in stuck:
        # If the offer set is still populated, a greenlet is actively
        # broadcasting this ride — leave it alone.
        if r.exists(f"broadcast:{ride.id}:offered_to"):
            continue
        try:
            ride_lifecycle.cancel(ride, actor="system",
                                  reason="stuck_broadcasting_swept")
            cancelled += 1
            current_app.logger.warning(
                "sweeper cancelled stuck ride %s (age=%ss)",
                ride.id, int((datetime.utcnow() - ride.created_at).total_seconds()),
            )
            _report_to_sentry(
                RuntimeError(f"ride {ride.id} stuck in broadcasting; swept"),
                ride_id=ride.id, where="sweeper",
            )
        except ValueError:
            # Another actor moved the ride's status between the query and
            # the cancel — nothing to do.
            db.session.rollback()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.warning("sweeper cancel failed for %s: %s", ride.id, e)
            _report_to_sentry(e, ride_id=ride.id, where="sweeper_cancel")
    return cancelled


def start_sweeper(app) -> None:
    """Spawn the periodic sweeper greenlet. Called once per worker from
    create_app(). Every worker runs the loop; the Redis leader-lock in
    sweep_stuck_broadcasting() ensures only one actually sweeps per tick."""
    def _loop():
        # Small jitter so N workers don't all hit Redis in the same millisecond.
        eventlet.sleep(2 + (id(app) % 5))
        while True:
            try:
                with app.app_context():
                    sweep_stuck_broadcasting()
            except Exception as e:  # noqa: BLE001
                try:
                    app.logger.warning("sweeper loop error: %s", e)
                except Exception:  # noqa: BLE001
                    pass
            eventlet.sleep(SWEEPER_INTERVAL_SECONDS)

    eventlet.spawn_n(_loop)
