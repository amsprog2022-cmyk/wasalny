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

import eventlet
import requests
from flask import current_app

from app import db
from app.extensions import get_redis
from app.models.zone import Zone


# Nominatim public policy is 1 req/sec. Under a burst (e.g. 20 office
# pastes arriving in 5s each firing 2-4 lookups), a naive fan-out would
# get us rate-limited or IP-banned. Cap concurrent in-flight calls with
# an eventlet semaphore — greenlets wait their turn instead of hammering.
# 3 is generous while still keeping us well under the fair-use ceiling.
_NOMINATIM_SEMAPHORE = eventlet.semaphore.Semaphore(3)
from app.services.geo import haversine_km


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
    with _NOMINATIM_SEMAPHORE:
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


def resolve_place_name(lat: float, lng: float) -> Optional[str]:
    """Return a short Arabic display string like 'شارع 45، الرملة، بنها' for
    a customer-app pin. Reuses the same 24h Redis cache as `resolve_zone`
    so drag interactions don't burn the free tier.

    Returns None if Nominatim gave us nothing usable.
    """
    r = get_redis(current_app.config.get("REDIS_URL"))
    label_key = f"geo:label:{lat:.3f},{lng:.3f}"
    cached = r.get(label_key)
    if cached is not None:
        raw = cached if isinstance(cached, str) else cached.decode("utf-8", "ignore")
        # Empty string = "we already tried and got nothing" — cache negative too.
        return raw or None

    resp = _nominatim_reverse(lat, lng)
    if not resp:
        try:
            r.setex(label_key, CACHE_TTL_SECONDS, "")
        except Exception:  # noqa: BLE001
            pass
        return None

    candidates = [c for c in _extract_candidate_names(resp) if c]
    # Prefer Nominatim's own `display_name` (already comma-separated, most
    # specific first). Truncate to first 3 tokens so we don't render a
    # 10-part address.
    display = (resp.get("display_name") or "").strip()
    if display:
        label = "، ".join([p.strip() for p in display.split(",")[:3] if p.strip()])
    else:
        label = "، ".join(candidates[:3]) if candidates else ""

    try:
        r.setex(label_key, CACHE_TTL_SECONDS, label)
    except Exception:  # noqa: BLE001
        pass
    return label or None


NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# Bias box covering Qalyubia + Greater Cairo (left,top,right,bottom lng/lat).
# bounded=0 means "prefer results here" rather than "only results here", so
# a trip from Shubra El Kheima or an occasional Cairo destination still works.
SEARCH_VIEWBOX = "30.90,30.60,31.60,29.90"

# Benha and the villages around it, ~20km across. Searched with bounded=1
# first: this box is the only reason a query like "الاهرام" offers شارع
# الاهرام in Benha before the Giza pyramids, which outrank it globally on
# every measure Nominatim has.
LOCAL_VIEWBOX = "30.96,30.67,31.40,30.27"

# Benha town centre. Local hits are ordered by distance from here, since
# the nearest match is almost always the one a Benha customer meant.
BENHA_CENTRE_LAT, BENHA_CENTRE_LNG = 30.4667, 31.1833


# Landmark-ish Nominatim classes we treat as a confident pickup hit.
# Wide categories like `place` (city/town/suburb) are excluded because
# they resolve to a huge bounding box — matcher would pick a captain
# 2km off from where the customer actually is.
_CONFIDENT_PICKUP_CLASSES = {
    "amenity",
    "tourism",
    "railway",
    "aeroway",
    "historic",
    "office",
    "shop",
    "building",
    "leisure",
    "healthcare",
}

# ~500m in decimal degrees at Egypt's latitude — anything tighter than
# this is a small enough bbox that we trust the coord for pickup matching.
_CONFIDENT_BBOX_WIDTH_DEG = 0.005


