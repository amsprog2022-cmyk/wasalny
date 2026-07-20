"""Redis-backed per-phone rate limiting for the Gemini AI pipeline.

Uses fixed-window counters (simpler than sliding-window and good enough
for spam protection). Bucket key expires automatically after the window
so we never accumulate junk in Redis.

Config:
  GEMINI_RATE_LIMIT_PER_HOUR — default 100 (~1.7/min, generous for humans)
"""
from __future__ import annotations

from flask import current_app

from app.extensions import get_redis


def _key(wa_id: str, hour_bucket: int) -> str:
    return f"ai_rl:{wa_id}:{hour_bucket}"


def check_gemini_limit(wa_id: str) -> tuple[bool, int]:
    """Increment the counter for this phone; return (allowed, current_count).

    `allowed = False` means the phone has exceeded the configured
    per-hour limit and the caller should skip Gemini + send a friendly
    Arabic "slow down" reply instead.
    """
    import time
    limit = int(current_app.config.get("GEMINI_RATE_LIMIT_PER_HOUR", 100))
    if limit <= 0:
        return True, 0   # limit disabled

    hour_bucket = int(time.time()) // 3600
    key = _key(wa_id, hour_bucket)

    try:
        r = get_redis(current_app.config.get("REDIS_URL"))
        count = int(r.incr(key))
        if count == 1:
            r.expire(key, 3700)   # slightly more than 1h to survive clock drift
    except Exception:  # noqa: BLE001
        # If Redis is down, fail open (allow the call). Rate limiting is
        # a nice-to-have; we never want to block real customers due to infra.
        current_app.logger.warning("ai rate-limit redis unavailable — fail-open")
        return True, 0

    return count <= limit, count
