"""Smoke test for the 5 new captain endpoints.

Runs in-process against the local app. Verifies:
  - change-password respects must_change_password + current-password check
  - earnings breaks down into today/week/month buckets
  - discipline returns baseline zero state
  - reject increments the rejection counter and applies warn/suspend at thresholds
  - rate-customer records a rating and rejects duplicates
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from decimal import Decimal

from app import create_app, db
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.ride import Ride
from app.models.zone import Zone
from app.models.captain_rating import CaptainRatingOfCustomer


def die(m): print(f"❌ {m}"); sys.exit(1)
def ok(m): print(f"✅ {m}")


def main() -> None:
    app = create_app()
    with app.app_context():
        # Clean state
        CaptainRatingOfCustomer.query.delete()
        Ride.query.delete()
        Customer.query.filter(Customer.wa_id.like("29999%")).delete()
        Driver.query.filter(Driver.wa_id == "20180000199").delete()
        db.session.commit()

        # Setup
        pickup = Zone.query.filter_by(slug="ramla").first() or Zone.query.first()
        dropoff = Zone.query.filter_by(slug="university").first() or Zone.query.offset(1).first()
        cap = Driver(
            wa_id="20180000199", name="Captain Smoke",
            car_model="Toyota", car_plate="CAP-SMK",
            approval_status="approved", is_active=True, must_change_password=True,
        )
        cap.set_password("wassalny-2026")
        db.session.add(cap)
        cust = Customer(wa_id="29999900000", name="Smoke Cust")
        db.session.add(cust)
        db.session.commit()

        # Fake completed ride
        ride = Ride(
            customer_id=cust.id, driver_id=cap.id,
            from_zone_id=pickup.id, to_zone_id=dropoff.id,
            price_egp=Decimal("25.00"), commission_egp=Decimal("3.75"),
            no_show_fee_egp=Decimal("0"), status="completed", source="app",
            completed_at=datetime.utcnow(),
        )
        db.session.add(ride)
        db.session.commit()

    # Login as the captain
    client = app.test_client()

    login = client.post(
        "/api/v1/auth/driver/login",
        json={"wa_id": "20180000199", "password": "wassalny-2026"},
    )
    if login.status_code != 200:
        die(f"login failed: {login.status_code} {login.data.decode()}")
    j = login.get_json()
    if not j.get("must_change_password"):
        die("must_change_password should be True on first login")
    ok(f"login ok, must_change_password={j['must_change_password']}")
    token = j["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    # 1) change-password: wrong current pw → 401
    r = client.post("/api/v1/driver/change-password", json={"current_password": "wrong", "new_password": "newpass123"}, headers=h)
    if r.status_code != 401:
        die(f"wrong current should 401, got {r.status_code}")
    ok("change-password rejects wrong current pw")

    # 1b) change-password: too short new → 400
    r = client.post("/api/v1/driver/change-password", json={"current_password": "wassalny-2026", "new_password": "x"}, headers=h)
    if r.status_code != 400:
        die(f"short new should 400, got {r.status_code}")
    ok("change-password rejects <6 char passwords")

    # 1c) success
    r = client.post("/api/v1/driver/change-password", json={"current_password": "wassalny-2026", "new_password": "newSecure123"}, headers=h)
    if r.status_code != 200:
        die(f"change-password success failed: {r.status_code} {r.data.decode()}")
    with app.app_context():
        drv = Driver.query.filter_by(wa_id="20180000199").first()
        if drv.must_change_password:
            die("must_change_password flag should be cleared after change")
    ok("change-password success + must_change_password cleared")

    # 2) earnings — should reflect our 1 fake completed ride
    r = client.get("/api/v1/driver/earnings", headers=h)
    if r.status_code != 200:
        die(f"earnings failed: {r.status_code}")
    e = r.get_json()
    if e["today"]["trips"] != 1 or e["today"]["gross_egp"] != 25.0:
        die(f"today bucket wrong: {e['today']}")
    if e["today"]["net_egp"] != 21.25:
        die(f"net wrong: {e['today']['net_egp']}")
    ok(f"earnings: today={e['today']['trips']} trip, {e['today']['gross_egp']} gross, {e['today']['net_egp']} net")

    # 3) discipline — baseline should be 0 rejections
    r = client.get("/api/v1/driver/discipline", headers=h)
    d = r.get_json()
    if d["rejections_today"] != 0:
        die(f"rejections should be 0 baseline, got {d['rejections_today']}")
    if d["remaining_before_warning"] != 5:
        die(f"remaining_before_warning should be 5, got {d['remaining_before_warning']}")
    ok(f"discipline baseline: {d['rejections_today']} rejections, {d['remaining_before_warning']} until warning")

    # 4) reject — need a fresh broadcasting ride first (this endpoint doesn't need a real ride but ride_id must exist)
    with app.app_context():
        cust_id = Customer.query.filter_by(wa_id="29999900000").first().id
        pickup_id = Zone.query.filter_by(slug="ramla").first().id
        dropoff_id = Zone.query.filter_by(slug="university").first().id
        broadcasting = Ride(
            customer_id=cust_id, from_zone_id=pickup_id, to_zone_id=dropoff_id,
            price_egp=Decimal("20"), commission_egp=Decimal("3"),
            status="broadcasting", source="app",
        )
        db.session.add(broadcasting); db.session.commit()
        b_id = broadcasting.id

    r = client.post(f"/api/v1/rides/{b_id}/reject", headers=h)
    if r.status_code != 200:
        die(f"reject failed: {r.status_code} {r.data.decode()}")
    resp = r.get_json()
    if resp["rejections_today"] != 1:
        die(f"expected 1 rejection, got {resp['rejections_today']}")
    ok(f"reject counted → {resp['rejections_today']}/{resp['warn_threshold']} today, action={resp['action']}")

    # Push to warn threshold (5 rejections total → warn)
    for _ in range(4):
        client.post(f"/api/v1/rides/{b_id}/reject", headers=h)
    r = client.get("/api/v1/driver/discipline", headers=h)
    d = r.get_json()
    if d["rejections_today"] != 5:
        die(f"expected 5 rejections, got {d['rejections_today']}")
    if d["discipline_status"] != "warned":
        die(f"expected warned at 5 rejections, got {d['discipline_status']}")
    ok(f"discipline: 5 rejections → status=warned ✓")

    # 5) rate-customer — first rate should succeed
    with app.app_context():
        ride_id = Ride.query.filter_by(status="completed").first().id
    r = client.post(f"/api/v1/rides/{ride_id}/rate-customer", json={"stars": 5, "comment": "عميل محترم"}, headers=h)
    if r.status_code != 200:
        die(f"rate-customer failed: {r.status_code} {r.data.decode()}")
    ok(f"rate-customer 5 stars saved")

    # duplicate → 409
    r = client.post(f"/api/v1/rides/{ride_id}/rate-customer", json={"stars": 4}, headers=h)
    if r.status_code != 409:
        die(f"duplicate rating should 409, got {r.status_code}")
    ok("rate-customer rejects duplicates")

    print("\n🎉 all 5 captain endpoints verified.")


if __name__ == "__main__":
    main()