def _nominatim_query(q: str, viewbox: str, bounded: int, limit: int) -> list[dict]:
    results: list[dict] = []
    with _NOMINATIM_SEMAPHORE:
        try:
            resp = requests.get(
                NOMINATIM_SEARCH_URL,
                params={
                    "q": q,
                    "format": "jsonv2",
                    "accept-language": "ar",
                    "countrycodes": "eg",
                    "viewbox": viewbox,
                    "bounded": bounded,
                    "limit": limit,
                    "addressdetails": 0,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=6,
            )
            if resp.status_code == 200:
                for item in resp.json():
                    try:
                        lat = float(item["lat"])
                        lng = float(item["lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    display = (item.get("display_name") or "").strip()
                    label = "، ".join(
                        [p.strip() for p in display.split(",")[:3] if p.strip()]
                    )
                    if not label:
                        continue
                    # bbox = [south_lat, north_lat, west_lng, east_lng]
                    bbox = item.get("boundingbox") or []
                    bbox_width: float | None = None
                    try:
                        if len(bbox) == 4:
                            bbox_width = abs(float(bbox[3]) - float(bbox[2]))
                    except (TypeError, ValueError):
                        bbox_width = None
                    results.append({
                        "label": label,
                        "lat": lat,
                        "lng": lng,
                        "class": item.get("class"),
                        "type": item.get("type"),
                        "bbox_width": bbox_width,
                    })
        except (requests.RequestException, ValueError):
            return []
    return results


def _nominatim_search_raw(query: str, limit: int = 6) -> list[dict]:
    """Fetch raw Nominatim /search results (including class + bbox), Benha
    first, cached 24h.

    Two passes. The first is locked to the Benha box, so a name that also
    exists somewhere famous resolves locally instead of sending the customer
    to Giza. The wider Qalyubia + Cairo pass only runs when the local one
    came up short, which keeps most searches at one request against
    Nominatim's 1/sec free tier.
    """
    q = (query or "").strip()
    if len(q) < 3:
        return []

    r = get_redis(current_app.config.get("REDIS_URL"))
    # v2: entries cached before the local-first pass carry the old ordering.
    cache_key = f"geo:search_raw:v2:{_normalize(q)}"
    cached = r.get(cache_key)
    if cached is not None:
        raw = cached if isinstance(cached, str) else cached.decode("utf-8", "ignore")
        try:
            return json.loads(raw)
        except ValueError:
            pass

    local = _nominatim_query(q, LOCAL_VIEWBOX, bounded=1, limit=limit)
    local.sort(key=lambda x: haversine_km(
        BENHA_CENTRE_LAT, BENHA_CENTRE_LNG, x["lat"], x["lng"]))

    results = local
    if len(results) < limit:
        seen = {(round(x["lat"], 4), round(x["lng"], 4)) for x in results}
        for item in _nominatim_query(q, SEARCH_VIEWBOX, bounded=0, limit=limit):
            key = (round(item["lat"], 4), round(item["lng"], 4))
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= limit:
                break

    try:
        r.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(results, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass
    return results


def search_places(query: str, limit: int = 6) -> list[dict]:
    """Forward-geocode free text into candidate destinations.

    Returns [{"label": str, "lat": float, "lng": float}, ...] biased to
    Qalyubia + Greater Cairo, Arabic labels. Backed by the same 24h cache
    used for pickup-confidence scoring.
    """
    return [
        {"label": r["label"], "lat": r["lat"], "lng": r["lng"]}
        for r in _nominatim_search_raw(query, limit=limit)
    ]


def geocode_pickup(text: str) -> Optional[dict]:
    """Forward-geocode a free-text pickup for the WhatsApp AI booking flow.

    Returns {"lat", "lng", "label", "confident": bool} or None if Nominatim
    yielded nothing. A hit is `confident` when we're OK feeding it into the
    GPS matcher without asking the customer to send a WhatsApp 📍 pin:

    - only one candidate returned (no ambiguity), OR
    - top result's OSM class is a landmark-ish category (hospital, station,
      university, etc.), OR
    - the bounding box is tight (<~500m across).

    Callers should fall back to asking for a location pin when confident=False.
    """
    results = _nominatim_search_raw(text, limit=3)
    if not results:
        return None
    top = results[0]
    is_landmark = (top.get("class") in _CONFIDENT_PICKUP_CLASSES)
    tight_bbox = (
        top.get("bbox_width") is not None
        and top["bbox_width"] < _CONFIDENT_BBOX_WIDTH_DEG
    )
    confident = (len(results) == 1) or is_landmark or tight_bbox
    return {
        "lat": top["lat"],
        "lng": top["lng"],
        "label": top["label"],
        "confident": confident,
    }
