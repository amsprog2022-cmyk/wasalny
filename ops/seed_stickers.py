"""Register brand stickers in the DB.

Actual .webp files live in `wassalny/stickers/`. This script only ensures a
DB row exists per sticker so the WhatsApp responder (Phase 3) can look them
up by purpose.

Usage:
  python -m ops.seed_stickers
"""
from __future__ import annotations

from app import create_app, db
from app.models.sticker import Sticker


STICKERS = [
    # Same branded PNG covers two moments until distinct art is provided:
    #   - `booked`         → fired the instant we understand the booking intent
    #   - `captain_coming` → fired when a captain accepts the offer
    {
        "name": "booked_247",
        "purpose": "booked",
        "file_path": "stickers/captain_coming.png",
    },
    {
        "name": "captain_coming_247",
        "purpose": "captain_coming",
        "file_path": "stickers/captain_coming.png",
    },
]


def run() -> None:
    app = create_app()
    with app.app_context():
        created = 0
        for s in STICKERS:
            existing = Sticker.query.filter_by(name=s["name"]).first()
            if existing:
                existing.purpose = s["purpose"]
                existing.file_path = s["file_path"]
            else:
                db.session.add(Sticker(**s, is_active=True))
                created += 1
        db.session.commit()
        print(f"[seed] Stickers created: {created}, total: {Sticker.query.count()}")


if __name__ == "__main__":
    run()
