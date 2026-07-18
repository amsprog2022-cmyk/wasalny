"""Seed the 5 test zones + placeholder pricing matrix.

Idempotent: safe to re-run. Zones are matched by slug; existing rows are left alone.
Prices follow Appendix C in PLAN.md: 20 EGP same-zone, 25 EGP cross-zone.

Usage:
  python -m ops.seed_zones
"""
from __future__ import annotations

from decimal import Decimal

from app import create_app, db
from app.models.zone import Zone, ZonePricing


TEST_ZONES = [
    ("ramla",          "الرملة",       "El Ramla"),
    ("downtown",       "وسط البلد",     "Downtown"),
    ("university",     "جامعة بنها",    "Benha University"),
    ("sarayat",        "السرايات",     "El Sarayat"),
    ("damanhour_road", "طريق دمنهور",  "Damanhour Road"),
]

SAME_ZONE_PRICE = Decimal("20.00")
CROSS_ZONE_PRICE = Decimal("25.00")


def run() -> None:
    app = create_app()
    with app.app_context():
        by_slug: dict[str, Zone] = {}
        created_zones = 0
        for slug, name_ar, name_en in TEST_ZONES:
            z = Zone.query.filter_by(slug=slug).first()
            if z is None:
                z = Zone(slug=slug, name_ar=name_ar, name_en=name_en, is_active=True)
                db.session.add(z)
                created_zones += 1
            by_slug[slug] = z
        db.session.commit()

        # Refresh IDs (Zone.id needs a flush to be non-None for new rows)
        for z in by_slug.values():
            db.session.refresh(z)

        existing = {
            (p.from_zone_id, p.to_zone_id) for p in ZonePricing.query.all()
        }
        created_prices = 0
        for f in by_slug.values():
            for t in by_slug.values():
                if (f.id, t.id) in existing:
                    continue
                price = SAME_ZONE_PRICE if f.id == t.id else CROSS_ZONE_PRICE
                db.session.add(
                    ZonePricing(from_zone_id=f.id, to_zone_id=t.id, price_egp=price)
                )
                created_prices += 1
        db.session.commit()

        print(f"[seed] Zones created: {created_zones}, already-present: {len(TEST_ZONES) - created_zones}")
        print(f"[seed] Price rows created: {created_prices}")
        print(f"[seed] Total zones: {Zone.query.count()}, total price rows: {ZonePricing.query.count()}")


if __name__ == "__main__":
    run()
