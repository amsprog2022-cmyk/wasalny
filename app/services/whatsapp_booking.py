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

from app import db, socketio
from app.models.ai_session import AiSession, AdminAlert
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.gemini_call import GeminiCallLog
from app.models.message import Message
from app.models.ride import Ride
from app.models.zone import Zone
from app.services import ai_parser
from app.services import rate_limit
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


def _customer_conversation(customer: Customer) -> Optional[Conversation]:
    """Return the customer's kind=customer conversation. The admin inbox reads
    this to show both sides of the AI dialogue — inbound messages are already
    written by the webhook; outbound AI replies are written here."""
    return (
        Conversation.query.filter_by(customer_id=customer.id, kind="customer").first()
    )


def _persist_outbound(
    customer: Customer,
    body: str,
    msg_type: str = "text",
    wa_message_id: str | None = None,
) -> None:
    """Write an outbound Message row so the admin dashboard sees the AI reply.

    Best-effort — swallows any error so a persistence hiccup never affects
    the customer-facing send, which has already succeeded by the time we
    get here.
    """
    try:
        conv = _customer_conversation(customer)
        if conv is None:
            return
        msg = Message(
            conversation_id=conv.id,
            wa_message_id=wa_message_id,
            direction="outbound",
            msg_type=msg_type,
            body=body,
            status="sent",
        )
        db.session.add(msg)
        conv.last_message_preview = body[:500]
        from datetime import datetime as _dt
        conv.last_message_at = _dt.utcnow()
        db.session.commit()
        # Live-refresh open inbox tabs.
        try:
            socketio.emit(
                "new_message",
                {"conversation": conv.to_dict(), "message": msg.to_dict()},
                namespace="/inbox",
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("inbox emit for AI outbound failed: %s", e)
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("persist AI outbound failed: %s", e)


def _try_send(wa_id: str, text: str, customer: Customer | None = None) -> None:
    """Best-effort text — swallow WhatsApp errors so the pipeline doesn't crash on send failures.

    When `customer` is supplied, also persist the outbound message to the
    conversation inbox so admins can read the AI's replies.
    """
    try:
        resp = whatsapp.send_text(wa_id, text)
        if customer is not None:
            wa_msg_id = (resp.get("messages") or [{}])[0].get("id") if isinstance(resp, dict) else None
            _persist_outbound(customer, text, msg_type="text", wa_message_id=wa_msg_id)
    except WhatsAppError as e:
        current_app.logger.warning("send_text failed to %s: %s", wa_id, e)


def _try_send_sticker(wa_id: str, purpose: str, customer: Customer | None = None) -> None:
    try:
        resp = stickers_svc.send_sticker_by_purpose(wa_id, purpose)
        if customer is not None and resp is not None:
            wa_msg_id = (resp.get("messages") or [{}])[0].get("id") if isinstance(resp, dict) else None
            _persist_outbound(
                customer, f"[sticker: {purpose}]", msg_type="sticker", wa_message_id=wa_msg_id
            )
    except WhatsAppError as e:
        current_app.logger.warning("send_sticker %s failed to %s: %s", purpose, wa_id, e)


def _open_handoff(
    customer: Customer,
    reason: str,
    message_body: str,
    session_id: int,
) -> AdminAlert:
    alert = AdminAlert(
        kind="ai_handoff",
        payload_json=json.dumps(
            {
                "reason": reason,
                "message": message_body,
                "session_id": session_id,
                "partial_pickup_slug": None,
                "partial_dropoff_slug": None,
            },
            ensure_ascii=False,
        ),
        customer_id=customer.id,
    )
    session = db.session.get(AiSession, session_id)
    if session:
        session.status = "handoff"
        # Roll session partials into the alert payload so admins have context
        # when they open the manual-assign dialog.
        payload = json.loads(alert.payload_json)
        payload["partial_pickup_slug"] = session.partial_pickup_slug
        payload["partial_dropoff_slug"] = session.partial_dropoff_slug
        alert.payload_json = json.dumps(payload, ensure_ascii=False)
    db.session.add(alert)
    db.session.commit()
    _try_send(customer.wa_id, "🙋 لحظات وحدهيتواصل معاك أحد الفريق دلوقتي.", customer=customer)
    # Push a live alert to admin dashboards so someone can act immediately.
    try:
        socketio.emit(
            "ai_alert_new",
            {
                "id": alert.id,
                "customer_id": customer.id,
                "customer_wa_id": customer.wa_id,
                "customer_name": customer.name or customer.wa_id,
                "reason": reason,
                "message": message_body,
            },
            namespace="/inbox",
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("ai_alert_new emit failed: %s", e)
    return alert


def _load_active_ride_context(customer_id: int) -> dict | None:
    """Return a dict describing this customer's current in-flight ride so
    the AI can answer questions about it. Returns None when they have none."""
    ride = (
        Ride.query.filter(
            Ride.customer_id == customer_id,
            Ride.status.in_(("broadcasting", "assigned", "started")),
        )
        .order_by(Ride.created_at.desc())
        .first()
    )
    if ride is None:
        return None
    return {
        "id": ride.id,
        "status": ride.status,
        "from_zone_ar": ride.from_zone.name_ar if ride.from_zone else None,
        "to_zone_ar": ride.to_zone.name_ar if ride.to_zone else None,
        "price_egp": float(ride.price_egp),
        "driver_name": ride.driver.name if ride.driver else None,
    }


def _log_gemini_call(
    customer: Customer, latency_ms: int, result: "ai_parser.ParseResult | None",
    *, was_rate_limited: bool = False,
) -> None:
    """Persist observability row. Best-effort; never crashes the pipeline."""
    try:
        row = GeminiCallLog(
            customer_id=customer.id,
            wa_id=customer.wa_id,
            latency_ms=latency_ms,
            intent=(result.intent if result else "rate_limited"),
            confidence=(result.confidence if result else None),
            used_fallback=(result.used_fallback if result else False),
            was_rate_limited=was_rate_limited,
        )
        db.session.add(row)
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("gemini metric log failed: %s", e)


def process_incoming(customer: Customer, message_body: str) -> dict:
    """Main entry point. Returns a small dict summarising what happened.

    The webhook returns 200 to Meta regardless — this runs after we've
    persisted the raw inbox message.
    """
    import time
    if not message_body or not message_body.strip():
        return {"handled": False, "reason": "empty"}

    # Rate-limit per phone — protect Gemini quota from a single abuser.
    allowed, count = rate_limit.check_gemini_limit(customer.wa_id)
    if not allowed:
        current_app.logger.warning(
            "Gemini rate limit exceeded for %s (count=%d)", customer.wa_id, count
        )
        _log_gemini_call(customer, latency_ms=0, result=None, was_rate_limited=True)
        _try_send(
            customer.wa_id,
            "🙏 معلش يا فندم، وصلت للحد الأقصى من الرسائل في الساعة دي. "
            "استنى شوية وحاول تاني.",
            customer=customer,
        )
        return {"handled": True, "action": "rate_limited"}

    session = _get_or_open_session(customer.id, customer.wa_id)
    prior = {
        "from": session.partial_pickup_slug,
        "to": session.partial_dropoff_slug,
    }
    active_ride = _load_active_ride_context(customer.id)

    t0 = time.time()
    result = ai_parser.parse_message(message_body, prior=prior, active_ride=active_ride)
    latency_ms = int((time.time() - t0) * 1000)
    session.touch(_ttl_minutes())
    _log_gemini_call(customer, latency_ms=latency_ms, result=result)

    # True gibberish / API error → human handoff. Low-confidence chat replies
    # are still worth sending, so we only handoff on `unknown` OR when a
    # booking-intent parse is under threshold.
    if result.used_fallback or result.intent == "unknown":
        _open_handoff(
            customer,
            reason="gemini_error" if result.used_fallback else "unknown_intent",
            message_body=message_body,
            session_id=session.id,
        )
        return {"handled": True, "action": "handoff", "confidence": result.confidence}

    # In-trip status question — AI already has ride context and composed a reply.
    if result.intent == "ride_status":
        if result.reply_ar:
            _try_send(customer.wa_id, result.reply_ar, customer=customer)
        elif active_ride:
            _try_send(
                customer.wa_id,
                f"🚗 حالة رحلتك: {active_ride['status']} من {active_ride['from_zone_ar']} "
                f"إلى {active_ride['to_zone_ar']}",
                customer=customer,
            )
        return {"handled": True, "action": "ride_status"}

    # Cancel the customer's active ride
    if result.intent == "cancel_ride":
        if active_ride is None:
            _try_send(customer.wa_id, "🙂 مش لاقيلك رحلة نشطة دلوقتي.", customer=customer)
            return {"handled": True, "action": "cancel_noop"}
        try:
            ride = db.session.get(Ride, active_ride["id"])
            ride_lifecycle.cancel(ride, actor="customer", reason="whatsapp_cancel")
            _try_send(customer.wa_id, result.reply_ar or "✅ اتلغت الرحلة. سلامات!", customer=customer)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("cancel via whatsapp failed: %s", e)
            _open_handoff(customer, reason="cancel_failed",
                          message_body=message_body, session_id=session.id)
        return {"handled": True, "action": "cancel_ride"}

    # File a complaint — creates admin alert + queues Complaint if models exist
    if result.intent == "complaint":
        summary = result.complaint_summary or message_body
        alert = AdminAlert(
            kind="complaint",
            payload_json=json.dumps(
                {
                    "summary": summary,
                    "message": message_body,
                    "ride_id": active_ride["id"] if active_ride else None,
                },
                ensure_ascii=False,
            ),
            customer_id=customer.id,
        )
        db.session.add(alert)
        db.session.commit()
        _try_send(
            customer.wa_id,
            result.reply_ar or "🙏 آسفين على اللي حصل. الشكوى راحت للإدارة وحد هيرد عليك قريب.",
            customer=customer,
        )
        return {"handled": True, "action": "complaint"}

    # Conversational Q&A — Gemini answered as a friendly agent. No session
    # state to persist and no booking to create; just relay the reply.
    if result.intent == "chat":
        if result.reply_ar:
            _try_send(customer.wa_id, result.reply_ar, customer=customer)
        return {"handled": True, "action": "chat"}

    # Merge whatever partials Gemini extracted from this turn.
    if result.from_zone_slug:
        session.partial_pickup_slug = result.from_zone_slug
    if result.to_zone_slug:
        session.partial_dropoff_slug = result.to_zone_slug

    # Explicit clarify path — Gemini decided we need one more question. Skip
    # the confidence gate here: the AI is deliberately asking to disambiguate,
    # so we always want the friendly follow-up, not a handoff.
    if result.intent == "clarify":
        session.clarify_count = (session.clarify_count or 0) + 1
        if session.clarify_count > 2:
            db.session.commit()
            _open_handoff(customer, reason="clarify_exhausted",
                          message_body=message_body, session_id=session.id)
            return {"handled": True, "action": "handoff"}
        db.session.commit()
        _try_send(
            customer.wa_id,
            result.reply_ar or "🌟 تحب تروح فين؟",
            customer=customer,
        )
        return {"handled": True, "action": "clarify"}

    # From here on we're in the booking flow — enforce confidence threshold.
    if result.confidence < MIN_CONFIDENCE:
        _open_handoff(
            customer,
            reason="low_confidence",
            message_body=message_body,
            session_id=session.id,
        )
        return {"handled": True, "action": "handoff", "confidence": result.confidence}

    # Short chatbot flow: at most one follow-up. Anything after that goes
    # to an admin so we don't loop-badger the customer.
    pickup_slug = session.partial_pickup_slug
    dropoff_slug = session.partial_dropoff_slug

    # We only need a pickup zone to fire an order — if we have it, we book,
    # even if the destination is missing or unknown. Captain agrees on
    # destination + price verbally on arrival for WhatsApp rides.
    if pickup_slug:
        from_zone = Zone.query.filter_by(slug=pickup_slug, is_active=True).first()
        if from_zone is None:
            # Gemini returned a slug that no longer maps to an active zone —
            # can't book, escalate.
            _open_handoff(
                customer, reason="unknown_zone",
                message_body=message_body, session_id=session.id,
            )
            return {"handled": True, "action": "handoff"}

        # Optional destination — if the AI matched it to a registered zone,
        # use it for pre-priced booking. Otherwise book WhatsApp-style
        # (deferred pricing).
        to_zone = None
        if dropoff_slug:
            to_zone = Zone.query.filter_by(slug=dropoff_slug, is_active=True).first()

        _try_send_sticker(customer.wa_id, "booked", customer=customer)

        try:
            ride, pending_ids = ride_lifecycle.create_ride(
                customer_id=customer.id,
                from_zone_id=from_zone.id,
                to_zone_id=(to_zone.id if to_zone else None),
                source="whatsapp",
            )
        except ValueError as e:
            _try_send(
                customer.wa_id,
                "معلش، حصلت مشكلة صغيرة. هنراجع الطلب بسرعة.",
                customer=customer,
            )
            _open_handoff(customer, reason=str(e), message_body=message_body, session_id=session.id)
            return {"handled": True, "action": "handoff"}

        session.status = "completed"
        db.session.commit()

        if to_zone:
            ack = (
                f"🚗 تمام! بندور على كابتن قريب من {from_zone.name_ar} "
                f"لينزلك في {to_zone.name_ar}."
            )
        else:
            ack = (
                f"🚗 تمام! بندور على كابتن قريب من {from_zone.name_ar}.\n"
                f"الكابتن أول ما يوصل هيتفق معاك على الوجهة والسعر."
            )
        _try_send(customer.wa_id, ack, customer=customer)

        matching.start_matching(ride.id, pending_fee_ids=pending_ids)
        return {"handled": True, "action": "ride_created", "ride_id": ride.id}

    # No pickup yet — one polite follow-up, then escalate.
    session.clarify_count = (session.clarify_count or 0) + 1
    if session.clarify_count > 2:
        db.session.commit()
        _open_handoff(
            customer, reason="clarify_exhausted",
            message_body=message_body, session_id=session.id,
        )
        return {"handled": True, "action": "handoff"}

    db.session.commit()
    if dropoff_slug and Zone.query.filter_by(slug=dropoff_slug, is_active=True).first():
        # We know where they want to go — ask only for pickup.
        prompt = "تمام 🚗 وحضرتك دلوقتي فين؟ ابعتلي اسم الحي بس."
    else:
        # Nothing usable — friendly open question, destination first per spec.
        prompt = result.reply_ar or "أهلاً 🌟 تحب تروح فين؟"
    _try_send(customer.wa_id, prompt, customer=customer)
    return {"handled": True, "action": "await_info"}
