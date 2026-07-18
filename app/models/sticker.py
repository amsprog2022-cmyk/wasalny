from datetime import datetime
from app import db


# Purpose = which moment in the flow the sticker is used for.
# Matches PLAN.md §Appendix C.
STICKER_PURPOSES = (
    "booked",           # trip received, searching for a captain
    "captain_coming",   # captain assigned, on their way
    "completed",        # trip finished safely
    "no_driver",        # no captain available now
    "generic",          # any brand touchpoint (e.g. onboarding reply)
)


class Sticker(db.Model):
    __tablename__ = "stickers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    purpose = db.Column(db.String(30), nullable=False, index=True)

    # Local file path (relative to project root, e.g. stickers/captain_coming.webp)
    file_path = db.Column(db.String(500), nullable=False)

    # Populated after we upload the file to Meta and get back a media id.
    wa_media_id = db.Column(db.String(120))

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "purpose": self.purpose,
            "file_path": self.file_path,
            "wa_media_id": self.wa_media_id,
            "is_active": self.is_active,
        }
