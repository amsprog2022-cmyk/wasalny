"""Phase 4b smoke — verify complaint → resolve → side effects → audit trail.

Runs in-process (no server needed).
"""
from __future__ import annotations

import sys
from decimal import Decimal

from app import create_app, db
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.ops import Complaint, CreditAdjustment, AuditLog
from app.models.ride import Ride
from app.models.zone import Zone
from app.services import complaints as complaints_svc


def die(m): print(f"❌ {m}"); sys.exit(1)
def ok(m): print(f"✅ {m}")


def main() -> None:
    app = create_app()
    with app.app_context():
        zones = Zone.query.filter_by(is_active=True).all()
        if len(zones) < 2:
            die("seed zones first")

        # Snap current DB counts
        before_credits = CreditAdjustment.query.count()
        before_audits = AuditLog.query.count()

        # Get a customer, captain, and completed ride to attach the complaint to
        customer = Customer.query.first()
        driver = Driver.query.filter_by(discipline_status="active").first()
        ride = Ride.query.filter_by(status="completed").first()
        if not (customer and driver and ride):
            die("need at least 1 customer, 1 active driver, 1 completed ride (run seed_fake_data)")

        # File a complaint against a completed ride
        c = complaints_svc.file_complaint(
            filed_by_kind="customer",
            filed_by_id=customer.id,
            subject="Test complaint",
            description="Testing Phase 4b flow",
            category="overcharge",
            ride_id=ride.id,
        )
        ok(f"complaint #{c.id} filed, status={c.status}")

        # Add an internal comment
        cc = complaints_svc.add_comment(c.id, 1, "Reaching out to captain now.")
        ok(f"comment #{cc.id} added")

        # Resolve with refund + captain warn
        driver_before_status = driver.discipline_status

        # We need to force the ride to be under this specific driver so the
        # warn side-effect targets the right captain.
        ride.driver_id = driver.id
        db.session.commit()

        resolved = complaints_svc.resolve(
            c.id,
            action="refund",
            resolution_text="Refunded 15 EGP as goodwill.",
            refund_amount_egp=Decimal("15.00"),
        )
        if resolved.status != "resolved":
            die(f"expected resolved, got {resolved.status}")
        if resolved.resolution_action != "refund":
            die(f"expected action=refund, got {resolved.resolution_action}")
        ok(f"complaint resolved: action={resolved.resolution_action}")

        # Verify a CreditAdjustment was created
        after_credits = CreditAdjustment.query.count()
        if after_credits != before_credits + 1:
            die(f"expected 1 new credit adjustment, got {after_credits - before_credits}")
        latest_credit = CreditAdjustment.query.order_by(CreditAdjustment.id.desc()).first()
        if latest_credit.amount_egp != Decimal("15.00") or latest_credit.direction != "credit":
            die(f"credit adjustment wrong: {latest_credit.amount_egp} {latest_credit.direction}")
        ok(f"credit adjustment: {latest_credit.amount_egp} EGP → customer #{latest_credit.target_id}")

        # Verify an AuditLog entry
        after_audits = AuditLog.query.count()
        if after_audits <= before_audits:
            die(f"expected new audit rows, got 0 new")
        latest_audit = AuditLog.query.order_by(AuditLog.id.desc()).first()
        if latest_audit.action != "complaint.resolve":
            die(f"audit action wrong: {latest_audit.action}")
        ok(f"audit log recorded: {latest_audit.action} target={latest_audit.target_kind}#{latest_audit.target_id}")

        # Now try a warn-captain resolution on another complaint
        c2 = complaints_svc.file_complaint(
            filed_by_kind="customer", filed_by_id=customer.id,
            subject="Second test", category="rude",
            ride_id=ride.id,
        )
        complaints_svc.resolve(
            c2.id, action="warn", resolution_text="Warning issued.",
        )
        # Reload driver
        db.session.refresh(driver)
        if driver_before_status == "active" and driver.discipline_status != "warned":
            die(f"expected driver warned, got {driver.discipline_status}")
        ok(f"captain #{driver.id} discipline_status={driver.discipline_status}")

    print("\n🎉 Phase 4b smoke passed.")


if __name__ == "__main__":
    main()
