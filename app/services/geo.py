"""Small geo helpers. Kept separate so pricing/matching don't reach for
math.* directly and so we can swap in an OSRM road-distance function in
Phase 2.5 without touching call sites."""
from __future__ import annotations

import math


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres between two lat/lng points.

    Straight-line, ignores roads/rivers. Fine for Phase 2 pricing — captain
    can override on arrival for edge cases. Fast: no external calls.
    """
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))
