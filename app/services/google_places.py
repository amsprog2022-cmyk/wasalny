"""Thin wrapper around Google Places API (New) v1 Text Search.

Nominatim's Arabic coverage in Benha is thin — customers type "كلية علوم"
and get a wrong hit or none at all, but Google Places nails it because it
crowdsources place names from millions of users. Everything that used to
call `reverse_geocode.search_places` now tries Google first and falls
back to Nominatim on any failure (missing API key, HTTP error, empty
response, quota exhausted).

Costs at Wassalny's scale: Text Search bills at $0.017 per request. A
few hundred rides per day → ~$5-15/month, capped by Google's per-SKU
daily quota which you set in Cloud Console.
"""
from __future__ import annotations

from typing import Optional

import requests
from flask import current_app

# Benha centre. Same coord used elsewhere in the codebase as the
# location-bias anchor. 20 km radius covers all of the greater
# Benha region + surrounding villages the office serves.
_BENHA_LAT = 30.4650
_BENHA_LNG = 31.1836
_BIAS_RADIUS_M = 20000

_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
# Only the fields we actually use — cuts the response payload + billing
# tier (Basic Data is cheapest; adding phone/opening_hours would jump us
# to Contact Data pricing).
_FIELD_MASK = "places.displayName,places.location,places.formattedAddress"


def search_places(query: str, limit: int = 5) -> Optional[list[dict]]:
    """Forward-geocode free text via Google Places Text Search.

    Returns [{"label": str, "lat": float, "lng": float}, ...] on success,
    or None on any failure — callers treat None as "try the fallback".
    An empty list [] is a real "no results" answer and is returned as-is
    so callers can distinguish "Google says nothing exists" from
    "Google didn't answer".
    """
    key = current_app.config.get("GOOGLE_MAPS_SERVER_KEY", "")
    if not key or not query or not query.strip():
        return None

    try:
        resp = requests.post(
            _ENDPOINT,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": _FIELD_MASK,
            },
            json={
                "textQuery": query.strip(),
                # Arabic labels come back preferred when languageCode is
                # set — otherwise Google may return the English name.
                "languageCode": "ar",
                "regionCode": "EG",
                "maxResultCount": max(1, min(limit, 20)),
                "locationBias": {
                    "circle": {
                        "center": {"latitude": _BENHA_LAT, "longitude": _BENHA_LNG},
                        "radius": _BIAS_RADIUS_M,
                    },
                },
            },
            timeout=6,
        )
    except requests.RequestException as e:
        current_app.logger.warning("google places request failed: %s", e)
        return None

    if resp.status_code != 200:
        # 4xx usually means bad key / Places API not enabled — log the
        # body once so the operator can fix it in Google Cloud Console.
        current_app.logger.warning(
            "google places %s: %s",
            resp.status_code, resp.text[:200],
        )
        return None

    try:
        payload = resp.json() or {}
    except ValueError:
        return None

    hits = payload.get("places") or []
    results: list[dict] = []
    for h in hits:
        loc = h.get("location") or {}
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            continue
        # Prefer the local display name; fall back to formatted address
        # when displayName isn't set (rare for POIs, common for streets).
        display = (h.get("displayName") or {}).get("text")
        label = display or h.get("formattedAddress") or ""
        results.append({
            "label": label,
            "lat": float(lat),
            "lng": float(lng),
        })
    return results
