"""Targeted smoke: no-show flow → pending fee → applied to next booking.

Requires the server started with NO_SHOW_ENABLE_AFTER_MINUTES=0 so we don't
have to wait 5 real minutes.
"""
from __future__ import annotations

import os
import sys
import time

import requests


BASE = os.getenv("BASE", "http://localhost:5000")


def die(m): print(f"❌ {m}"); sys.exit(1)
def ok(m): print(f"✅ {m}")


def main() -> None:
    from app import create_app, db
    from app.models.driver import Driver
    from app.models.ride import Ride, CustomerPendingFee
    from app.models.customer import Customer
    from app.models.zone import Zone

    app = create_app()
    with app.app_context():
        pickup = Zone.query.filter_by(slug="ramla").first()
        dropoff = Zone.query.filter_by(slug="university").first()
        if not pickup or not dropoff:
            die("run: python -m ops.seed_zones")

        # Reset ride & fee state
        Ride.query.delete()
        CustomerPendingFee.query.delete()
        Customer.query.filter(Customer.wa_id.like("20199%")).delete()
        db.session.commit()

        # Ensure a captain exists
        cap = Driver.query.filter_by(wa_id="201000000099").first()
        if not cap:
            cap = Driver(
                wa_id="201000000099", name="No-show captain",
                car_model="Toyota", car_plate="NSH-1", car_color="silver",
            )
            cap.set_password("t")
            db.session.add(cap)
            db.session.commit()

        pickup_id, dropoff_id = pickup.id, dropoff.id

    # ------------ ride 1: force a no-show ------------
    cust = requests.post(f"{BASE}/api/v1/customer/login", json={"wa_id": "201990000001", "name": "Test"}).json()
    ch = {"Authorization": f"Bearer {cust['access_token']}"}

    cap_tok = requests.post(f"{BASE}/api/v1/auth/driver/login", json={"wa_id": "201000000099"}).json()["access_token"]
    dh = {"Authorization": f"Bearer {cap_tok}"}

    requests.post(f"{BASE}/api/v1/driver/availability", headers=dh, json={"action": "online", "zone_id": pickup_id}).raise_for_status()

    ride = requests.post(f"{BASE}/api/v1/rides", headers=ch, json={"from_zone_id": pickup_id, "to_zone_id": dropoff_id}).json()
    rid = ride["id"]
    ok(f"ride #{rid} created @ {ride['price_egp']} EGP")

    # Wait for broadcasting, then accept
    for _ in range(30):
        time.sleep(0.2)
        s = requests.get(f"{BASE}/api/v1/rides/{rid}", headers=ch).json()["status"]
        if s == "broadcasting": break
    else:
        die("stuck in new")
    r = requests.post(f"{BASE}/api/v1/rides/{rid}/accept", headers=dh)
    if r.status_code != 200: die(f"accept failed: {r.text}")
    ok("captain accepted")

    # Wait a beat for assign() to run
    for _ in range(20):
        time.sleep(0.2)
        if requests.get(f"{BASE}/api/v1/rides/{rid}", headers=ch).json()["status"] == "assigned":
            break
    ok("assigned")

    # Mark no-show (env NO_SHOW_ENABLE_AFTER_MINUTES=0 → immediately eligible)
    r = requests.post(f"{BASE}/api/v1/rides/{rid}/no-show", headers=dh)
    if r.status_code != 200:
        die(f"no-show failed: {r.status_code} {r.text}")
    final_status = r.json()["status"]
    if final_status != "cancelled_no_show":
        die(f"expected cancelled_no_show got {final_status}")
    ok(f"no-show recorded, status = {final_status}")

    # ------------ verify pending fee row exists ------------
    with app.app_context():
        fee = CustomerPendingFee.query.filter_by(from_ride_id=rid).first()
        if fee is None:
            die("no pending fee row created")
        ok(f"pending fee row created: {fee.amount_egp} EGP, reason={fee.reason}")

    # ------------ ride 2: quote should include the fee ------------
    q = requests.post(
        f"{BASE}/api/v1/rides/quote",
        headers=ch,
        json={"from_zone_id": pickup_id, "to_zone_id": dropoff_id},
    ).json()
    if q["pending_fees_egp"] <= 0:
        die(f"quote should show pending fees: {q}")
    ok(f"next quote → ride={q['ride_price_egp']} + pending={q['pending_fees_egp']} = total {q['total_egp']} EGP")

    # Bring captain online again (was set offline by no_show)
    requests.post(f"{BASE}/api/v1/driver/availability", headers=dh, json={"action": "online", "zone_id": pickup_id})

    ride2 = requests.post(f"{BASE}/api/v1/rides", headers=ch, json={"from_zone_id": pickup_id, "to_zone_id": dropoff_id}).json()
    rid2 = ride2["id"]
    if ride2["no_show_fee_egp"] <= 0:
        die(f"ride2 should carry the pending fee: {ride2}")
    ok(f"ride #{rid2} carries {ride2['no_show_fee_egp']} EGP pending fee")

    # Verify the fee row was marked applied
    with app.app_context():
        fee = CustomerPendingFee.query.filter_by(from_ride_id=rid).first()
        if fee.applied_to_ride_id != rid2:
            die(f"fee not marked applied: {fee.applied_to_ride_id}")
        ok(f"pending fee marked applied → ride #{fee.applied_to_ride_id}")

    print("\n🎉 no-show + pending-fee path verified.")


if __name__ == "__main__":
    main()
