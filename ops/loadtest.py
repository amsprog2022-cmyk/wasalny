"""Concurrency load test for the matching engine.

Simulates N customers booking simultaneously against M captains and asserts:
  - Every ride ends in one of the terminal states (assigned/completed/cancelled).
  - No captain is assigned to two rides at the same time — the atomic
    reservation invariant from PLAN §11.

Usage:
  BASE=http://localhost:5000 python -m ops.loadtest --customers 20 --captains 10
"""
from __future__ import annotations

import argparse
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


BASE = os.getenv("BASE", "http://localhost:5000")


def seed_captains(n: int) -> list[dict]:
    """Create N captains + one customer in the DB, log everyone in, bring captains online."""
    from app import create_app, db
    from app.models.driver import Driver
    from app.models.zone import Zone

    app = create_app()
    with app.app_context():
        zones = Zone.query.filter_by(is_active=True).all()
        if not zones:
            raise SystemExit("run: python -m ops.seed_zones first")
        for i in range(n):
            wa = f"20100000{i:04d}"
            if not Driver.query.filter_by(wa_id=wa).first():
                d = Driver(
                    wa_id=wa,
                    name=f"Captain {i}",
                    car_model="Toyota",
                    car_plate=f"CAP-{i:04d}",
                    car_color="silver",
                )
                d.set_password("test")
                db.session.add(d)
        db.session.commit()
        return [{"wa_id": f"20100000{i:04d}", "zone_id": random.choice(zones).id} for i in range(n)]


def login_captain(wa_id: str) -> str:
    r = requests.post(f"{BASE}/api/v1/auth/driver/login", json={"wa_id": wa_id})
    r.raise_for_status()
    return r.json()["access_token"]


def login_customer(wa_id: str) -> str:
    r = requests.post(f"{BASE}/api/v1/customer/login", json={"wa_id": wa_id, "name": f"cust {wa_id[-4:]}"})
    r.raise_for_status()
    return r.json()["access_token"]


def bring_online(token: str, zone_id: int) -> None:
    requests.post(
        f"{BASE}/api/v1/driver/availability",
        headers={"Authorization": f"Bearer {token}"},
        json={"action": "online", "zone_id": zone_id},
    ).raise_for_status()


def book_ride(token: str, from_zone: int, to_zone: int) -> dict:
    r = requests.post(
        f"{BASE}/api/v1/rides",
        headers={"Authorization": f"Bearer {token}"},
        json={"from_zone_id": from_zone, "to_zone_id": to_zone},
    )
    if r.status_code == 429:
        return {"error": "rate_limited"}
    r.raise_for_status()
    return r.json()


def get_ride(token: str, ride_id: int) -> dict:
    r = requests.get(
        f"{BASE}/api/v1/rides/{ride_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()


def captain_accept_loop(token: str, ride_ids: list[int], stop_after: float) -> list[int]:
    """A captain polls each broadcasting ride and tries to accept.

    Returns as soon as this captain has won 1 ride (real captains can't hold two).
    Drops rides only when they're terminal (already_taken) — retries `not_broadcasting`.
    """
    won = []
    deadline = time.time() + stop_after
    while time.time() < deadline and ride_ids:
        for rid in list(ride_ids):
            try:
                r = requests.post(
                    f"{BASE}/api/v1/rides/{rid}/accept",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=3,
                )
                if r.status_code == 200 and r.json().get("claimed"):
                    won.append(rid)
                    return won
                if r.status_code == 409:
                    err = (r.json() or {}).get("error", "")
                    if err in ("already_taken",):
                        ride_ids.remove(rid)
                    # else: not_broadcasting → retry after backoff
            except Exception:
                pass
        time.sleep(0.05)
    return won


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customers", type=int, default=20)
    parser.add_argument("--captains", type=int, default=10)
    args = parser.parse_args()

    print(f"seeding {args.captains} captains…")
    captains_meta = seed_captains(args.captains)

    print("logging captains in and bringing them online…")
    captains = []
    for c in captains_meta:
        tok = login_captain(c["wa_id"])
        bring_online(tok, c["zone_id"])
        captains.append({"token": tok, "zone_id": c["zone_id"]})

    print(f"logging {args.customers} customers in…")
    customer_tokens = [login_customer(f"20155500{i:04d}") for i in range(args.customers)]

    print("booking rides concurrently…")
    # Each customer picks a random pickup/dropoff zone from the captains' zones
    zones = list({c["zone_id"] for c in captains})
    if len(zones) < 2:
        zones.append(zones[0])
    t0 = time.time()
    rides = []
    with ThreadPoolExecutor(max_workers=args.customers) as ex:
        futs = [
            ex.submit(book_ride, tok, random.choice(zones), random.choice(zones))
            for tok in customer_tokens
        ]
        for f in as_completed(futs):
            r = f.result()
            if "error" not in r:
                rides.append(r)
    print(f"  {len(rides)} rides created in {time.time() - t0:.2f}s")

    ride_ids = [r["id"] for r in rides]

    print("captains racing to accept…")
    all_accepted: list[int] = []
    with ThreadPoolExecutor(max_workers=len(captains)) as ex:
        futs = [ex.submit(captain_accept_loop, c["token"], list(ride_ids), 12.0) for c in captains]
        for f in as_completed(futs):
            all_accepted.extend(f.result())

    # Matching runs up to 3 rounds × 10s. Wait for all cancellations to settle.
    print("waiting for non-matched rides to auto-cancel (up to 35s)…")
    time.sleep(35)

    print("verifying final states…")
    from app import create_app
    from app.models.ride import Ride
    app = create_app()
    with app.app_context():
        finals = Ride.query.filter(Ride.id.in_(ride_ids)).all()
        by_status: dict[str, int] = {}
        driver_assignments: dict[int, list[int]] = {}
        for r in finals:
            by_status[r.status] = by_status.get(r.status, 0) + 1
            if r.driver_id:
                driver_assignments.setdefault(r.driver_id, []).append(r.id)

        print("  final status distribution:")
        for k, v in sorted(by_status.items()):
            print(f"    {k:>20}: {v}")

        # Invariant #1: no driver assigned to >1 concurrently-active ride
        # (they can have multiple rides across their lifetime, but for THIS batch
        # a captain should hold at most 1 assigned/started ride at a time)
        overloaded = []
        for did, rids in driver_assignments.items():
            active = [r for r in finals if r.driver_id == did and r.status in ("assigned", "started")]
            if len(active) > 1:
                overloaded.append((did, [r.id for r in active]))
        if overloaded:
            print(f"❌ INVARIANT BROKEN: captains with >1 active ride: {overloaded}")
            raise SystemExit(1)
        print("✅ atomic reservation invariant holds: no captain has >1 active ride")

        # Invariant #2: no ride stuck in `new` or `broadcasting`
        stuck = [r.id for r in finals if r.status in ("new", "broadcasting")]
        if stuck:
            print(f"❌ INVARIANT BROKEN: rides stuck mid-flow: {stuck}")
            raise SystemExit(1)
        print("✅ no rides stuck mid-flow")


if __name__ == "__main__":
    main()
