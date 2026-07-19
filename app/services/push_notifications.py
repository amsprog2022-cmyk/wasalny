"""Firebase Cloud Messaging push notifications.

Silent-fails when Firebase isn't configured (e.g. on a laptop without the
service account env var) so the rest of the app keeps working. Call
`_init_firebase_admin` at app boot in app/__init__.py — this module assumes
Firebase Admin has already been initialised.

Payload shape (both `notification` + `data` for maximum flexibility):
  notification: shown by the OS when the app is backgrounded/killed
  data: parsed by the Flutter app to deep-link into the right screen
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import current_app

from app import db
from app.models.customer import Customer
from app.models.driver import Driver


# ---------- token registration ----------

def register_customer_token(customer_id: int, token: str, platform: str) -> None:
    c = db.session.get(Customer, customer_id)
    if c is None:
        return
    c.fcm_token = token
    c.fcm_platform = platform
    c.fcm_updated_at = datetime.utcnow()
    db.session.commit()


def register_driver_token(driver_id: int, token: str, platform: str) -> None:
    d = db.session.get(Driver, driver_id)
    if d is None:
        return
    d.fcm_token = token
    d.fcm_platform = platform
    d.fcm_updated_at = datetime.utcnow()
    db.session.commit()


def clear_customer_token(customer_id: int) -> None:
    c = db.session.get(Customer, customer_id)
    if c is not None:
        c.fcm_token = None
        db.session.commit()


def clear_driver_token(driver_id: int) -> None:
    d = db.session.get(Driver, driver_id)
    if d is not None:
        d.fcm_token = None
        db.session.commit()


# ---------- send helpers ----------

def _messaging():
    """Lazy import so the module loads even without firebase-admin installed."""
    try:
        from firebase_admin import messaging  # noqa: WPS433
        return messaging
    except Exception:  # noqa: BLE001
        return None


def _send(token: Optional[str], title: str, body: str, data: dict | None = None,
          collapse_key: str | None = None) -> bool:
    """Send one push. Returns True if delivered to FCM successfully."""
    if not token:
        return False
    m = _messaging()
    if m is None:
        current_app.logger.warning("firebase_admin not available — skipping push")
        return False

    # FCM data payload values must all be strings
    string_data = {k: str(v) for k, v in (data or {}).items()}

    try:
        message = m.Message(
            token=token,
            notification=m.Notification(title=title, body=body),
            data=string_data,
            android=m.AndroidConfig(
                priority="high",
                collapse_key=collapse_key,
                notification=m.AndroidNotification(
                    sound="default",
                    channel_id="wassalny_default",
                ),
            ),
            apns=m.APNSConfig(
                headers={"apns-priority": "10"},
                payload=m.APNSPayload(
                    aps=m.Aps(
                        sound="default",
                        content_available=True,
                        mutable_content=True,
                    ),
                ),
            ),
        )
        m.send(message)
        return True
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("FCM send failed: %s", e)
        return False


def send_to_customer(customer_id: int, *, title: str, body: str,
                     data: dict | None = None, collapse_key: str | None = None) -> bool:
    c = db.session.get(Customer, customer_id)
    if c is None or not c.fcm_token:
        return False
    return _send(c.fcm_token, title, body, data, collapse_key)


def send_to_driver(driver_id: int, *, title: str, body: str,
                   data: dict | None = None, collapse_key: str | None = None) -> bool:
    d = db.session.get(Driver, driver_id)
    if d is None or not d.fcm_token:
        return False
    return _send(d.fcm_token, title, body, data, collapse_key)


def send_to_drivers(driver_ids: list[int], *, title: str, body: str,
                    data: dict | None = None, collapse_key: str | None = None) -> int:
    """Fan-out. Returns count of successful pushes."""
    if not driver_ids:
        return 0
    drivers = Driver.query.filter(Driver.id.in_(driver_ids)).all()
    tokens = [d.fcm_token for d in drivers if d.fcm_token]
    if not tokens:
        return 0

    m = _messaging()
    if m is None:
        return 0

    string_data = {k: str(v) for k, v in (data or {}).items()}

    try:
        # Prefer batch send when we have multiple recipients (single HTTP call)
        message = m.MulticastMessage(
            tokens=tokens,
            notification=m.Notification(title=title, body=body),
            data=string_data,
            android=m.AndroidConfig(
                priority="high",
                collapse_key=collapse_key,
                notification=m.AndroidNotification(
                    sound="default",
                    channel_id="wassalny_default",
                ),
            ),
            apns=m.APNSConfig(
                headers={"apns-priority": "10"},
                payload=m.APNSPayload(
                    aps=m.Aps(
                        sound="default",
                        content_available=True,
                        mutable_content=True,
                    ),
                ),
            ),
        )
        resp = m.send_each_for_multicast(message)
        return int(resp.success_count or 0)
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("FCM batch send failed: %s", e)
        return 0
