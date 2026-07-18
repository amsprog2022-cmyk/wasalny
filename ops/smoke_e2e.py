"""End-to-end smoke test for Phase 2.

Runs against a live server on http://localhost:5000. Requires:
  - App booted (`python wsgi.py`)
  - `python -m ops.seed_zones` already run
  - At least one driver row we can log in as (created here if missing)

Flow tested:
  1. Customer logs in (phone-only)
  2. Customer quotes a ride
  3. A test captain "goes online" in the pickup zone (via availability service directly
     — we don't have a driver Socket.IO client in this script)
  4. Customer creates the ride
  5. Captain polls broadcasting → accepts → wins the lock
  6. Captain starts the trip → completes it
  7. Verify ride ended in `completed`
"""
from __future__ import annotations

import os
import sys
import time

import requests


BASE = os.getenv("BASE", "http://localhost:5000")


def die(msg: str) -> None:
    print(f"❌ {msg}")
    sys.exit(1)


def ok(msg: str) -> None:
    print(f"✅ {msg}")


def main() -> None:
    # ---- setup: seed a test captain in the DB and mark them available in Redis ----
    from app import create_app, db
    from app.models.driver import Driver
    from app.models.zone import Zone
    from app.services import availability as av

    app = create_app()
    with app.app_context():
        pickup = Zone.query.filter_by(slug="ramla").first()
        dropoff = Zone.query.filter_by(slug="university").first()
        if not pickup or not dropoff:
            die("Seed zones first: python -m ops.seed_zones")

        captain = Driver.query.filter_by(wa_id="201000000001").first()
        if not captain:
            captain = Driver(
                wa_id="201000000001",
                name="Ahmed (test captain)",
                car_model="Toyota Corolla",
                car_plate="ABC 123",
                car_color="silver",
            )
            captain.set_password("test-pass")
            db.session.add(captain)
            db.session.commit()
        # Reset any prior ride state that might leak across smoke runs
        from app.models.ride import Ride
        Ride.query.delete()
        db.session.commit()
        pickup_id, dropoff_id = pickup.id, dropoff.id

    # ---- customer login (phone only) ----
    r = requests.post(f"{BASE}/api/v1/customer/login", json={"wa_id": "201122334455", "name": "Sara"})
    if r.status_code != 200:
        die(f"customer login failed: {r.status_code} {r.text}")
    cust_token = r.json()["access_token"]
    ok("customer logged in")

    ch = {"Authorization": f"Bearer {cust_token}"}

    # ---- quote ----
    r = requests.post(
        f"{BASE}/api/v1/rides/quote",
        headers=ch,
        json={"from_zone_id": pickup_id, "to_zone_id": dropoff_id},
    )
    if r.status_code != 200:
        die(f"quote failed: {r.status_code} {r.text}")
    q = r.json()
    ok(f"quote → {q['ride_price_egp']} EGP")

    # ---- captain login ----
    r = requests.post(f"{BASE}/api/v1/auth/driver/login", json={"wa_id": "201000000001"})
    if r.status_code != 200:
        die(f"driver login failed: {r.status_code} {r.text}")
    drv_token = r.json()["access_token"]
    dh = {"Authorization": f"Bearer {drv_token}"}
    ok("captain logged in")

    # ---- captain goes online in the server's Redis (over HTTP) ----
    r = requests.post(
        f"{BASE}/api/v1/driver/availability",
        headers=dh,
        json={"action": "online", "zone_id": pickup_id},
    )
    if r.status_code != 200 or not r.json().get("online"):
        die(f"driver online failed: {r.status_code} {r.text}")
    ok("captain online in pickup zone")

    # ---- customer creates the ride (matching kicks off in background greenlet) ----
    r = requests.post(
        f"{BASE}/api/v1/rides",
        headers=ch,
        json={"from_zone_id": pickup_id, "to_zone_id": dropoff_id},
    )
    if r.status_code != 201:
        die(f"create ride failed: {r.status_code} {r.text}")
    ride = r.json()
    ride_id = ride["id"]
    ok(f"ride #{ride_id} created, status={ride['status']}")

    # ---- wait for status to become "broadcasting", then accept ----
    for _ in range(20):
        time.sleep(0.2)
        r = requests.get(f"{BASE}/api/v1/rides/{ride_id}", headers=ch)
        if r.status_code == 200 and r.json().get("status") == "broadcasting":
            break
    else:
        die("ride never reached broadcasting")
    ok("ride is broadcasting")

    # ---- accept ----
    r = requests.post(f"{BASE}/api/v1/rides/{ride_id}/accept", headers=dh)
    if r.status_code != 200 or not r.json().get("claimed"):
        die(f"accept failed: {r.status_code} {r.text}")
    ok("captain claimed the ride (atomic SET NX)")

    # ---- wait until ride is 'assigned' ----
    for _ in range(20):
        time.sleep(0.2)
        r = requests.get(f"{BASE}/api/v1/rides/{ride_id}", headers=ch)
        if r.status_code == 200 and r.json().get("status") == "assigned":
            break
    else:
        die("ride never reached assigned after accept")
    ok("ride is assigned → driver populated")

    # ---- start ----
    r = requests.post(f"{BASE}/api/v1/rides/{ride_id}/start", headers=dh)
    if r.status_code != 200 or r.json().get("status") != "started":
        die(f"start failed: {r.status_code} {r.text}")
    ok("trip started")

    # ---- complete ----
    r = requests.post(f"{BASE}/api/v1/rides/{ride_id}/complete", headers=dh)
    if r.status_code != 200 or r.json().get("status") != "completed":
        die(f"complete failed: {r.status_code} {r.text}")
    ok(f"trip completed → final status: {r.json()['status']}")

    # ---- re-quote should be same price (no pending fees) ----
    r = requests.post(
        f"{BASE}/api/v1/rides/quote",
        headers=ch,
        json={"from_zone_id": pickup_id, "to_zone_id": dropoff_id},
    )
    q2 = r.json()
    if q2["pending_fees_egp"] != 0:
        die("unexpected pending fees after clean trip")
    ok("no pending fees after clean trip")

    print("\n🎉 Phase 2 end-to-end smoke passed.")


if __name__ == "__main__":
    main()
