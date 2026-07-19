"""WhatsApp booking pipeline — customer message → Gemini → ride created.

Called from the webhook whenever an inbound customer message lands. This is
the second entry point into `create_ride`; the first is the mobile app.

Flow:
  1. Reuse or open an AiSession for this wa_id.
  2. Ask Gemini to parse the message (with the session's prior partial state).
  3. Decide:
     - book_ride + both zones + confidence >= 0.55 → send "booked" sticker
       IMMEDIATELY, then create ride, then ack with details
     - book_ride but missing a zone → save partial, ask a follow-up question
     - chat  → send Gemini's conversational reply as a friendly Wassalny agent
     - clarify → send reply_ar (booking-adjacent follow-up question)
     - unknown / low confidence / error → open admin handoff alert
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from flask import current_app

from app import db
from app.models.ai_session import AiSession, AdminAlert
from app.models.customer import Customer
from app.models.zone import Zone
from app.services import ai_parser
from app.services import ride_lifecycle
from app.services import matching
from app.services import stickers as stickers_svc
from app.services import whatsapp
from app.services.whatsapp import WhatsAppError


MIN_CONFIDENCE = 0.55


def _ttl_minutes() -> int:
    return int(current_app.config.get("AI_SESSION_TTL_MINUTES", 30))


def _get_or_open_session(customer_id: int, wa_id: str) -> AiSession:
    now = datetime.utcnow()
    session = (
        AiSession.query.filter_by(customer_id=customer_id, status="parsing")
        .filter(AiSession.expires_at > now)
        .order_by(AiSession.id.desc())
        .first()
    )
    if session is None:
        session = AiSession(
            customer_id=customer_id,
            wa_id=wa_id,
            status="parsing",
            expires_at=now + timedelta(minutes=_ttl_minutes()),
        )
        db.session.add(session)
        db.session.flush()
    return session


def _try_send(wa_id: str, text: str) -> None:
    """Best-effort text — swallow WhatsApp errors so the pipeline doesn't crash on send failures."""
    try:
        whatsapp.send_text(wa_id, text)
    except WhatsAppError as e:
        current_app.logger.warning("send_text failed to %s: %s", wa_id, e)


def _try_send_sticker(wa_id: str, purpose: str) -> None:
    try:
        stickers_svc.send_sticker_by_purpose(wa_id, purpose)
    except WhatsAppError as e:
        current_app.logger.warning("send_sticker %s failed to %s: %s", purpose, wa_id, e)


def _open_handoff(customer_id: int, wa_id: str, reason: str, message_body: str, session_id: int) -> AdminAlert:
    alert = AdminAlert(
        kind="ai_handoff",
        payload_json=json.dumps(
            {"reason": reason, "message": message_body, "session_id": session_id}, ensure_ascii=False
        ),
        customer_id=customer_id,
    )
    db.session.add(alert)
    session = db.session.get(AiSession, session_id)
    if session:
        session.status = "handoff"
    db.session.commit()
    _try_send(wa_id, "🙋 لحظات وحدهيتواصل معاك أحد الفريق دلوقتي.")
    return alert


