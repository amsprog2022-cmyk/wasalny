"""Seed realistic fake data so the admin dashboard has something to render.

Creates:
  - 20 captains (mix of active / suspended, various ratings & trip counts)
  - 60 customers
  - ~120 rides over the last 7 days (completed / cancelled / cancelled_no_show / active)
  - A few pending no-show fees
  - Some admin alerts (AI handoffs, no-driver events)

Idempotent by wa_id — safe to re-run.

Usage:
  python -m ops.seed_fake_data
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from decimal import Decimal

from app import create_app, db
from app.models.ai_session import AdminAlert
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.ops import Complaint, SosAlert, AdminBroadcast, Announcement
from app.models.ride import Ride, RideStatusEvent, CustomerPendingFee, Broadcast
from app.models.zone import Zone


ARABIC_MALE_NAMES = [
    "أحمد", "محمد", "علي", "حسن", "حسين", "عمرو", "طارق", "خالد",
    "مصطفى", "سعيد", "إبراهيم", "محمود", "يوسف", "كريم", "زياد",
    "أشرف", "فادي", "شريف", "هاني", "بلال",
]

ARABIC_FAMILY_NAMES = [
    "فخري", "منصور", "سالم", "الشرقاوي", "زيدان", "مبروك", "الجوهري",
    "العطار", "النجار", "الحداد", "السيد", "الجندي", "القاضي", "الفقي",
]

CUSTOMER_NAMES = [
    "سارة", "منى", "دينا", "هدى", "نور", "ياسمين", "رنا", "ندى", "شيماء", "ريم",
    "مروة", "أميرة", "علياء", "لمى", "هبة",
    "أحمد", "علي", "كريم", "زياد", "محمود",
]

CAR_MODELS = [
    ("Toyota Corolla", "أبيض"),
    ("Hyundai Elantra", "فضي"),
    ("Kia Cerato", "أسود"),
    ("Nissan Sunny", "رمادي"),
    ("Chevrolet Optra", "أبيض"),
    ("BYD F3", "فضي"),
    ("Renault Logan", "أبيض"),
]


def _rand_arabic_name() -> str:
    return f"{random.choice(ARABIC_MALE_NAMES)} {random.choice(ARABIC_FAMILY_NAMES)}"


def _seed_captains(zones: list[Zone]) -> list[Driver]:
    captains: list[Driver] = []
    for i in range(20):
        wa = f"20120000{i:04d}"
        d = Driver.query.filter_by(wa_id=wa).first()
        if d:
            captains.append(d)
            continue
        model, color = random.choice(CAR_MODELS)
        d = Driver(
            wa_id=wa,
            name=_rand_arabic_name(),
            car_model=model,
            car_plate=f"ب ن ه {random.randint(1000, 9999)}",
            car_color=color,
            category=random.choice(["economy", "economy", "economy", "business"]),
            rating=Decimal(str(round(random.uniform(4.2, 5.0), 2))),
            total_trips=random.randint(20, 500),
            discipline_status=random.choices(
                ["active", "active", "active", "active", "warned", "suspended"],
                k=1,
            )[0],
        )
        d.set_password("test")
        # Small chance of being suspended: set suspended_until
        if d.discipline_status == "suspended":
            d.suspended_until = datetime.utcnow() + timedelta(hours=random.randint(2, 20))
        db.session.add(d)
        captains.append(d)
    db.session.commit()
    return captains


def _seed_customers() -> list[Customer]:
    customers: list[Customer] = []
    for i in range(60):
        wa = f"20155500{i:04d}"
        c = Customer.query.filter_by(wa_id=wa).first()
        if c:
            customers.append(c)
            continue
        c = Customer(wa_id=wa, name=random.choice(CUSTOMER_NAMES))
        db.session.add(c)
        customers.append(c)
    db.session.commit()
    return customers


def _seed_rides(
    zones: list[Zone],
    captains: list[Driver],
    customers: list[Customer],
) -> None:
    """Create ~120 rides distributed over the last 7 days."""
    # Skip if we already seeded historical data
    if Ride.query.filter(Ride.source.in_(("app", "whatsapp"))).count() > 20:
        return

    active_captains = [c for c in captains if c.discipline_status != "suspended"]
    now = datetime.utcnow()

    for _ in range(120):
        cust = random.choice(customers)
        drv = random.choice(active_captains)
        f_zone = random.choice(zones)
        t_zone = random.choice([z for z in zones if z.id != f_zone.id])
        created_at = now - timedelta(
            hours=random.randint(0, 24 * 7),
            minutes=random.randint(0, 59),
        )

        # Weighted status outcome
        status = random.choices(
            ["completed", "completed", "completed", "completed", "completed",
             "cancelled", "cancelled_no_show"],
            k=1,
        )[0]

        price = Decimal("25.00") if f_zone.id != t_zone.id else Decimal("20.00")
        commission = (price * Decimal("0.15")).quantize(Decimal("0.01"))

        ride = Ride(
            customer_id=cust.id,
            driver_id=drv.id,
            from_zone_id=f_zone.id,
            to_zone_id=t_zone.id,
            price_egp=price,
            commission_egp=commission,
            no_show_fee_egp=0,
            status=status,
            source=random.choice(["app", "app", "app", "whatsapp"]),
            created_at=created_at,
        )

        if status == "completed":
            ride.assigned_at = created_at + timedelta(seconds=random.randint(5, 20))
            ride.started_at = ride.assigned_at + timedelta(minutes=random.randint(2, 8))
            ride.completed_at = ride.started_at + timedelta(minutes=random.randint(5, 25))
            ride.rating = random.choices([3, 4, 5, 5, 5], k=1)[0]
            if ride.rating == 5:
                ride.rating_comment = random.choice(
                    ["ممتاز", "شكراً كابتن", "الكابتن محترم جداً", None, None]
                )
        elif status == "cancelled":
            ride.assigned_at = created_at + timedelta(seconds=random.randint(5, 20))
            ride.cancelled_at = ride.assigned_at + timedelta(minutes=random.randint(1, 4))
            ride.cancel_reason = "customer_cancelled"
        elif status == "cancelled_no_show":
            ride.assigned_at = created_at + timedelta(seconds=random.randint(5, 20))
            ride.cancelled_at = ride.assigned_at + timedelta(minutes=random.randint(6, 12))
            ride.cancel_reason = "customer_no_show"

        db.session.add(ride)
        db.session.flush()

        # A minimal audit event so the ride detail page has a timeline
        db.session.add(
            RideStatusEvent(
                ride_id=ride.id,
                event="created",
                actor="customer",
                payload_json=json.dumps({"seed": True}),
                created_at=created_at,
            )
        )
        db.session.add(
            RideStatusEvent(
                ride_id=ride.id,
                event=status,
                actor="system",
                created_at=ride.completed_at or ride.cancelled_at or created_at,
            )
        )

        # For no-show, add a matching pending-fee row that eventually got applied
        if status == "cancelled_no_show":
            db.session.add(
                CustomerPendingFee(
                    customer_id=cust.id,
                    reason="no_show",
                    amount_egp=Decimal("10.00"),
                    from_ride_id=ride.id,
                    created_at=ride.cancelled_at,
                    # 50/50 whether it's been applied yet
                    applied_to_ride_id=ride.id if random.random() < 0.5 else None,
                    applied_at=ride.cancelled_at + timedelta(hours=random.randint(1, 24))
                    if random.random() < 0.5 else None,
                )
            )

    # Add 3 active-in-flight rides for realism on the home page
    for _ in range(3):
        cust = random.choice(customers)
        drv = random.choice(active_captains)
        f_zone = random.choice(zones)
        t_zone = random.choice([z for z in zones if z.id != f_zone.id])
        started = now - timedelta(minutes=random.randint(3, 12))
        db.session.add(
            Ride(
                customer_id=cust.id,
                driver_id=drv.id,
                from_zone_id=f_zone.id,
                to_zone_id=t_zone.id,
                price_egp=Decimal("25.00"),
                commission_egp=Decimal("3.75"),
                status="started",
                source="app",
                created_at=started - timedelta(minutes=2),
                assigned_at=started - timedelta(minutes=1),
                started_at=started,
            )
        )

    db.session.commit()


def _seed_alerts(customers: list[Customer]) -> None:
    """A few open alerts on the /alerts page."""
    if AdminAlert.query.count() > 3:
        return
    now = datetime.utcnow()
    for i in range(3):
        cust = random.choice(customers)
        db.session.add(
            AdminAlert(
                kind="ai_handoff",
                payload_json=json.dumps(
                    {
                        "reason": "low_confidence",
                        "message": random.choice(
                            [
                                "عايز عربية من عند المسجد",
                                "من هنا لبيت خالتي",
                                "الطلب اللي فات",
                            ]
                        ),
                    },
                    ensure_ascii=False,
                ),
                customer_id=cust.id,
                created_at=now - timedelta(minutes=random.randint(2, 45)),
            )
        )
    db.session.commit()


def _seed_complaints(customers: list[Customer], captains: list[Driver]) -> None:
    if Complaint.query.count() > 3:
        return
    now = datetime.utcnow()
    completed_rides = Ride.query.filter_by(status="completed").limit(20).all()
    subjects = [
        ("رحلة اتأخرت جداً", "wrong_route"),
        ("الكابتن كان تعبان ومهملش", "rude"),
        ("نسيت موبايلي في العربية", "missing_item"),
        ("الحساب غلط، أخد أكتر من السعر", "overcharge"),
        ("كنت بستنى والكابتن ماجاش", "no_show"),
    ]
    for i in range(8):
        subj, cat = random.choice(subjects)
        ride = random.choice(completed_rides) if completed_rides else None
        db.session.add(
            Complaint(
                filed_by_kind="customer" if random.random() < 0.7 else "driver",
                filed_by_id=random.choice(customers).id if random.random() < 0.7 else random.choice(captains).id,
                subject=subj,
                description=random.choice(["", "الكابتن كان لطيف بس اتأخر شوية", "المشكلة تكررت", ""]) or None,
                category=cat,
                ride_id=ride.id if ride else None,
                status=random.choices(["open", "open", "in_progress", "resolved"], k=1)[0],
                created_at=now - timedelta(hours=random.randint(0, 40)),
            )
        )
    db.session.commit()


def _seed_sos(customers: list[Customer], captains: list[Driver]) -> None:
    if SosAlert.query.count() > 0:
        return
    now = datetime.utcnow()
    completed = Ride.query.filter_by(status="completed").limit(5).all()
    for i in range(2):
        ride = random.choice(completed) if completed else None
        if not ride:
            continue
        db.session.add(
            SosAlert(
                ride_id=ride.id,
                customer_id=ride.customer_id,
                driver_id=ride.driver_id,
                message=random.choice(["الكابتن مش رايح الاتجاه الصح", "أنا خايفة، تعبت"]),
                status=random.choice(["open", "resolved"]),
                created_at=now - timedelta(hours=random.randint(1, 48)),
            )
        )
    db.session.commit()


def _seed_marketing() -> None:
    if AdminBroadcast.query.count() > 0:
        return
    now = datetime.utcnow()
    db.session.add(
        AdminBroadcast(
            kind="whatsapp_marketing",
            message_ar="عرض خاص للعملاء المميزين: خصم 20% على أول رحلة الأسبوع ده! 🎉",
            audience_filter_json=json.dumps({"kind": "vip"}),
            recipient_count=8,
            delivered_count=8,
            sent_at=now - timedelta(days=2),
        )
    )
    db.session.add(
        Announcement(
            audience="both",
            title_ar="جاري تحديث النظام الليلة",
            body_ar="بعض الميزات ممكن تتأثر لمدة ساعة من الساعة ٢ صباحاً.",
            priority="info",
            starts_at=now,
            ends_at=now + timedelta(hours=12),
        )
    )
    db.session.commit()


def run() -> None:
    app = create_app()
    with app.app_context():
        zones = Zone.query.filter_by(is_active=True).order_by(Zone.id.asc()).all()
        if not zones:
            raise SystemExit("run: python -m ops.seed_zones first")
        random.seed(42)  # deterministic

        captains = _seed_captains(zones)
        customers = _seed_customers()
        _seed_rides(zones, captains, customers)
        _seed_alerts(customers)
        _seed_complaints(customers, captains)
        _seed_sos(customers, captains)
        _seed_marketing()

        print(f"[fake-seed] captains:   {Driver.query.count()}")
        print(f"[fake-seed] customers:  {Customer.query.count()}")
        print(f"[fake-seed] rides:      {Ride.query.count()}")
        print(f"[fake-seed] pending:    {CustomerPendingFee.query.count()}")
        print(f"[fake-seed] alerts:     {AdminAlert.query.count()}")
        print(f"[fake-seed] complaints: {Complaint.query.count()}")
        print(f"[fake-seed] sos:        {SosAlert.query.count()}")
        print(f"[fake-seed] broadcasts: {AdminBroadcast.query.count()}")


if __name__ == "__main__":
    run()
