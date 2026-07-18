"""Send brand stickers over WhatsApp.

Meta's Cloud API requires stickers as WebP 512×512 uploaded via the media
endpoint, then referenced by media_id in the message. We cache media_ids
on the Sticker DB row so we only upload each sticker once.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests
from flask import current_app

from app import db
from app.models.sticker import Sticker
from app.services.whatsapp import WhatsAppError, _base_url, _headers, _post, GRAPH_URL


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_path(rel: str) -> Path:
    return PROJECT_ROOT / rel


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".webp":
        return "image/webp"
    if ext == ".png":
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "application/octet-stream"


def upload_sticker(sticker: Sticker) -> str:
    """Upload the sticker file to Meta and cache the returned media_id."""
    path = _resolve_path(sticker.file_path)
    if not path.exists():
        raise WhatsAppError(f"sticker file missing on disk: {path}")

    url = f"{_base_url()}/media"
    token = current_app.config["WHATSAPP_ACCESS_TOKEN"]
    mime = _mime_for(path)
    with path.open("rb") as fh:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (path.name, fh, mime)},
            timeout=30,
        )
    if resp.status_code >= 400:
        raise WhatsAppError(f"upload failed {resp.status_code}: {resp.text}")
    media_id = resp.json().get("id")
    if not media_id:
        raise WhatsAppError(f"no media_id in response: {resp.text}")
    sticker.wa_media_id = media_id
    db.session.commit()
    return media_id


def send_sticker_by_purpose(to_wa_id: str, purpose: str) -> Optional[dict]:
    """Look up the sticker for a given moment (e.g. 'captain_coming') and send it.

    If no sticker is registered for that purpose, returns None quietly — callers
    should still send the accompanying text message. If a sticker is registered
    but has no wa_media_id yet, we upload on first use.
    """
    sticker = (
        Sticker.query.filter_by(purpose=purpose, is_active=True)
        .order_by(Sticker.id.desc())
        .first()
    )
    if sticker is None:
        return None
    if not sticker.wa_media_id:
        try:
            upload_sticker(sticker)
        except WhatsAppError as e:
            current_app.logger.warning("sticker upload failed for %s: %s", purpose, e)
            return None

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_wa_id,
        "type": "sticker",
        "sticker": {"id": sticker.wa_media_id},
    }
    return _post(payload)
