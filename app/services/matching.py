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


def _blocked_driver_ids() -> set[int]:
    """Captains admin doesn't want to receive rides right now — suspended or
    banned discipline. Includes soft-deleted rows for safety. Nuclear
    enforcement point: both matching paths must skip these before emitting.
    """
    from app.models.driver import Driver
    rows = (
        db.session.query(Driver.id)
        .filter(
            (Driver.discipline_status.in_(("suspended", "banned"))) |
            (Driver.deleted_at.isnot(None))
        )
        .all()
    )
    return {r[0] for r in rows}


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
    # Prefer the reverse-geocoded street addresses — zone names cover huge
    # polygons ("بنها") which tells the captain nothing about where to go.
    # Fall back to the zone name if the reverse-geocode failed at booking.
    from_txt = (ride.pickup_address or
                (ride.from_zone.name_ar if ride.from_zone else "")).strip()
    to_txt = (ride.dropoff_address or
              (ride.to_zone.name_ar if ride.to_zone else "")).strip()
    # WhatsApp bookings start without a destination or price — captain sets
    # both on arrival. Distinguish the two body copies so captains know what
    # they're accepting.
    if ride.to_zone_id is None and not ride.dropoff_address:
        label = "طلب مكتب" if ride.source == "office" else "طلب واتساب"
        body_ar = f"{label} من {from_txt} · الوجهة والسعر بعد الوصول"
    else:
        net_egp = float(ride.price_egp) * (1 - float(current_app.config.get("WASSALNY_COMMISSION_RATE", "0.15")))
        body_ar = f"من {from_txt} إلى {to_txt} · صافي {net_egp:.0f} ج.م"
    # Inline the ride payload as JSON so the captain app can hydrate the
    # TripOfferScreen the moment the notification is tapped — no need to
    # wait for the /driver/active-ride REST roundtrip after cold-open.
    # FCM data has a ~4KB cap; ride.to_dict() is comfortably under.
    push.send_to_drivers(
        id_list,
        title="🔔 رحلة جديدة",
        body=body_ar,
        data={
            "kind": "trip_offered",
            "ride_id": ride.id,
            "expires_in_seconds": _accept_window(),
            "ride_json": json.dumps(
                ride.to_dict(include_customer_contact=True),
                default=str, ensure_ascii=False,
            ),
        },
        collapse_key=f"ride:{ride.id}",
    )


# ---------- entry point ----------

def match_ride(ride_id: int, pending_fee_ids: list[int] | None = None) -> None:
    """Attempt to assign a captain to the ride.

    Two paths, in order:
      1. Chained ride: reserve for a captain finishing a nearby trip.
         When this succeeds, customer/office notifications have already
         fired inside try_queue_ride and we return early.
      2. Normal ladder / GPS matching: today's behavior.

    Mutates the ride status through queued → broadcasting → assigned
    (queued only via path 1; broadcasting only via path 2).
    """
    r = _r()
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return
    if ride.status in ("assigned", "started", "completed", "cancelled", "cancelled_no_show", "queued"):
        return

    # Path 1: chained-ride reservation. Silent when no captain qualifies.
    if try_queue_ride(ride):
        return

    current_app.logger.info(
        "match_ride ride=%s top_n=%s window=%ss radii=%s",
        ride_id,
        current_app.config.get("GEO_MATCH_TOP_N"),
        _accept_window(),
        current_app.config.get("GEO_MATCH_RADII_KM"),
    )
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
    blocked = _blocked_driver_ids()

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
            if driver_id in blocked:
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
        # Filter blocked (suspended/banned/deleted) captains here too.
        blocked = _blocked_driver_ids()
        driver_ids = [d for d in driver_ids if d not in blocked]
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


# ---------- office dispatch ----------

