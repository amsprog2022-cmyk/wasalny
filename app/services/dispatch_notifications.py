"""Shared helpers for post-dispatch customer notifications.

Both the alert-driven assign flow (`app/routes/alerts.py assign`) and
the admin-created ride flow (`app/routes/live_map.py create_ride`) send
the same "🚗 لقينالك كابتن! ..." WhatsApp text with car + phone info.
This module centralises that so the two callers stay in sync.
"""
from __future__ import annotations

from typing import Any

from flask import current_app

from app.models.customer import Customer
from app.models.driver import Driver
from app.services import whatsapp
from app.services.whatsapp import WhatsAppError


def notify_customer_of_assignment(customer: Customer, driver: Driver) -> bool:
    """Send the "captain assigned" WhatsApp text with car + phone info to
    the customer, and persist it to the conversation inbox so admins see
    it on the timeline. Best-effort — returns True/False so the caller
    can flash an alert to the admin when the send fails.
    """
    if customer is None or not customer.wa_id:
        return False

    car_bits = []
    if getattr(driver, "car_model", None):
        car_bits.append(driver.car_model)
    if getattr(driver, "car_color", None):
        car_bits.append(driver.car_color)
    car_line = " · ".join(car_bits)
    plate = getattr(driver, "car_plate", None)
    wa_display = (
        driver.wa_id
        if driver.wa_id and driver.wa_id.startswith("+")
        else f"+{driver.wa_id}" if driver.wa_id else ""
    )
    body_lines = [
        f"🚗 لقينالك كابتن! ده {driver.name} جاي دلوقتي.",
    ]
    if car_line:
        body_lines.append(f"العربية: {car_line}")
    if plate:
        body_lines.append(f"اللوحة: {plate}")
    if wa_display:
        body_lines.append(f"📞 رقمه: {wa_display}")
        body_lines.append("لو محتاج تكلمه اضغط على الرقم.")
    body = "\n".join(body_lines)

    try:
        resp: Any = whatsapp.send_text(customer.wa_id, body)
    except WhatsAppError as e:  # noqa: BLE001
        current_app.logger.warning("assignment WhatsApp failed: %s", e)
        return False

    wa_msg_id = None
    if isinstance(resp, dict):
        wa_msg_id = (resp.get("messages") or [{}])[0].get("id")

    # Persist so it shows up on the admin conversation timeline.
    try:
        from app.services import whatsapp_booking
        whatsapp_booking._persist_outbound(
            customer, body, msg_type="text", wa_message_id=wa_msg_id,
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("assignment persist failed: %s", e)

    # Now that they've got a captain, nudge them toward the app. Throttled
    # inside send_app_promo so a regular WhatsApp rider isn't advertised at
    # after every single trip.
    try:
        from app.services import whatsapp_menu
        whatsapp_menu.send_app_promo(customer)
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("app promo after assignment failed: %s", e)

    return True
