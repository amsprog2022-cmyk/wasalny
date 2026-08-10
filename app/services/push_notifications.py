"""Firebase Cloud Messaging push notifications.

Silent-fails when Firebase isn't configured (e.g. on a laptop without the
service account env var) so the rest of the app keeps working. Call
`_init_firebase_admin` at app boot in app/__init__.py — this module assumes
Firebase Admin has already been initialised.

Broadcasts have a side effect worth knowing about: tokens FCM reports as
permanently dead are cleared from the Customer/Driver rows, so a fan-out
both sends and prunes.

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
    is_ride_offer = string_data.get("kind") == "trip_offered"

    try:
        message = _build_message(
            m, string_data, title=title, body=body,
            collapse_key=collapse_key, is_ride_offer=is_ride_offer,
            token=token,
        )
        m.send(message)
        return True
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("FCM send failed: %s", e)
        return False


def _build_message(m, string_data: dict, *, title: str, body: str,
                   collapse_key: str | None, is_ride_offer: bool,
                   token: str | None = None, tokens: list | None = None):
    """Build the FCM message with the platform overrides the client needs.

    Trip-offered pushes are **data-only on Android**. That's what lets the
    Dart _backgroundMessageHandler in main.dart fire when the app is killed,
    which is what triggers the full-screen ride-offer alarm. If we sent a
    top-level notification, the OS would render a normal quiet notification
    itself and skip the background handler entirely — the exact bug where
    a killed captain phone shows a silent push instead of a ringing alarm.

    iOS can't render a data-only push (no banner appears), so we mirror the
    title/body into the APNS alert block so iOS still notifies. Everything
    that isn't a trip offer keeps the old notification+data shape.
    """
    channel_id = "ride_offer" if is_ride_offer else "wassalny_default"

    if is_ride_offer:
        # Preserve title/body inside data — the Dart killed-state handler
        # reads data['title'] / data['body'] to render its own alarm.
        string_data.setdefault("title", title)
        string_data.setdefault("body", body)

        android_cfg = m.AndroidConfig(
            priority="high",
            collapse_key=collapse_key,
            # NO android.notification block — that's what keeps the payload
            # data-only on Android and forces the background handler to run.
        )
        apns_cfg = m.APNSConfig(
            headers={"apns-priority": "10"},
            payload=m.APNSPayload(
                aps=m.Aps(
                    alert=m.ApsAlert(title=title, body=body),
                    sound="default",
                    content_available=True,
                    mutable_content=True,
                    custom_data={"interruption-level": "time-sensitive"},
                ),
            ),
        )
        notification_arg = None
    else:
        android_cfg = m.AndroidConfig(
            priority="high",
            collapse_key=collapse_key,
            notification=m.AndroidNotification(
                sound="default",
                channel_id=channel_id,
            ),
        )
        apns_cfg = m.APNSConfig(
            headers={"apns-priority": "10"},
            payload=m.APNSPayload(
                aps=m.Aps(
                    sound="default",
                    content_available=True,
                    mutable_content=True,
                ),
            ),
        )
        notification_arg = m.Notification(title=title, body=body)

    if tokens is not None:
        return m.MulticastMessage(
            tokens=tokens,
            notification=notification_arg,
            data=string_data,
            android=android_cfg,
            apns=apns_cfg,
        )
    return m.Message(
        token=token,
        notification=notification_arg,
        data=string_data,
        android=android_cfg,
        apns=apns_cfg,
    )


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


# FCM rejects a multicast carrying more than this many tokens.
_MULTICAST_CHUNK = 500


def _send_multicast(tokens: list[str], title: str, body: str,
                    data: dict | None = None,
                    collapse_key: str | None = None) -> int:
    """Fan out to raw tokens. Returns the count FCM accepted."""
    tokens = [t for t in tokens if t]
    if not tokens:
        return 0
    m = _messaging()
    if m is None:
        return 0

    string_data = {k: str(v) for k, v in (data or {}).items()}
    is_ride_offer = string_data.get("kind") == "trip_offered"

    sent = 0
    dead: list[str] = []
    for i in range(0, len(tokens), _MULTICAST_CHUNK):
        chunk = tokens[i:i + _MULTICAST_CHUNK]
        try:
            # A copy per chunk so setdefault('title'/'body') for trip-offered
            # doesn't compound across iterations (harmless but tidy).
            per_chunk_data = dict(string_data)
            message = _build_message(
                m, per_chunk_data, title=title, body=body,
                collapse_key=collapse_key, is_ride_offer=is_ride_offer,
                tokens=chunk,
            )
            resp = m.send_each_for_multicast(message)
            sent += int(resp.success_count or 0)
            dead.extend(_dead_tokens(chunk, resp))
        except Exception as e:  # noqa: BLE001
            # One bad chunk must not sink the rest of a 10k broadcast.
            current_app.logger.warning("FCM batch send failed: %s", e)
    if dead:
        _forget_tokens(dead)
    return sent


def _dead_tokens(chunk: list[str], resp) -> list[str]:
    """Tokens FCM says will never work again — uninstalled apps, mostly.

    Anything else (a timeout, a quota trip) is transient and must be left
    alone; deleting on those would silently unsubscribe live users.
    """
    out = []
    for token, r in zip(chunk, getattr(resp, "responses", []) or []):
        exc = getattr(r, "exception", None)
        if exc is None:
            continue
        if type(exc).__name__ in ("UnregisteredError", "SenderIdMismatchError"):
            out.append(token)
    return out


def _forget_tokens(tokens: list[str]) -> None:
    """Clear dead tokens so the next broadcast doesn't pay for them again."""
    try:
        for model in (Customer, Driver):
            (model.query
             .filter(model.fcm_token.in_(tokens))
             .update({model.fcm_token: None}, synchronize_session=False))
        db.session.commit()
        current_app.logger.info("cleared %d dead FCM tokens", len(tokens))
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.warning("FCM token cleanup failed: %s", e)


def send_to_drivers(driver_ids: list[int], *, title: str, body: str,
                    data: dict | None = None, collapse_key: str | None = None) -> int:
    """Fan-out. Returns count of successful pushes."""
    if not driver_ids:
        return 0
    drivers = Driver.query.filter(Driver.id.in_(driver_ids)).all()
    return _send_multicast(
        [d.fcm_token for d in drivers if d.fcm_token],
        title, body, data, collapse_key,
    )


def broadcast_push(audience: str, *, title: str, body: str,
                   data: dict | None = None) -> dict:
    """Admin broadcast to every registered device.

    `audience` is one of customer / driver / both. Returns per-side counts so
    the admin page can report what actually went out rather than how many
    accounts exist.
    """
    result = {"customers": 0, "drivers": 0}
    # Only the token column — at 10k accounts, hydrating full ORM rows to read
    # one string each is the difference between a query and an outage.
    if audience in ("customer", "both"):
        tokens = [
            row[0] for row in
            db.session.query(Customer.fcm_token)
            .filter(Customer.fcm_token.isnot(None)).all()
        ]
        result["customers"] = _send_multicast(tokens, title, body, data)
    if audience in ("driver", "both"):
        tokens = [
            row[0] for row in
            db.session.query(Driver.fcm_token)
            .filter(Driver.fcm_token.isnot(None)).all()
        ]
        result["drivers"] = _send_multicast(tokens, title, body, data)
    return result
