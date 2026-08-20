"""WhatsApp booking pipeline — customer message → Gemini → GPS ride created.

Called from the webhook whenever an inbound customer message lands. This is
the second entry point into `create_ride`; the first is the mobile app.

Flow (Phase 3.1, GPS-first):
  1. Reuse or open an AiSession for this wa_id.
  2. If the message is a WhatsApp location pin, treat it as confident pickup
     coords immediately (skip Gemini).
  3. Cancel keyword check first — if the customer has an active broadcasting
     ride and says "إلغاء" / "الغي" / "cancel", cancel and confirm.
  4. Otherwise ask Gemini to parse the message. Gemini returns pickup_text
     and dropoff_text (free-form Arabic place names).
  5. Geocode dropoff_text best-effort (any hit is fine — captain confirms
     with customer on arrival).
  6. Geocode pickup_text with `geocode_pickup` — confident hits (landmarks,
     tight bbox, single result) become the pickup coords. Ambiguous hits
     trigger a "please send a 📍 pin" reply instead of a bad match.
  7. Once we have pickup coords + at least a dropoff text/coords → call
     ride_lifecycle.create_ride with lat/lng and send a proactive
     "بندور على كابتن" WhatsApp reply.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Optional

from flask import current_app

from app import db, socketio
from app.models.ai_session import AiSession, AdminAlert
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.gemini_call import GeminiCallLog
from app.models.intercity_request import IntercityRequest
from app.models.message import Message
from app.models.ride import Ride
from app.services import ai_parser
from app.services import rate_limit
from app.services import reverse_geocode as rg
from app.services import ride_lifecycle
from app.services import matching
from app.services import service_requests
from app.services import stickers as stickers_svc
from app.services import whatsapp
from app.services import whatsapp_menu as wa_menu
from app.services.whatsapp import WhatsAppError


MIN_CONFIDENCE = 0.55

# Customer said "cancel my ride" via WhatsApp. Broad enough to catch common
# Egyptian spellings without matching casual chit-chat.
_CANCEL_KEYWORDS_RE = re.compile(
    r"^\s*(الغاء|إلغاء|الغي|إلغي|كنسل|cancel)\s*$",
    re.IGNORECASE,
)


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
        db.session.rollback()
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
        # when they open the manual-assign dialog. Include the new GPS
        # partial fields alongside the legacy slugs.
        payload = json.loads(alert.payload_json)
        payload["partial_pickup_slug"] = session.partial_pickup_slug
        payload["partial_dropoff_slug"] = session.partial_dropoff_slug
        payload["partial_pickup_text"] = session.partial_pickup_text
        payload["partial_pickup_lat"] = session.partial_pickup_lat
        payload["partial_pickup_lng"] = session.partial_pickup_lng
        payload["partial_dropoff_text"] = session.partial_dropoff_text
        payload["partial_dropoff_lat"] = session.partial_dropoff_lat
        payload["partial_dropoff_lng"] = session.partial_dropoff_lng
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
    the AI can answer questions about it. Returns None when they have none.

    Prefers reverse-geocoded street addresses over zone names since GPS
    bookings may leave to_zone_id NULL when the destination is captain-set.
    """
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
    from_label = (
        ride.pickup_address
        or (ride.from_zone.name_ar if ride.from_zone else None)
    )
    to_label = (
        ride.dropoff_address
        or (ride.to_zone.name_ar if ride.to_zone else None)
    )
    return {
        "id": ride.id,
        "status": ride.status,
        "from_zone_ar": from_label,
        "to_zone_ar": to_label,
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
        # Roll back so a failed metrics write doesn't poison the session for
        # the rest of the pipeline — the ride itself matters far more.
        db.session.rollback()
        current_app.logger.warning("gemini metric log failed: %s", e)


def _try_book_ride(customer: Customer, session: AiSession) -> Optional[dict]:
    """Fire ride_lifecycle.create_ride when we have enough session state.

    Always requires both pickup coords AND a destination the customer
    typed. Private rides now get their destination up-front (auto-priced,
    dispatched to the captain from → to just like an app booking) rather
    than deferring to the captain-on-arrival flow. Non-private kinds stay
    the same — the admin needs both ends to dispatch by hand.

    Returns a small dict on success, or None if we still need more info.
    """
    if session.partial_pickup_lat is None or session.partial_pickup_lng is None:
        return None

    kind = session.service_kind or "private"
    is_private = kind == "private"

    # Both trip kinds now need a typed destination. Geocoded coords are
    # nice-to-have — if the geocoder missed on private, we still book
    # (captain will confirm with the customer at pickup) but the address
    # string is required so the captain knows where he's going.
    if not session.partial_dropoff_text:
        return None

    _try_send_sticker(customer.wa_id, "booked", customer=customer)

    try:
        ride, pending_ids = ride_lifecycle.create_ride(
            customer_id=customer.id,
            pickup_lat=session.partial_pickup_lat,
            pickup_lng=session.partial_pickup_lng,
            dropoff_lat=session.partial_dropoff_lat,
            dropoff_lng=session.partial_dropoff_lng,
            source="whatsapp",
            service_kind=kind,
        )
    except ValueError as e:
        _try_send(
            customer.wa_id,
            "معلش، حصلت مشكلة في إنشاء الرحلة. هنراجع الطلب بسرعة.",
            customer=customer,
        )
        current_app.logger.warning("whatsapp create_ride failed: %s", e)
        return {"handled": True, "action": "create_failed"}

    session.status = "completed"
    db.session.commit()

    if is_private:
        # Proactive "we're searching" message — matches the plan-approved UX
        # decision so the customer isn't left in silence for 2-3 min. Also
        # quotes the calculated price up-front when both ends are geocoded,
        # so the customer knows what they'll pay before the captain arrives.
        parts = [f"تمام. بندور على كابتن قريب من {ride.pickup_address or 'مكانك'}"]
        if ride.dropoff_address:
            parts.append(f" لينزلك في {ride.dropoff_address}")
        parts.append(".")
        if ride.price_egp and float(ride.price_egp) > 0:
            parts.append(f"\nسعر الرحلة: {float(ride.price_egp):.0f} ج.م")
        parts.append(" 🚗")
        _try_send(customer.wa_id, "".join(parts), customer=customer)
        matching.start_matching(ride.id, pending_fee_ids=pending_ids)
    else:
        dest = ride.dropoff_address or session.partial_dropoff_text
        ack = (
            f"✅ تمام. طلب {wa_menu.label_ar(kind)} اتسجل.\n"
            f"من: {ride.pickup_address or 'مكانك'}\n"
            f"إلى: {dest}\n\n"
            "طلبك راح للإدارة دلوقتي وهنبعتلك كابتن ونبلغك بسعره في أقرب وقت. "
            "لو حبيت تلغي اكتب: إلغاء"
        )
        _try_send(customer.wa_id, ack, customer=customer)
        service_requests.queue_service_request(ride)

    return {"handled": True, "action": "ride_created", "ride_id": ride.id,
            "service_kind": kind}


def _next_question(session: AiSession) -> str:
    """The one thing we still need from the customer, phrased for their
    chosen service. Pickup always comes first, then destination — every
    kind (private included) now needs both before we dispatch."""
    if session.partial_pickup_lat is None or session.partial_pickup_lng is None:
        return (
            "📍 حضرتك فين دلوقتي؟\n"
            "اكتبلي اسم المكان، أو ابعت موقعك من الواتس بالخطوات دي:\n"
            "دوس على 📎 المرفقات ← الموقع ← ابعت موقعك الحالي."
        )
    if (session.service_kind or "private") == "delivery":
        return "الطلب رايح فين؟ اكتبلي العنوان. 📦"
    return "رايح فين؟ اكتبلي اسم المكان. 🏁"


def _apply_service_choice(
    customer: Customer, session: AiSession, kind: str
) -> dict:
    """Lock the chosen service onto the session and move to the next step.

    A pin may already have arrived before the customer picked, so we try
    to book immediately rather than asking a question we know the answer to.
    """
    session.service_kind = kind
    session.clarify_count = 0
    db.session.commit()

    # Travel outside Benha never becomes a ride — no pin, no price, no
    # captain. All we need is the customer's own wording for the admin who
    # calls back, so ask one open question and stop here.
    if kind == "intercity":
        _try_send(customer.wa_id, "منين لفين؟", customer=customer)
        return {"handled": True, "action": "intercity_asked"}

    booked = _try_book_ride(customer, session)
    if booked:
        return booked

    # Reassure at the moment of choice rather than only after the pin
    # arrives — otherwise the pickup question sits there feeling ignored.
    _try_send_sticker(customer.wa_id, "booked", customer=customer)
    _try_send(
        customer.wa_id,
        f"بنشوف اقرب كابتن لحضرتك.\n\n{_next_question(session)}",
        customer=customer,
    )
    return {"handled": True, "action": "service_chosen", "service_kind": kind}


def _send_service_numbers(customer: Customer, session: AiSession) -> dict:
    """Reply with the depot phone numbers admins keep on /office-numbers.

    Ends the AiSession — this row doesn't create a ride, we're just
    handing the customer a list so they call the depot themselves.
    """
    from app.models.office import ServiceNumber

    numbers = (
        ServiceNumber.query.filter_by(is_active=True)
        .order_by(ServiceNumber.id.asc()).all()
    )
    if numbers:
        lines = ["لخدمات وصلني الاخري:", ""]
        for n in numbers:
            lines.append(n.service_label)
            lines.append(n.phone)
            lines.append("")
        lines.append("كلمنا مباشر واتس أو اتصال علي الأرقام دى.")
        body = "\n".join(lines)
    else:
        body = (
            "معلش، لسه مفيش أرقام مسجلة للخدمات دي. "
            "لو حابب حد من الإدارة يكلمك ابعت رقم 4."
        )
    _try_send(customer.wa_id, body, customer=customer)
    session.status = "completed"
    db.session.commit()
    return {"handled": True, "action": "service_numbers_sent"}


def _file_inquiry(customer: Customer, session: AiSession) -> dict:
    """Queue an admin callback for a general inquiry.

    No ride, no captain, no phone list — just an AdminAlert(kind="inquiry")
    with the customer's info so someone on the admin dashboard calls back.
    """
    alert = AdminAlert(
        kind="inquiry",
        payload_json=json.dumps({
            "wa_id": customer.wa_id,
            "customer_name": customer.name or "",
        }, ensure_ascii=False),
        customer_id=customer.id,
    )
    db.session.add(alert)
    session.status = "completed"
    db.session.commit()

    _try_send(
        customer.wa_id,
        "سيتم التواصل معكم من احد افراد الإدارة.",
        customer=customer,
    )
    try:
        socketio.emit(
            "ai_alert_new",
            {
                "id": alert.id,
                "customer_id": customer.id,
                "customer_wa_id": customer.wa_id,
                "customer_name": customer.name or customer.wa_id,
                "reason": "inquiry",
                "message": "استفسار",
            },
            namespace="/inbox",
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("inquiry alert emit failed: %s", e)
    return {"handled": True, "action": "inquiry_filed", "alert_id": alert.id}


def _file_intercity_request(customer: Customer, session: AiSession, text: str) -> dict:
    """Park a "سفر خارج بنها" enquiry on the admin board.

    Stored verbatim: Gemini never sees this branch because the admin needs
    the customer's own phrasing to quote a price over the phone.
    """
    db.session.add(IntercityRequest(
        customer_id=customer.id,
        wa_id=customer.wa_id,
        raw_text=text,
        source="whatsapp",
    ))
    session.status = "completed"
    db.session.commit()
    _try_send(customer.wa_id, "حد من الادارة هيتواصل مع حضرتك.", customer=customer)
    return {"handled": True, "action": "intercity_filed"}


def _apply_pickup_from_pin(session: AiSession, lat: float, lng: float) -> None:
    """Location-pin messages are treated as confident pickup coords."""
    session.partial_pickup_lat = lat
    session.partial_pickup_lng = lng
    session.partial_pickup_text = None  # pin trumps any earlier text guess


def _resolve_pickup_from_text(session: AiSession, text: str) -> tuple[bool, str | None]:
    """Try to geocode a free-text pickup. Returns (resolved, label).

    Any hit (confident or ambiguous) is treated as good enough — the
    captain calls the customer as soon as he accepts, so a "close-ish"
    starting point plus a phone call beats forcing the customer to
    figure out how to send a WhatsApp location pin. If our search
    turns up nothing at all, THEN we ask for the pin.

    Falls back to /search-places if the confidence-scored geocoder
    misses — search accepts noisier queries like short place names
    ("الفل", "الإعزام") that geocode_pickup rejects.
    """
    if not text or not text.strip():
        return False, None
    session.partial_pickup_text = text
    # Scope to Benha — same reasoning as _resolve_dropoff_from_text below.
    q = text if "بنها" in text else f"{text} بنها"
    hit = rg.geocode_pickup(q)
    if hit is not None:
        session.partial_pickup_lat = hit["lat"]
        session.partial_pickup_lng = hit["lng"]
        return True, hit.get("label")
    # Nothing from geocode_pickup — try the broader search as a fallback.
    results = rg.search_places(q, limit=1)
    if results:
        session.partial_pickup_lat = results[0]["lat"]
        session.partial_pickup_lng = results[0]["lng"]
        return True, results[0].get("label")
    return False, None


def _resolve_dropoff_from_text(session: AiSession, text: str) -> None:
    """Geocode dropoff best-effort. Any hit is fine — the captain confirms
    with the customer at pickup, and pricing is coord-based only when we
    have both ends. Missing dropoff coords just falls back to captain-set.

    We scope the search to Benha: if the customer typed just "منشية" we
    query "منشية بنها" so Nominatim doesn't return a hit in Alexandria.
    Nothing added when the text already mentions Benha.
    """
    session.partial_dropoff_text = text
    q = text if "بنها" in text else f"{text} بنها"
    results = rg.search_places(q, limit=1)
    if results:
        session.partial_dropoff_lat = results[0]["lat"]
        session.partial_dropoff_lng = results[0]["lng"]


def process_incoming(customer: Customer, payload) -> dict:
    """Main entry point. `payload` is one of:
      {"kind": "text", "body": "<user message>"}
      {"kind": "location", "lat": <float>, "lng": <float>}

    Kept string-tolerant for backward compat (old webhook path) — a raw
    string is treated as a text message.

    Returns a small dict summarising what happened. The webhook returns 200
    to Meta regardless — this runs in a greenlet after inbox persistence.
    """
    import time

    # Backward compat: earlier callers passed a raw string.
    if isinstance(payload, str):
        payload = {"kind": "text", "body": payload}
    if not isinstance(payload, dict):
        return {"handled": False, "reason": "bad_payload"}

    kind = payload.get("kind")
    chosen_kind: str | None = None
    chosen_info: str | None = None
    if kind == "text":
        message_body = (payload.get("body") or "").strip()
        if not message_body:
            return {"handled": False, "reason": "empty"}
    elif kind == "location":
        try:
            pin_lat = float(payload["lat"])
            pin_lng = float(payload["lng"])
        except (KeyError, TypeError, ValueError):
            return {"handled": False, "reason": "bad_location"}
        message_body = f"📍 {pin_lat:.6f},{pin_lng:.6f}"
    elif kind == "menu_choice":
        row_id = (payload.get("row_id") or "").strip()
        chosen_kind = wa_menu.kind_from_row_id(row_id)
        chosen_info = wa_menu.info_action_from_row_id(row_id)
        if chosen_kind is None and chosen_info is None:
            return {"handled": False, "reason": "unknown_row"}
        label = (wa_menu.label_ar(chosen_kind) if chosen_kind
                 else wa_menu.info_label_ar(chosen_info))
        message_body = f"[{label}]"
    else:
        return {"handled": False, "reason": "unknown_kind"}

    active_ride = _load_active_ride_context(customer.id)

    # Cancel keyword — checked FIRST so a customer can bail out even if the
    # AI is confused. Only fires when they actually have a live ride.
    if (
        kind == "text"
        and active_ride is not None
        and active_ride["status"] in ("broadcasting", "assigned")
        and _CANCEL_KEYWORDS_RE.match(message_body)
    ):
        try:
            ride = db.session.get(Ride, active_ride["id"])
            ride_lifecycle.cancel(ride, actor="customer", reason="whatsapp_cancel")
            _try_send(customer.wa_id, "✅ تمام، الرحلة اتلغت. سلامات.", customer=customer)
            return {"handled": True, "action": "cancel_keyword"}
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("cancel keyword failed: %s", e)
            # Fall through to normal handling if cancel errored.

    session = _get_or_open_session(customer.id, customer.wa_id)
    session.touch(_ttl_minutes())

    # MENU TAP — the customer picked a row off the interactive list. Info
    # rows (services / inquiry) short-circuit before we ever touch the ride
    # pipeline, so they don't create an AiSession.service_kind.
    if kind == "menu_choice":
        if chosen_info == "services":
            return _send_service_numbers(customer, session)
        if chosen_info == "inquiry":
            return _file_inquiry(customer, session)
        return _apply_service_choice(customer, session, chosen_kind)

    # INTERCITY — the customer already answered "منين لفين؟". Whatever they
    # typed is the request, so it goes straight to the admin board without
    # ever reaching Gemini or the ride pipeline. A pin here is useless to
    # the admin (there's no route to price), so we re-ask instead.
    if session.service_kind == "intercity":
        if kind == "text":
            return _file_intercity_request(customer, session, message_body)
        _try_send(customer.wa_id, "منين لفين؟ اكتبهالي.", customer=customer)
        return {"handled": True, "action": "intercity_reask"}

    # SERVICE GATE — every conversation starts by picking one of the four
    # services. Until that's on the session we don't call Gemini at all;
    # we just show the menu. Customers mid-ride skip the gate so they can
    # still ask "فين الكابتن؟" or cancel without re-picking a service.
    if not session.service_kind and active_ride is None:
        if kind == "location":
            # Pin arrived before they chose — keep it, then ask.
            _apply_pickup_from_pin(session, pin_lat, pin_lng)
            db.session.commit()
            wa_menu.send_service_menu(customer)
            return {"handled": True, "action": "menu_sent"}

        typed_info = wa_menu.match_info_action(message_body)
        if typed_info == "services":
            return _send_service_numbers(customer, session)
        if typed_info == "inquiry":
            return _file_inquiry(customer, session)

        typed = wa_menu.match_service_kind(message_body)
        if typed:
            return _apply_service_choice(customer, session, typed)

        db.session.commit()
        wa_menu.send_service_menu(customer)
        return {"handled": True, "action": "menu_sent"}

    # Rate-limit per phone — protect Gemini quota from a single abuser.
    # Only text reaches Gemini; pins and menu taps never do.
    if kind == "text":
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

    # LOCATION PIN — trust it. Attach as pickup and try to book right away
    # (if we already have a dropoff from a prior turn we're good to go).
    if kind == "location":
        _apply_pickup_from_pin(session, pin_lat, pin_lng)
        db.session.commit()
        booked = _try_book_ride(customer, session)
        if booked:
            return booked
        # We have pickup pin but still no destination — ask for it.
        _try_send(customer.wa_id, _next_question(session), customer=customer)
        return {"handled": True, "action": "await_dropoff"}

    # TEXT — hand to Gemini.
    prior = {
        "from": session.partial_pickup_text or session.partial_pickup_slug,
        "to":   session.partial_dropoff_text or session.partial_dropoff_slug,
    }
    t0 = time.time()
    result = ai_parser.parse_message(
        message_body,
        prior=prior,
        active_ride=active_ride,
        service_kind=session.service_kind,
    )
    latency_ms = int((time.time() - t0) * 1000)
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
                f"حالة رحلتك: {active_ride['status']} من {active_ride['from_zone_ar']} "
                f"إلى {active_ride['to_zone_ar']} 🚗",
                customer=customer,
            )
        return {"handled": True, "action": "ride_status"}

    # Explicit AI-driven cancel intent (broader than the keyword regex).
    if result.intent == "cancel_ride":
        if active_ride is None:
            _try_send(customer.wa_id, "🙂 مش لاقيلك رحلة نشطة دلوقتي.", customer=customer)
            return {"handled": True, "action": "cancel_noop"}
        try:
            ride = db.session.get(Ride, active_ride["id"])
            ride_lifecycle.cancel(ride, actor="customer", reason="whatsapp_cancel")
            _try_send(customer.wa_id, result.reply_ar or "✅ اتلغت الرحلة. سلامات.", customer=customer)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("cancel via whatsapp failed: %s", e)
            _open_handoff(customer, reason="cancel_failed",
                          message_body=message_body, session_id=session.id)
        return {"handled": True, "action": "cancel_ride"}

    # File a complaint — creates admin alert.
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

    # Conversational Q&A — just relay the AI's friendly reply.
    if result.intent == "chat":
        if result.reply_ar:
            _try_send(customer.wa_id, result.reply_ar, customer=customer)
        return {"handled": True, "action": "chat"}

    # Merge whatever partials Gemini extracted from this turn. Text fields
    # are the primary source; slugs are kept for observability only.
    if result.from_zone_slug:
        session.partial_pickup_slug = result.from_zone_slug
    if result.to_zone_slug:
        session.partial_dropoff_slug = result.to_zone_slug
    if result.dropoff_text:
        _resolve_dropoff_from_text(session, result.dropoff_text)

    # Fallback for the "بنها" case: pickup already pinned, we've clearly
    # asked "رايح فين؟", customer replied with a short place name, but
    # Gemini didn't pull it into dropoff_text. Use the raw message as the
    # destination text so the customer doesn't get stuck in a loop of the
    # bot asking the same question after every one-word answer.
    if (session.partial_pickup_lat is not None
            and not session.partial_dropoff_text
            and message_body
            and 2 <= len(message_body) <= 60):
        _resolve_dropoff_from_text(session, message_body)

    pickup_confident = (
        session.partial_pickup_lat is not None and session.partial_pickup_lng is not None
    )
    pickup_ambiguous_label: str | None = None
    if not pickup_confident and result.pickup_text:
        pickup_confident, pickup_ambiguous_label = _resolve_pickup_from_text(
            session, result.pickup_text,
        )

    # Fallback for one-word replies where Gemini didn't extract pickup_text.
    # Customer wrote e.g. "الفل!" or "الإعزام" after we asked "حضرتك فين
    # دلوقتي؟" — before this fallback, that reply just bounced back the same
    # question until the clarify counter blew and it went to handoff. Now we
    # try /search-places on the raw message and dispatch on any hit.
    if (not pickup_confident
            and session.partial_pickup_lat is None
            and message_body
            and 2 <= len(message_body) <= 60):
        pickup_confident, pickup_ambiguous_label = _resolve_pickup_from_text(
            session, message_body,
        )

    # Try to book BEFORE the clarify handler — the fallback above may have
    # just filled in the missing dropoff, in which case Gemini's "I need
    # more info" verdict is stale. Book if we now have everything;
    # otherwise fall through to the clarify reply.
    if pickup_confident:
        booked = _try_book_ride(customer, session)
        if booked:
            return booked

    # Explicit clarify path — Gemini decided we need one more question.
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
            result.reply_ar or _next_question(session),
            customer=customer,
        )
        return {"handled": True, "action": "clarify"}

    # Booking flow — enforce confidence threshold on the AI parse itself.
    if result.confidence < MIN_CONFIDENCE:
        _open_handoff(
            customer,
            reason="low_confidence",
            message_body=message_body,
            session_id=session.id,
        )
        return {"handled": True, "action": "handoff", "confidence": result.confidence}

    # Pickup confidently geocoded → book straight away.
    if pickup_confident:
        booked = _try_book_ride(customer, session)
        if booked:
            return booked

    # Genuine geocode miss (both geocode_pickup + search returned nothing).
    # Ask for the pin or a more specific place name — this is the rare
    # case now that _resolve_pickup_from_text accepts any hit.
    if result.pickup_text and not pickup_confident:
        db.session.commit()
        _try_send(
            customer.wa_id,
            f"مش قادر ألاقي '{result.pickup_text}' في بنها. "
            "جرب تكتب اسم مكان معروف قريب منك (مثال: جامعة بنها، شبرا الشورى)، "
            "أو ابعت موقعك: 📎 المرفقات ← الموقع ← ابعت موقعك الحالي.",
            customer=customer,
        )
        return {"handled": True, "action": "await_pin"}

    # No pickup at all yet — polite follow-up, cap the loops.
    session.clarify_count = (session.clarify_count or 0) + 1
    if session.clarify_count > 2:
        db.session.commit()
        _open_handoff(customer, reason="clarify_exhausted",
                      message_body=message_body, session_id=session.id)
        return {"handled": True, "action": "handoff"}
    db.session.commit()
    _try_send(customer.wa_id, _next_question(session), customer=customer)
    return {"handled": True, "action": "await_info"}