def process_incoming(customer: Customer, message_body: str) -> dict:
    """Main entry point. Returns a small dict summarising what happened.

    The webhook returns 200 to Meta regardless — this runs after we've
    persisted the raw inbox message.
    """
    if not message_body or not message_body.strip():
        return {"handled": False, "reason": "empty"}

    session = _get_or_open_session(customer.id, customer.wa_id)
    prior = {
        "from": session.partial_pickup_slug,
        "to": session.partial_dropoff_slug,
    }

    result = ai_parser.parse_message(message_body, prior=prior)
    session.touch(_ttl_minutes())

    # True gibberish / API error → human handoff. Low-confidence chat replies
    # are still worth sending, so we only handoff on `unknown` OR when a
    # booking-intent parse is under threshold.
    if result.used_fallback or result.intent == "unknown":
        _open_handoff(
            customer.id, customer.wa_id,
            reason="gemini_error" if result.used_fallback else "unknown_intent",
            message_body=message_body,
            session_id=session.id,
        )
        return {"handled": True, "action": "handoff", "confidence": result.confidence}

    # Conversational Q&A — Gemini answered as a friendly agent. No session
    # state to persist and no booking to create; just relay the reply.
    if result.intent == "chat":
        if result.reply_ar:
            _try_send(customer.wa_id, result.reply_ar)
        return {"handled": True, "action": "chat"}

    # From here on we're in the booking flow — enforce confidence threshold.
    if result.confidence < MIN_CONFIDENCE:
        _open_handoff(
            customer.id, customer.wa_id,
            reason="low_confidence",
            message_body=message_body,
            session_id=session.id,
        )
        return {"handled": True, "action": "handoff", "confidence": result.confidence}

    # Merge new partials
    if result.from_zone_slug:
        session.partial_pickup_slug = result.from_zone_slug
    if result.to_zone_slug:
        session.partial_dropoff_slug = result.to_zone_slug

    if result.intent == "clarify":
        db.session.commit()
        if result.reply_ar:
            _try_send(customer.wa_id, result.reply_ar)
        return {"handled": True, "action": "clarify"}

    # intent == book_ride
    pickup = session.partial_pickup_slug
    dropoff = session.partial_dropoff_slug
    if not pickup or not dropoff:
        db.session.commit()
        # Ask the missing side
        if not pickup and not dropoff:
            _try_send(customer.wa_id, "أهلاً 🌟 من فين لفين؟ اكتب الحيّين بس.")
        elif not pickup:
            _try_send(customer.wa_id, "تمام، وجهتك محفوظة. حضرتك من فين هنعديك؟")
        else:
            _try_send(customer.wa_id, "تمام، إحنا هنعديك من هنا. عايز تنزل فين؟")
        return {"handled": True, "action": "await_partial"}

    # Both zones present — validate they exist
    from_zone = Zone.query.filter_by(slug=pickup, is_active=True).first()
    to_zone = Zone.query.filter_by(slug=dropoff, is_active=True).first()
    if not from_zone or not to_zone:
        # Gemini hallucinated a slug that doesn't exist — handoff to a human.
        _open_handoff(
            customer.id, customer.wa_id,
            reason="unknown_zone",
            message_body=message_body,
            session_id=session.id,
        )
        return {"handled": True, "action": "handoff"}

    # Immediate acknowledgment BEFORE the slower work — customer sees the
    # branded sticker within ~1s of their message instead of waiting on
    # pricing/DB writes/matching.
    _try_send_sticker(customer.wa_id, "booked")

    # Create the ride
    try:
        ride, pending_ids = ride_lifecycle.create_ride(
            customer_id=customer.id,
            from_zone_id=from_zone.id,
            to_zone_id=to_zone.id,
            source="whatsapp",
        )
    except ValueError as e:
        _try_send(customer.wa_id, "معلش، السعر مش موجود للطريق ده. هنراجعه بسرعة.")
        _open_handoff(customer.id, customer.wa_id, reason=str(e), message_body=message_body, session_id=session.id)
        return {"handled": True, "action": "handoff"}

    # Mark AI session complete
    session.status = "completed"
    db.session.commit()

    # Follow-up ack with concrete booking details
    _try_send(
        customer.wa_id,
        f"🚗 تم استلام طلبك!\n"
        f"من: {from_zone.name_ar}\n"
        f"إلى: {to_zone.name_ar}\n"
        f"السعر: {float(ride.price_egp):.0f} ج.م\n"
        f"بندور على كابتن قريب…",
    )

    # Kick off matching (assign() will send captain_coming sticker on assignment)
    matching.start_matching(ride.id, pending_fee_ids=pending_ids)

    return {"handled": True, "action": "ride_created", "ride_id": ride.id}