def match_office_ride(ride_id: int, pending_fee_ids: list[int] | None = None) -> None:
    """Matching for a trip the office took by phone and pasted into WhatsApp.

    Distance ladder: nearest 3 captains first, then next 7, then next 15,
    then everyone else with an FCM token. Each ring gets its own accept
    window and skips captains already offered in earlier rings. This
    replaces the old blast-to-everyone approach which pinged 300 captains
    per lost trip; now the typical trip closes in ring 1 with 3 pushes
    and only a genuinely un-fillable trip ever reaches the "rest" ring.

    Ranking source per captain:
      - Redis GEO (fresh, updated by every driver_position socket ping)
      - Falls back to Driver.latitude/longitude snapshot for offline captains
      - Captains with no position on record only appear in the final ring

    Fastest-wins guarantee is unchanged: every accept still goes through
    `try_claim`, the ladder just changes who hears each round.
    """
    r = _r()
    ride = db.session.get(Ride, ride_id)
    if ride is None:
        return
    if ride.status in ("assigned", "started", "completed", "cancelled", "cancelled_no_show", "queued"):
        return

    # Chained ride reservation runs first — same as the app path. Office
    # queue-hits get the exact same office reply as a normal assignment,
    # because try_queue_ride fires notify_office_assigned itself.
    if try_queue_ride(ride):
        return

    ride_lifecycle.mark_broadcasting(ride)
    tried: set[int] = set()
    winner: Optional[int] = None

    has_pickup = ride.pickup_lat is not None and ride.pickup_lng is not None

    if has_pickup:
        # Ladder rings — sizes and windows kept short of a full deploy via
        # env knobs, defaults chosen for a ~3.5 min total. The last "rest"
        # ring is unbounded and includes captains with no known position.
        ring_sizes = _parse_int_list(
            current_app.config.get("OFFICE_LADDER_RING_SIZES", "3,7,15"),
        )
        ring_windows = _parse_int_list(
            current_app.config.get("OFFICE_LADDER_WINDOWS_SECONDS", "30,45,60,60"),
        )
        # Pad windows with the last value so a shorter list still works.
        while len(ring_windows) < len(ring_sizes) + 1:
            ring_windows.append(ring_windows[-1] if ring_windows else 60)

        # Re-rank captains at each ring — since _office_ranked_candidates
        # already filters out anyone in `tried`, the first N of a fresh
        # ranking is exactly "the next N closest we haven't offered yet",
        # so newly-online / just-finished captains slot into their real
        # distance rank instead of being frozen at an early snapshot.
        for ring_idx, size in enumerate(ring_sizes):
            if winner is not None:
                break
            ranked = _office_ranked_candidates(
                ride.pickup_lat, ride.pickup_lng, tried,
                include_positionless=False,
            )
            batch = ranked[:size]
            if not batch:
                continue
            window_s = int(ring_windows[ring_idx])
            winner = _office_round(ride, batch, tried, r, window_s)

        # Final ring: everyone else eligible, including captains we have
        # no position for (they were skipped in the ranked rings above).
        if winner is None:
            rest = _office_rest_candidates(tried)
            if rest:
                window_s = int(ring_windows[len(ring_sizes)])
                winner = _office_round(ride, rest, tried, r, window_s)
    else:
        # No pickup coords → can't rank by distance, so fall back to a
        # single blast covering everyone. Same window as the final ring.
        window_s = int(current_app.config.get("OFFICE_BLAST_WINDOW_SECONDS", 60))
        winner = _office_round(
            ride, _office_rest_candidates(tried), tried, r, window_s,
        )

    r.delete(f"broadcast:{ride.id}:offered_to")

    if winner:
        db.session.refresh(ride)
        ride_lifecycle.assign(ride, winner, pending_fee_ids=pending_fee_ids)
        from app.services import office_dispatch
        office_dispatch.notify_office_assigned(ride)
        return

    db.session.refresh(ride)
    r.delete(f"ride:{ride.id}:lock")
    ride_lifecycle.cancel(ride, actor="system", reason="no_driver_available")
    _emit_no_driver_alert(ride)
    from app.services import office_dispatch
    office_dispatch.notify_office_no_driver(ride)


