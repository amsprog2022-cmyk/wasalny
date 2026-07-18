"""Captain discipline logic per Decision #12.

Daily rejections are tracked in Redis (auto-expires at midnight Cairo).
Thresholds are configurable via env — defaults: warn @ 5, suspend @ 10.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import current_app

from app import db
from app.extensions import get_redis
from app.models.driver import Driver


def _r():
    return get_redis(current_app.config.get("REDIS_URL"))


def _warn_threshold() -> int:
    return int(current_app.config.get("CAPTAIN_REJECT_WARN_THRESHOLD", 5))


def _suspend_threshold() -> int:
    return int(current_app.config.get("CAPTAIN_REJECT_SUSPEND_THRESHOLD", 10))


def _suspend_hours() -> int:
    return int(current_app.config.get("CAPTAIN_SUSPEND_HOURS", 24))


def _seconds_until_cairo_midnight() -> int:
    """TTL for the rejection counter — resets at 00:00 Cairo (UTC+2, no DST).

    Cairo doesn't observe DST as of 2016, so we use a fixed +2 offset.
    """
    cairo = timezone(timedelta(hours=2))
    now_cairo = datetime.now(cairo)
    midnight = (now_cairo + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight - now_cairo).total_seconds())


def _key(driver_id: int) -> str:
    return f"driver:{driver_id}:rejections_today"


def rejections_today(driver_id: int) -> int:
    val = _r().get(_key(driver_id))
    return int(val) if val else 0


def register_rejection(driver_id: int) -> dict:
    """Increment the daily rejection counter and apply discipline if thresholds crossed.

    Returns a summary dict the captain app can render immediately.
    """
    r = _r()
    key = _key(driver_id)
    n = r.incr(key)
    if n == 1:
        # First rejection today — set TTL so we reset at Cairo midnight
        r.expire(key, _seconds_until_cairo_midnight())

    n = int(n)
    warn_at = _warn_threshold()
    suspend_at = _suspend_threshold()

    action = "none"
    driver = db.session.get(Driver, driver_id)
    if driver is not None:
        if n >= suspend_at:
            driver.discipline_status = "suspended"
            driver.suspended_until = datetime.utcnow() + timedelta(hours=_suspend_hours())
            action = "suspended"
        elif n >= warn_at and driver.discipline_status == "active":
            driver.discipline_status = "warned"
            action = "warned"
        db.session.commit()

    return {
        "rejections_today": n,
        "warn_threshold": warn_at,
        "suspend_threshold": suspend_at,
        "action": action,
        "discipline_status": driver.discipline_status if driver else "active",
        "suspended_until": (
            driver.suspended_until.isoformat() if driver and driver.suspended_until else None
        ),
    }


def get_state(driver_id: int) -> dict:
    """Read-only summary of the captain's discipline state for the banner UI."""
    n = rejections_today(driver_id)
    warn_at = _warn_threshold()
    suspend_at = _suspend_threshold()
    driver = db.session.get(Driver, driver_id)
    return {
        "rejections_today": n,
        "warn_threshold": warn_at,
        "suspend_threshold": suspend_at,
        "remaining_before_warning": max(0, warn_at - n),
        "remaining_before_suspend": max(0, suspend_at - n),
        "discipline_status": driver.discipline_status if driver else "active",
        "suspended_until": (
            driver.suspended_until.isoformat() if driver and driver.suspended_until else None
        ),
    }
