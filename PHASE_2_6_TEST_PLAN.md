# Phase 2.6 — Test & Audit Plan (handoff)

All code below is ALREADY WRITTEN and uncommitted in three repos. Your job:
verify, commit, push, then run the audit checklist. Do NOT rewrite features.

## Repos & versions
| Repo | Path | Version | Push target |
|---|---|---|---|
| Backend | `wassalny/` | — | `github.com/amsprog2022-cmyk/wasalny.git` (Railway autodeploys on push) |
| Captain app | `wassalny/mobile/captain_app` | `1.2.4+21` | its own GitHub repo (Xcode Cloud builds on push) |
| Customer app | `wassalny/mobile/customer_app` | `1.2.2+16` | its own GitHub repo (Xcode Cloud builds on push) |

## What changed (context for review)

### Backend
- `app/api/rides_api.py`
  - `POST /rides/<id>/accept`: on claim, waits ≤3s (15×0.2s eventlet sleeps +
    `db.session.expire`) for status→assigned, returns `{"claimed": true, "ride": {...}}`
    (payload forces status `assigned` if still flipping).
  - `GET /rides/search-places?q=` (customer JWT): forward geocode, 400
    `query_too_short` if q < 3 chars.
  - `GET /driver/active-ride` offer branch: adds `offer_expires_in` =
    min(Redis TTL of `broadcast:{id}:offered_to`, `BROADCAST_ACCEPT_WINDOW_SECONDS`).
- `app/services/reverse_geocode.py`: `search_places(query, limit=6)` — Nominatim
  `/search`, viewbox `30.90,30.60,31.60,29.90` (bias not filter, `bounded=0`),
  `countrycodes=eg`, `accept-language=ar`, Redis cache 24h key `geo:search:{q}`.
- `app/sockets/driver_socket.py` `on_driver_position`:
  - replaced per-ping `Ride.query.filter(status IN ...)` with Redis
    `driver:{id}:current_ride` GET + `db.session.get(Ride, pk)` (guarded:
    driver_id match + status in assigned/started).
  - new relay: emits `captain_position {ride_id, lat, lng}` to room
    `customer:{customer_id}` on `/customer` when on a trip.
- `app/services/ride_lifecycle.py` `assign()`: sets `driver:{id}:current_ride`
  with 6h TTL (was only 15s from try_claim → double-booking window). Lock already
  deleted at complete/cancel/no_show — verify that's still true.
- `app/services/availability.py` `count_available_in_zone`: rewritten to ZCARD
  (was O(all drivers) hash scan per zone). Callers: debug_api.py:737,
  routes/zones.py:27, matching.py:141.

### Captain app
- `lib/services/rides_service.dart`: `accept()` → `AcceptResult{claimed, ride?}`;
  409 → `claimed:false`; `ActiveRideResult.offerExpiresIn` parsed.
- `lib/screens/trip_offer_screen.dart`: `_accept()` uses `result.ride ??
  ride.copyWith(status:'assigned')`; catch shows error snackbar; countdown seeds
  from `offered.offerExpiresIn ?? 30`.
- `lib/screens/splash_screen.dart` + `home_screen.dart`: `setOffered(ride,
  result.offerExpiresIn ?? 10)`.
- `lib/screens/trip_in_progress_screen.dart`: `_statusLabel` handles
  `broadcasting` ('في انتظار التأكيد'); `_selfHealIfStale()` on first frame —
  if active ride status==broadcasting, re-fetch active-ride and route
  (null→Home, offer→TripOfferScreen, else setActive).

### Customer app
- `lib/state/active_ride_provider.dart`: `captainLat/captainLng` from
  `captain_position` socket event (ride_id-guarded); `notifiedTerminalRideId`
  once-per-ride terminal guard — `_mergeRideFromEvent` / `setRide` / `refresh`
  all refuse to resurrect a notified terminal ride; `clear()` preserves marker.
- `lib/screens/trip_in_progress_screen.dart` + `searching_screen.dart`:
  terminal snackbar only on status *transition* + id != notifiedTerminalRideId;
  `clearSnackBars()` first; 3s duration. Trip screen has `_liveMap` flutter_map
  panel (OSM tiles, green pickup pin, orange taxi marker, camera auto-refits
  via `_liveMapController.fitCamera` when captain moves).
- `lib/screens/destination_search_screen.dart` (NEW): debounced (400ms, min 3
  chars) search UI, stale-response guard, pops `PlaceResult`.
- `lib/screens/booking_screen.dart`: top bar → `_openDestinationSearch()`;
  result sets `_destination` + `_destinationLabel` and moves map (no
  reverse-geocode round-trip).
- `lib/services/rides_service.dart`: `searchPlaces()` + `PlaceResult`.

## Verification checklist (run in order)

1. **Backend boot smoke**
   `cd wassalny && .venv/bin/python -c "from app import create_app; create_app(); print('BOOT_OK')"`
2. **Static analysis** — must be clean:
   - `cd mobile/captain_app && flutter analyze`
   - `cd mobile/customer_app && flutter analyze`
3. **Search endpoint** (after deploy or against local run):
   `curl '.../api/v1/rides/search-places?q=شارع النصر' -H 'Authorization: Bearer <customer JWT>'`
   → JSON list of `{label, lat, lng}`; repeat call should hit Redis cache.
   Also: q of 2 chars → 400 `query_too_short`.
4. **Commit + push all three repos** (only after 1–2 pass). Backend push
   triggers Railway deploy — watch the deploy log for boot errors.
5. **Device scenarios**
   a. Book ride → captain accepts → captain screen immediately shows
      'روح للعميل' with arrived/start buttons — NEVER raw "broadcasting".
   b. Kill captain app mid-offer → reopen → offer screen with correct
      remaining countdown (≈10s window, not 30).
   c. Customer cancels → exactly ONE 3s 'اتلغت الرحلة' snackbar; navigate to
      booking/home → no leftover banner; background+resume → still no repeat.
   d. Customer types destination → picks result → red pin lands there →
      quote → book (tap-on-map flow must still work too).
   e. During assigned ride, customer trip screen shows the orange taxi marker
      moving as the captain streams GPS; marker stays in frame (auto-refit).
6. **Concurrency spot-checks** (scale audit follow-through)
   - Captain on an active trip must NOT receive new offers (6h
     `driver:{id}:current_ride` lock; try a second booking nearby).
   - After complete/cancel, same captain DOES receive offers again
     (lock cleanup).
   - `zone_counts` / zones endpoints still return correct numbers (ZCARD path).

## Known deliberate scope cuts (do not "fix")
- Accept window stays 10s; no re-broadcast rounds (user deferred).
- Search is biased (not restricted) to Qalyubia + Greater Cairo — results
  outside the viewbox are allowed.
- Customer map panel is non-interactive by design.
