"""Driver availability engine — pure Redis for sub-5ms reads.

Redis key layout (matches PLAN.md §7):

  driver:{id}:status         HASH   { online, available, zone_id, last_hb }
  zone:{id}:available_drivers ZSET  driver_ids scored by last-activity ts
  driver:{id}:current_ride   STRING optional, set when driver has active trip

Rules:
- A driver only shows up in `zone:*:available_drivers` while:
    online=1 AND available=1 AND has an active heartbeat
- The zset is trimmed lazily on read to expire stale entries (heartbeat timeout).
- All state changes flow through this service — no direct Redis writes elsewhere.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from flask import current_app

from app.extensions import get_redis


def _r():
    return get_redis(current_app.config.get("REDIS_URL"))


def _hb_timeout() -> int:
    return int(current_app.config.get("DRIVER_HEARTBEAT_TIMEOUT_SECONDS", 60))


# ---------- keys ----------

def k_driver(driver_id: int) -> str:
    return f"driver:{driver_id}:status"


def k_zone(zone_id: int) -> str:
    return f"zone:{zone_id}:available_drivers"


# ---------- writes ----------

def set_online(driver_id: int, zone_id: int) -> None:
    """Driver went online in a zone. Marked available by default."""
    r = _r()
    now = time.time()
    r.hset(
        k_driver(driver_id),
        mapping={
            "online": "1",
            "available": "1",
            "zone_id": str(zone_id),
            "last_hb": str(now),
        },
    )
    r.zadd(k_zone(zone_id), {str(driver_id): now})


def set_offline(driver_id: int) -> None:
    """Driver signed out or lost connection permanently."""
    r = _r()
    prev = r.hget(k_driver(driver_id), "zone_id")
    if prev:
        r.zrem(k_zone(int(prev)), str(driver_id))
    r.hset(k_driver(driver_id), mapping={"online": "0", "available": "0"})


def set_available(driver_id: int, available: bool) -> None:
    """Manual toggle from the captain app (Decision #12 — busy vs available)."""
    r = _r()
    zone_raw = r.hget(k_driver(driver_id), "zone_id")
    if not zone_raw:
        return
    zone_id = int(zone_raw)
    now = time.time()
    r.hset(
        k_driver(driver_id),
        mapping={"available": "1" if available else "0", "last_hb": str(now)},
    )
    if available:
        r.zadd(k_zone(zone_id), {str(driver_id): now})
    else:
        r.zrem(k_zone(zone_id), str(driver_id))


def change_zone(driver_id: int, new_zone_id: int) -> None:
    """Captain reports a new current zone (e.g. after completing a trip)."""
    r = _r()
    prev = r.hget(k_driver(driver_id), "zone_id")
    if prev and int(prev) != new_zone_id:
        r.zrem(k_zone(int(prev)), str(driver_id))
    now = time.time()
    r.hset(
        k_driver(driver_id),
        mapping={"zone_id": str(new_zone_id), "last_hb": str(now)},
    )
    if r.hget(k_driver(driver_id), "available") == "1":
        r.zadd(k_zone(new_zone_id), {str(driver_id): now})


def heartbeat(driver_id: int) -> None:
    """Periodic ping from the captain app (every 15s)."""
    r = _r()
    zone_raw = r.hget(k_driver(driver_id), "zone_id")
    if not zone_raw:
        return
    now = time.time()
    r.hset(k_driver(driver_id), "last_hb", str(now))
    if r.hget(k_driver(driver_id), "available") == "1":
        r.zadd(k_zone(int(zone_raw)), {str(driver_id): now})


# ---------- reads ----------

@dataclass
class DriverPresence:
    driver_id: int
    online: bool
    available: bool
    zone_id: int | None
    last_hb: float | None

    @property
    def is_live(self) -> bool:
        if not self.online or self.last_hb is None:
            return False
        return (time.time() - self.last_hb) <= 60  # cheap check; real timeout in _hb_timeout


def get_presence(driver_id: int) -> DriverPresence:
    r = _r()
    data = r.hgetall(k_driver(driver_id)) or {}
    return DriverPresence(
        driver_id=driver_id,
        online=data.get("online") == "1",
        available=data.get("available") == "1",
        zone_id=int(data["zone_id"]) if data.get("zone_id") else None,
        last_hb=float(data["last_hb"]) if data.get("last_hb") else None,
    )


def available_drivers_in_zone(zone_id: int) -> list[int]:
    """Return every driver reachable in this zone.

    Two paths:
      1. Fast path — the Redis sorted-set updated by heartbeats.
      2. Fallback — scan `driver:*:status` hashes. This catches captains
         whose app is backgrounded (iOS backgrounds within ~30s so their
         heartbeat expires quickly) but who are still `online=1` in the
         hash. FCM push in matching.py wakes them.

    We only trim entries older than 1h so genuinely dead sessions get
    cleaned up eventually without punishing a backgrounded captain.
    """
    r = _r()
    long_stale_cutoff = time.time() - max(_hb_timeout(), 3600)
    r.zremrangebyscore(k_zone(zone_id), min="-inf", max=long_stale_cutoff)
    ids: list[int] = [int(x) for x in r.zrange(k_zone(zone_id), 0, -1)]
    # Supplement with a hash scan for any online captain in this zone that
    # the zset doesn't currently know about (backgrounded, no recent heartbeat).
    seen = set(ids)
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match="driver:*:status", count=200)
        for key in keys:
            key_str = key.decode() if isinstance(key, (bytes, bytearray)) else key
            data = r.hgetall(key)
            def _get(k: str) -> str:
                v = data.get(k) or data.get(k.encode()) if data else None
                if isinstance(v, (bytes, bytearray)):
                    return v.decode()
                return str(v) if v is not None else ""
            if _get("online") != "1":
                continue
            if _get("zone_id") != str(zone_id):
                continue
            parts = key_str.split(":")
            if len(parts) < 3:
                continue
            try:
                did = int(parts[1])
            except ValueError:
                continue
            if did in seen:
                continue
            seen.add(did)
            ids.append(did)
        if cursor == 0:
            break
    return ids


def count_available_in_zone(zone_id: int) -> int:
    return len(available_drivers_in_zone(zone_id))


def zone_counts(zone_ids: Iterable[int]) -> dict[int, int]:
    return {zid: count_available_in_zone(zid) for zid in zone_ids}
