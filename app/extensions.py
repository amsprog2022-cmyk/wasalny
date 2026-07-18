"""Shared singletons that live outside the Flask app factory.

Kept small so both the web process and RQ workers can import them.
"""
from __future__ import annotations

import redis
import fakeredis


_redis_client: redis.Redis | None = None


def get_redis(url: str | None = None) -> redis.Redis:
    """Return a process-wide Redis client.

    If REDIS_URL is empty, we fall back to fakeredis so `python wsgi.py`
    just works on a fresh laptop without spinning up a real Redis.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    if url:
        _redis_client = redis.Redis.from_url(url, decode_responses=True)
    else:
        _redis_client = fakeredis.FakeRedis(decode_responses=True)
    return _redis_client