def _parse_int_list(raw) -> list[int]:
    """'3,7,15' → [3, 7, 15]. Silently drops junk."""
    out: list[int] = []
    if raw is None:
        return out
    for piece in str(raw).split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            continue
    return out


def _office_ranked_candidates(
    pickup_lat: float, pickup_lng: float, tried: set[int],
    include_positionless: bool = False,
) -> list[int]:
    """Every eligible captain sorted by distance to pickup, ascending.

    Ranking source per captain: Redis GEO first (freshest — updated on
    every driver_position socket ping), then the Driver.latitude/longitude
    snapshot (last-known, used for offline captains). Captains with no
    position on record anywhere are only included when
    include_positionless=True, and then appended at the end unranked.

    Excludes captains in `tried`, blocked, deleted, without an FCM token,
    or currently on another trip. The busy check + geopos lookups are
    pipelined so ranking 300 captains costs one Redis round-trip instead
    of 600 sequential calls — critical under bursty office load.
    """
    from app.models.driver import Driver
    from app.services.availability import GEO_KEY
    from app.services.geo import haversine_km

    blocked = _blocked_driver_ids()
    r = _r()

    # Pull the whole eligible pool in one Postgres query, including the
    # position snapshot so the fallback for offline captains is free.
    rows = (
        db.session.query(Driver.id, Driver.latitude, Driver.longitude)
        .filter(Driver.fcm_token.isnot(None), Driver.deleted_at.is_(None))
        .all()
    )
    eligible = [
        (did, snap_lat, snap_lng)
        for (did, snap_lat, snap_lng) in rows
        if did not in tried and did not in blocked
    ]
    if not eligible:
        return []

    # Batch the busy-check + GEO fetch in one Redis pipeline. On Railway's
    # external Redis (~1 ms/call), the difference between 300 sequential
    # calls and 1 pipelined round-trip is ~1s of pure network wait per
    # matching cycle, per unmatched trip.
    with r.pipeline(transaction=False) as pipe:
        for did, _, _ in eligible:
            pipe.exists(f"driver:{did}:current_ride")
            pipe.geopos(GEO_KEY, str(did))
        pipe_results = pipe.execute()

    ranked: list[tuple[float, int]] = []
    positionless: list[int] = []
    for i, (did, snap_lat, snap_lng) in enumerate(eligible):
        busy = bool(pipe_results[i * 2])
        if busy:
            continue

        geo = pipe_results[i * 2 + 1]
        pos: tuple[float, float] | None = None
        if geo and geo[0] is not None:
            lng, lat = geo[0]
            pos = (float(lat), float(lng))
        if pos is None and snap_lat is not None and snap_lng is not None:
            pos = (float(snap_lat), float(snap_lng))
        if pos is None:
            positionless.append(did)
            continue

        dist = haversine_km(pickup_lat, pickup_lng, pos[0], pos[1])
        ranked.append((dist, did))

    ranked.sort(key=lambda t: t[0])
    result = [did for _, did in ranked]
    if include_positionless:
        result.extend(positionless)
    return result


def _office_rest_candidates(tried: set[int]) -> list[int]:
    """Every eligible captain regardless of distance/position — for the
    final ladder ring. Includes captains with no position on record.
    Busy check is pipelined for the same one-round-trip reason as
    _office_ranked_candidates."""
    from app.models.driver import Driver

    blocked = _blocked_driver_ids()
    r = _r()
    rows = (
        db.session.query(Driver.id)
        .filter(Driver.fcm_token.isnot(None), Driver.deleted_at.is_(None))
        .all()
    )
    candidates = [
        did for (did,) in rows
        if did not in tried and did not in blocked
    ]
    if not candidates:
        return []
    with r.pipeline(transaction=False) as pipe:
        for did in candidates:
            pipe.exists(f"driver:{did}:current_ride")
        busy_flags = pipe.execute()
    return [did for did, busy in zip(candidates, busy_flags) if not busy]


