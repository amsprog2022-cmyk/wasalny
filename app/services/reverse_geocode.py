"""Resolve GPS coordinates to a Wassalny Zone via Nominatim.

Nominatim (nominatim.openstreetmap.org) is OSM's free reverse-geocoder.
Free tier is 1 req/sec. Our captain fleet goes online ~once per shift,
so we're comfortably inside the limit — plus we cache resolved zones
in Redis with a 24h TTL so a captain who logs in twice in the same
day doesn't burn quota.

Fuzzy match strategy:
  1. Nominatim returns an `address` dict with keys like `neighbourhood`,
     `suburb`, `village`, `town`, `city_district`, `city`.
  2. We look at each of those in order of specificity.
  3. For each candidate name, normalize (أ/إ/آ→ا, ة→ه, ى→ي, no diacritics)
     and compare against every active Zone.name_ar.
  4. Return the FIRST match (or None if nothing matches).
  5. Callers should fall back to a default zone when this returns None.
"""
from __future__ import annotations

import json
import time
import unicodedata
from typing import Optional

import requests
from flask import current_app

from app import db
from app.extensions import get_redis
from app.models.zone import Zone


NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "Wassalny-Benha/1.0 (contact@wassalny.com)"

# Cache resolved coordinates for a day; a captain's zone doesn't change
# meaningfully every 10m. Bucket to 3-decimal-places (~110m grid) so nearby
# lookups hit the same cache entry.
CACHE_TTL_SECONDS = 24 * 3600


def _cache_key(lat: float, lng: float) -> str:
    return f"geo:zone:{lat:.3f},{lng:.3f}"


def _normalize(text: str) -> str:
    """Mirror the Arabic-normalization we use in the app zone picker."""
    if not text:
        return ""
    out = []
    for ch in unicodedata.normalize("NFC", text):
        cp = ord(ch)
        if cp in (0x0623, 0x0625, 0x0622, 0x0671):   # أ إ آ ٱ
            out.append("ا")
        elif cp == 0x0629:                             # ة
            out.append("ه")
        elif cp == 0x0649:                             # ى
            out.append("ي")
        elif cp in (0x064B, 0x064C, 0x064D, 0x064E, 0x064F, 0x0650, 0x0651, 0x0652, 0x0653, 0x0640):
            # skip tashkeel + tatweel
            continue
        else:
            out.append(ch)
    return "".join(out).lower().strip()


def _nominatim_reverse(lat: float, lng: float) -> dict | None:
    """Call Nominatim. Returns the raw JSON or None on any failure."""
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "accept-language": "ar",
                "zoom": 16,   # neighbourhood-level detail
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=6,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _extract_candidate_names(nominatim_response: dict) -> list[str]:
    """Pull the neighbourhood/suburb/village names in most-specific-first order."""
    addr = (nominatim_response or {}).get("address") or {}
    return [
        addr.get("neighbourhood"),
        addr.get("quarter"),
        addr.get("suburb"),
        addr.get("village"),
        addr.get("hamlet"),
        addr.get("town"),
        addr.get("city_district"),
        addr.get("city"),
    ]


def resolve_zone(lat: float, lng: float) -> Optional[Zone]:
    """Reverse-geocode + fuzzy-match to a Wassalny Zone. Returns a Zone or None.

    Cached in Redis for 24h at 3-decimal grid granularity.
    """
    r = get_redis(current_app.config.get("REDIS_URL"))
    key = _cache_key(lat, lng)

    # Cache hit
    cached = r.get(key)
    if cached is not None:
        # Redis returns strings when decode_responses=True (our default).
        raw = cached if isinstance(cached, str) else cached.decode("utf-8", "ignore")
        try:
            payload = json.loads(raw)
            zone_id = payload.get("zone_id")
            if zone_id:
                zone = db.session.get(Zone, int(zone_id))
                if zone is not None and zone.is_active:
                    return zone
        except (ValueError, TypeError):
            pass

    # Call Nominatim
    resp = _nominatim_reverse(lat, lng)
    candidates = [c for c in _extract_candidate_names(resp or {}) if c]

    # Fuzzy match against active zones — bidirectional substring on normalized text.
    matched_zone: Zone | None = None
    if candidates:
        active_zones = Zone.query.filter_by(is_active=True).all()
        norm_zones = [(_normalize(z.name_ar), z) for z in active_zones]
        for candidate in candidates:
            cand_norm = _normalize(candidate)
            if not cand_norm:
                continue
            for name_norm, zone in norm_zones:
                if not name_norm:
                    continue
                # Match either direction so "الرملة" matches "رملة"-in-response
                # and vice versa.
                if cand_norm in name_norm or name_norm in cand_norm:
                    matched_zone = zone
                    break
            if matched_zone:
                break

    # Cache the resolution so a re-tap doesn't hit Nominatim again.
    try:
        payload = {
            "zone_id": (matched_zone.id if matched_zone else None),
            "candidates": candidates,
            "ts": int(time.time()),
        }
        r.setex(key, CACHE_TTL_SECONDS, json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("reverse_geocode cache write failed: %s", e)

    return matched_zone


def default_zone() -> Optional[Zone]:
    """Fallback when reverse-geocode returns nothing usable. Uses the first
    active zone by id — deterministic, easy to override later via config."""
    slug = current_app.config.get("DEFAULT_ZONE_SLUG", "downtown")
    z = Zone.query.filter_by(slug=slug, is_active=True).first()
    if z is not None:
        return z
    return Zone.query.filter_by(is_active=True).order_by(Zone.id.asc()).first()
