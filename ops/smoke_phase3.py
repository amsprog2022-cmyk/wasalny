"""Phase 3 smoke — WhatsApp booking pipeline.

Runs two scenarios:
  A. Gemini fails / no API key → an AdminAlert(kind=ai_handoff) is created,
     no ride is booked. This is the reliability path (Decision #4).
  B. Gemini succeeds (mocked in-process) → a Ride is created with
     source='whatsapp' and matching is queued. Matching itself is stubbed
     because Phase 2 already proves that engine end-to-end.

Both run against the local app inside its context — no live server needed.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta

from app import create_app, db
from app.models.ai_session import AdminAlert, AiSession
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.ride import Ride
from app.models.zone import Zone
from app.services import ai_parser, availability as av, whatsapp_booking


def die(m): print(f"❌ {m}"); sys.exit(1)
def ok(m): print(f"✅ {m}")


def _reset(app):
    with app.app_context():
        AdminAlert.query.delete()
        AiSession.query.delete()
        Ride.query.delete()
        Customer.query.filter(Customer.wa_id.like("209%")).delete()
        db.session.commit()


def scenario_a_handoff(app):
    print("\n--- Scenario A: no Gemini key → handoff path ---")
    with app.app_context():
        cust = Customer(wa_id="209900000001", name="A-user")
        db.session.add(cust)
        db.session.commit()
        cust_id = cust.id
        # No API key → parse_message returns unknown → handoff branch
        app.config["GEMINI_API_KEY"] = ""
        c = db.session.get(Customer, cust_id)
        result = whatsapp_booking.process_incoming(c, "عايز عربية من مكان مش موجود")
        if result.get("action") != "handoff":
            die(f"expected handoff, got {result}")
        alerts = AdminAlert.query.filter_by(customer_id=cust_id, kind="ai_handoff").all()
        if not alerts:
            die("no AdminAlert row created")
        ok(f"handoff alert created (reason: {alerts[0].payload_json})")


def scenario_b_success(app):
    print("\n--- Scenario B: Gemini succeeds (mocked) → ride created + assigned ---")
    with app.app_context():
        pickup = Zone.query.filter_by(slug="ramla").first()
        dropoff = Zone.query.filter_by(slug="university").first()
        if not pickup or not dropoff:
            die("seed zones first: python -m ops.seed_zones")

        # Pre-online a captain in the pickup zone so matching can succeed.
        cap = Driver.query.filter_by(wa_id="209800000099").first()
        if not cap:
            cap = Driver(
                wa_id="209800000099", name="P3 captain",
                car_model="Toyota", car_plate="P3-1", car_color="silver",
            )
            cap.set_password("t")
            db.session.add(cap)
            db.session.commit()
        av.set_online(cap.id, pickup.id)

        cust = Customer(wa_id="209900000002", name="B-user")
        db.session.add(cust)
        db.session.commit()

        # Monkey-patch the parser to return a valid book_ride intent.
        def _fake_parse(msg, prior=None):
            return ai_parser.ParseResult(
                intent="book_ride",
                from_zone_slug=pickup.slug,
                to_zone_slug=dropoff.slug,
                confidence=0.95,
                reply_ar="",
                raw_response="(mocked)",
            )
        ai_parser.parse_message = _fake_parse
        whatsapp_booking.ai_parser.parse_message = _fake_parse

        # Stub out the async matching spawn — we test matching separately in Phase 2 smoke.
        # This isolates Phase 3 to just the AI booking pipeline.
        from app.services import matching
        matching_calls = []
        def _stub_start_matching(ride_id, pending_fee_ids=None):
            matching_calls.append({"ride_id": ride_id, "pending_fee_ids": pending_fee_ids})
        matching.start_matching = _stub_start_matching
        whatsapp_booking.matching.start_matching = _stub_start_matching

        result = whatsapp_booking.process_incoming(cust, "عايز عربية من الرملة لجامعة بنها")
        if result.get("action") != "ride_created":
            die(f"expected ride_created, got {result}")
        ride_id = result["ride_id"]
        ok(f"ride #{ride_id} created via WhatsApp source")

        # Verify the ride was written with the right shape
        r = db.session.get(Ride, ride_id)
        if r.source != "whatsapp":
            die(f"expected source=whatsapp, got {r.source}")
        if r.from_zone_id != pickup.id or r.to_zone_id != dropoff.id:
            die(f"wrong zones: {r.from_zone_id}→{r.to_zone_id}")
        if r.status != "new":
            die(f"ride should still be 'new' (matching stubbed), got {r.status}")
        ok(f"ride shape correct: source=whatsapp, zones={r.from_zone.slug}→{r.to_zone.slug}")

        # Verify matching was scheduled
        if not matching_calls or matching_calls[0]["ride_id"] != ride_id:
            die(f"matching.start_matching was not called: {matching_calls}")
        ok("matching.start_matching queued correctly")

        # Verify AiSession moved to 'completed'
        session = AiSession.query.filter_by(customer_id=cust.id).order_by(AiSession.id.desc()).first()
        if session.status != "completed":
            die(f"AiSession stuck at status={session.status}")
        ok("AiSession marked completed")


def main() -> None:
    app = create_app()
    _reset(app)
    scenario_a_handoff(app)
    scenario_b_success(app)
    print("\n🎉 Phase 3 smoke passed.")


if __name__ == "__main__":
    main()