# ---------- chained rides (en-route matching) ----------

def offer_queued_to(driver_id: int) -> None:
    """Fire the queued ride's accept screen for `driver_id`, if any.

    Called by ride_lifecycle.complete() the moment the captain finishes
    his current trip. Uses the standard _emit_offer + _wait_for_accept
    flow so the captain sees the normal accept screen with countdown.

    Outcomes:
      - Captain accepts → try_claim + ride_lifecycle.assign(). The
        customer sees no visible change (they were told the same
        captain 8 min ago, and now he actually starts the trip).
      - Captain rejects / times out → release the queue, drop the
        Redis lock, kick off a fresh matching cycle. The customer
        will see a second "الكابتن X قبل الرحلة" with a different
        captain — silent swap.
    """
    ride = (
        Ride.query.filter(
            Ride.queued_for_driver_id == driver_id,
            Ride.status == "queued",
        )
        .order_by(Ride.id.asc())
        .first()
    )
    if ride is None:
        return

    app = current_app._get_current_object()

    def _worker():
        with app.app_context():
            try:
                _offer_queued_ride_worker(ride.id, driver_id)
            except Exception as e:  # noqa: BLE001
                app.logger.exception(
                    "queued offer failed for ride %s / driver %s: %s",
                    ride.id, driver_id, e,
                )

    eventlet.spawn_n(_worker)


def _offer_queued_ride_worker(ride_id: int, driver_id: int) -> None:
    """The hand-off flow, run in its own greenlet.

    Sequence:
      1. Flip ride status queued → broadcasting so try_claim on accept
         behaves like any other broadcast.
      2. Emit offer + wait 45s.
      3. Winner (only ever this one captain) → ride_lifecycle.assign
         with a from_queue_handoff=True hint so it doesn't re-notify
         the customer (they already got "captain assigned" at queue time).
      4. Timeout/reject → release the queue slot + fresh matching.
    """
    r = _r()
    ride = db.session.get(Ride, ride_id)
    if ride is None or ride.status != "queued":
        return

    ride.status = "broadcasting"
    db.session.commit()

    # Match the schema _office_round uses so try_claim's pubsub notify
    # lands in the same room the accept endpoint publishes to.
    r.sadd(f"broadcast:{ride.id}:offered_to", str(driver_id))
    r.expire(f"broadcast:{ride.id}:offered_to", 60)
    _emit_offer(ride, [driver_id])

    window_s = int(current_app.config.get("QUEUE_HANDOFF_WINDOW_SECONDS", 45))
    winner = _wait_for_accept(ride.id, window_s)

    r.delete(f"broadcast:{ride.id}:offered_to")
    r.delete(f"driver:{driver_id}:queue_lock")

    if winner == driver_id:
        db.session.refresh(ride)
        # from_queue_handoff so assign() skips the customer re-notify
        # (they were already told this captain's name at queue time).
        ride_lifecycle.assign(
            ride, driver_id, pending_fee_ids=None, from_queue_handoff=True,
        )
        return

    # Nobody accepted → release the queue, wipe the linkage, and let a
    # fresh normal-matching cycle find a different captain for this
    # customer. The customer will see a second "captain assigned" text.
    db.session.refresh(ride)
    ride.queued_for_driver_id = None
    ride.driver_id = None
    ride.status = "new"  # start_matching guards against non-new states
    db.session.commit()
    # Tell the previously-offered captain the offer expired so his
    # in-trip badge clears (in case he was watching it).
    try:
        socketio.emit(
            "trip_offer_expired",
            {"ride_id": ride.id},
            namespace="/driver",
            room=f"driver:{driver_id}",
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("expired emit failed: %s", e)
    if ride.source == "office":
        start_office_matching(ride.id)
    else:
        start_matching(ride.id)


def try_queue_ride(ride: Ride, radius_km: float = 3.0) -> bool:
    """Try to reserve `ride` for a captain currently on a nearby trip.

    Returns True when we successfully queued. The caller MUST then skip
    the normal ladder/broadcast — the customer/office has already been
    notified as if this were a regular assignment.

    Contract:
      - Skips silently when `ride` has no pickup coord (bare-number
        office pastes fall through to the ladder as before).
      - Uses a per-captain Redis lock (`driver:{id}:queue_lock`) to make
        two concurrent queue attempts race-safe: only one wins.
      - Prices via the same distance-based `quote_by_coords` used for
        any app ride — customer pays what they'd pay for a normal trip.
      - Fires `dispatch_notifications.notify_customer_of_assignment`
        (app + WhatsApp customers) OR `office_dispatch.notify_office_assigned`
        (office rides) so the customer sees an ordinary "captain assigned"
        message with zero hint they've been queued.
    """
    if ride.pickup_lat is None or ride.pickup_lng is None:
        return False

    captain_id = _queue_eligible_captain(
        ride.pickup_lat, ride.pickup_lng, radius_km=radius_km,
    )
    if captain_id is None:
        return False

    r = _r()
    # Per-captain queue lock — 15 min TTL matches queue_expires_at.
    queue_ttl = int(current_app.config.get("QUEUE_TTL_SECONDS", 900))
    lock_key = f"driver:{captain_id}:queue_lock"
    got = r.set(lock_key, str(ride.id), nx=True, ex=queue_ttl)
    if not got:
        return False

    # Price the ride like any GPS booking. If the dropoff is missing
    # (deferred flow), leave the existing zero price alone — captain
    # will confirm on arrival exactly as with a normal WhatsApp trip.
    from decimal import Decimal
    try:
        if ride.dropoff_lat is not None and ride.dropoff_lng is not None:
            from app.services import pricing as pricing_svc
            q = pricing_svc.quote_by_coords(
                ride.customer_id, ride.pickup_lat, ride.pickup_lng,
                ride.dropoff_lat, ride.dropoff_lng,
            )
            ride.price_egp = Decimal(str(q.ride_price_egp))
            ride.commission_egp = Decimal(str(q.commission_egp))
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("queue quote failed for ride %s: %s", ride.id, e)

    ride.status = "queued"
    ride.driver_id = captain_id
    ride.queued_for_driver_id = captain_id
    ride.queue_expires_at = datetime.utcnow() + timedelta(seconds=queue_ttl)
    db.session.commit()

    # From here on, the customer sees identical UX to a normal assignment.
    from app.models.driver import Driver
    driver = db.session.get(Driver, captain_id)
    if ride.source == "office":
        try:
            from app.services import office_dispatch
            office_dispatch.notify_office_assigned(ride)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("queue office notify failed: %s", e)
    else:
        if ride.customer is not None and driver is not None:
            try:
                from app.services import dispatch_notifications
                dispatch_notifications.notify_customer_of_assignment(
                    ride.customer, driver,
                )
            except Exception as e:  # noqa: BLE001
                current_app.logger.warning(
                    "queue customer notify failed: %s", e
                )

    # App customers also need the socket event or they'd stay stuck on
    # "بندور على كابتن..." forever. The customer_app handler flips
    # isSearching=False on trip_assigned and swaps in the assigned ride.
    if ride.source == "app":
        try:
            socketio.emit(
                "trip_assigned",
                {"ride": ride.to_dict()},
                namespace="/customer",
                room=f"customer:{ride.customer_id}",
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("queue customer socket emit failed: %s", e)

    # Real-time badge for the captain's trip-in-progress screen.
    try:
        socketio.emit(
            "queued_ride_attached",
            {
                "queued_next": {
                    "ride_id": ride.id,
                    "pickup_address": ride.pickup_address or "",
                    "dropoff_address": ride.dropoff_address or "",
                    "price_egp": float(ride.price_egp or 0),
                },
            },
            namespace="/driver",
            room=f"driver:{captain_id}",
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("queued_ride_attached emit failed: %s", e)

    current_app.logger.info(
        "ride %s queued for captain %s (radius %.2f km)",
        ride.id, captain_id, radius_km,
    )
    return True


def _queue_eligible_captain(
    pickup_lat: float, pickup_lng: float, radius_km: float = 3.0,
) -> Optional[int]:
    """Return the closest captain whose CURRENT trip's destination is
    within `radius_km` of the new pickup, or None if nobody qualifies.

    A captain qualifies when he:
      - has an active ride (status assigned / started)
      - that ride has a known dropoff coord (excludes pre-arrival
        WhatsApp/office trips — those don't know their destination yet)
      - has wants_chained_rides = True
      - is not soft-deleted
      - doesn't already have a queued ride linked to him

    Ranking is by Haversine distance from the captain's current ride
    dropoff to the new pickup. Single Postgres query + Python loop —
    dropoff coords are on the Ride row so no per-driver lookups.
    """
    from app.models.driver import Driver
    from app.services.geo import haversine_km

    # Captains already reserved for a queued ride are out — one queue
    # slot per captain to keep the state simple.
    already_queued = {
        did for (did,) in db.session.query(Ride.queued_for_driver_id)
        .filter(Ride.status == "queued",
                Ride.queued_for_driver_id.isnot(None))
        .all()
    }

    rows = (
        db.session.query(Ride.driver_id, Ride.dropoff_lat, Ride.dropoff_lng)
        .join(Driver, Driver.id == Ride.driver_id)
        .filter(
            Ride.status.in_(("assigned", "started")),
            Ride.driver_id.isnot(None),
            Ride.dropoff_lat.isnot(None),
            Ride.dropoff_lng.isnot(None),
            Driver.wants_chained_rides.is_(True),
            Driver.deleted_at.is_(None),
        )
        .all()
    )

    best_id: Optional[int] = None
    best_km = radius_km + 1
    for did, dlat, dlng in rows:
        if did in already_queued:
            continue
        km = haversine_km(dlat, dlng, pickup_lat, pickup_lng)
        if km < best_km:
            best_km = km
            best_id = did
    return best_id if best_km <= radius_km else None


def _office_nearest_candidates(ride: Ride, tried: set[int]) -> list[int]:
    """The single closest available captain to the pickup.

    Kept for backwards compatibility (unused after the ladder rewrite),
    but callable if something else in the codebase wants it.
    """
    radius = float(current_app.config.get("OFFICE_NEAREST_RADIUS_KM", 10))
    blocked = _blocked_driver_ids()
    for driver_id, _distance, _coords in av.nearest_drivers(
        ride.pickup_lat, ride.pickup_lng, radius_km=radius, limit=8,
    ):
        if driver_id in tried or driver_id in blocked:
            continue
        if not av.get_presence(driver_id).available:
            continue
        return [driver_id]
    return []


def _office_blast_candidates(tried: set[int]) -> list[int]:
    """Every captain we can physically reach — offline ones included.

    Presence is deliberately not checked. A captain whose app is closed still
    gets the FCM push, and accepting it flips him online (ride_lifecycle.assign).
    The only exclusion beyond discipline is a captain already on a trip.
    """
    from app.models.driver import Driver

    blocked = _blocked_driver_ids()
    rows = (
        db.session.query(Driver.id)
        .filter(Driver.fcm_token.isnot(None), Driver.deleted_at.is_(None))
        .all()
    )
    r = _r()
    return [
        did for (did,) in rows
        if did not in tried and did not in blocked
        and not r.exists(f"driver:{did}:current_ride")
    ]


def _office_round(ride: Ride, picked: list[int], tried: set[int], r,
                  window_s: int) -> Optional[int]:
    """Offer to `picked` and wait `window_s` for one of them to claim it."""
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
        return None

    tried.update(picked)
    r.sadd(f"broadcast:{ride.id}:offered_to", *[str(x) for x in picked])
    r.expire(f"broadcast:{ride.id}:offered_to", window_s + 5)
    _emit_offer(ride, picked)

    winner = _wait_for_accept(ride.id, window_s)
    b.ended_at = datetime.utcnow()
    b.outcome = "accepted" if winner else "timeout"
    if winner:
        b.accepted_by_driver_id = winner
    db.session.commit()

    if not winner:
        for did in picked:
            socketio.emit(
                "trip_offer_expired",
                {"ride_id": ride.id},
                namespace="/driver",
                room=f"driver:{did}",
            )
    return winner


def start_office_matching(ride_id: int, pending_fee_ids: list[int] | None = None) -> None:
    app = current_app._get_current_object()
    pending_fee_ids = pending_fee_ids or []

    def _worker():
        with app.app_context():
            try:
                match_office_ride(ride_id, pending_fee_ids=pending_fee_ids)
            except Exception as e:
                app.logger.exception("office matching failed for ride %s: %s", ride_id, e)
                _report_to_sentry(e, ride_id=ride_id, where="match_office_ride")

    eventlet.spawn_n(_worker)


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


def release_queued_for(driver_id: int, *, reason: str) -> int:
    """Release every ride queued for `driver_id` and kick off fresh
    matching for each. Called when the captain's current trip cancels
    (so he'll no longer trigger a hand-off) or when a queued ride's
    lifetime expires. Returns the count released."""
    r = _r()
    rides = (
        Ride.query.filter(
            Ride.queued_for_driver_id == driver_id,
            Ride.status == "queued",
        )
        .all()
    )
    if not rides:
        return 0
    released = 0
    for ride in rides:
        try:
            ride.status = "new"
            ride.queued_for_driver_id = None
            ride.queue_expires_at = None
            ride.driver_id = None
            db.session.commit()
        except Exception as e:  # noqa: BLE001
            db.session.rollback()
            current_app.logger.warning(
                "release_queued_for failed for ride %s: %s", ride.id, e,
            )
            continue
        r.delete(f"driver:{driver_id}:queue_lock")
        current_app.logger.info(
            "released queued ride %s (driver=%s reason=%s)",
            ride.id, driver_id, reason,
        )
        # Silent swap for the customer: fresh matching will announce
        # whichever new captain wins.
        if ride.source == "office":
            start_office_matching(ride.id)
        else:
            start_matching(ride.id)
        released += 1
    return released


def sweep_expired_queued() -> int:
    """Release rides whose queued_for captain never completed his
    current trip inside the queue TTL. Runs alongside the broadcasting
    sweeper. Same leader-lock — only one worker sweeps per tick."""
    now = datetime.utcnow()
    expired = (
        Ride.query.filter(
            Ride.status == "queued",
            Ride.queue_expires_at.isnot(None),
            Ride.queue_expires_at < now,
        )
        .all()
    )
    if not expired:
        return 0
    total = 0
    # release_queued_for is per-driver — group by captain so we don't
    # re-scan the same queue slot twice.
    seen: set[int] = set()
    for ride in expired:
        did = ride.queued_for_driver_id
        if did is None or did in seen:
            continue
        seen.add(did)
        try:
            total += release_queued_for(did, reason="queue_ttl_expired")
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning(
                "sweep_expired_queued failed for driver %s: %s", did, e,
            )
    return total


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
                    sweep_expired_queued()
            except Exception as e:  # noqa: BLE001
                try:
                    app.logger.warning("sweeper loop error: %s", e)
                except Exception:  # noqa: BLE001
                    pass
            eventlet.sleep(SWEEPER_INTERVAL_SECONDS)

    eventlet.spawn_n(_loop)
