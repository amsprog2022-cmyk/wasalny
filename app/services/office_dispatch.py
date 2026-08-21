"""Turn an office WhatsApp message into a dispatched trip.

The office takes the trip on a phone call, then pastes the caller's number
into WhatsApp — usually with a pickup place after it, sometimes with nothing,
and sometimes with the digits reversed by whatever copied them.

The customer whose number was pasted never contacted us, so we never message
them. Every reply in this flow goes back to the office.
"""
from __future__ import annotations

import re
from typing import Optional

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app import db
from app.extensions import get_redis
from app.models.customer import Customer
from app.models.office import OfficeNumber

# Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹) → ASCII.
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})

# Separators the office types inside a number: "+20 100-123 4567", "(010)…".
_SEPARATORS = re.compile(r"[\s\-–—./()]+")
_DIGIT_RUN = re.compile(r"\d{10,}")

# The four Egyptian mobile operators. Anything else is a landline or a typo,
# and dispatching a captain to a landline wastes a trip.
_EG_MOBILE_PREFIX = re.compile(r"^1[0125]\d{8}$")

_OFFICE_CACHE_TTL = 60


# ---------- office number registry ----------

def normalize_wa_id(raw: str) -> Optional[str]:
    """Turn anything a human might type into Meta's wa_id form (201XXXXXXXXX).

    Used both by the inbound parser and by the admin page, so a number added
    as "+20 105 008 4115" can never fail to match the "201050084115" that
    arrives on the webhook.
    """
    digits = _SEPARATORS.sub("", (raw or "").strip()).translate(_DIGIT_MAP)
    digits = re.sub(r"\D", "", digits.lstrip("+"))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("20"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return f"20{digits}" if _EG_MOBILE_PREFIX.match(digits) else None


def is_office(wa_id: str) -> bool:
    """True when this sender is a live office number.

    Cached for a minute — a busy office pastes several trips in a row and
    this sits in the webhook's hot path.
    """
    if not wa_id:
        return False
    key = f"office:is:{wa_id}"
    try:
        r = get_redis(current_app.config.get("REDIS_URL"))
        cached = r.get(key)
        if cached is not None:
            return cached == "1"
    except Exception:  # noqa: BLE001
        r = None

    hit = OfficeNumber.query.filter_by(wa_id=wa_id, is_active=True).first() is not None
    if r is not None:
        try:
            r.setex(key, _OFFICE_CACHE_TTL, "1" if hit else "0")
        except Exception:  # noqa: BLE001
            pass
    return hit


def invalidate_cache(wa_id: str) -> None:
    """Drop the is_office cache so an admin edit takes effect immediately."""
    try:
        get_redis(current_app.config.get("REDIS_URL")).delete(f"office:is:{wa_id}")
    except Exception:  # noqa: BLE001
        pass


# ---------- parsing ----------

def extract_phone(text: str) -> tuple[Optional[str], str]:
    """Pull the customer's number out of the pasted message.

    Returns (wa_id, leftover_text). Deliberately regex-only: a hallucinated
    digit means a captain drives to a stranger, so this must not depend on a
    model. Handles Arabic-Indic digits, separators, a missing or present
    country code, and the reversed-digit paste the office keeps producing.
    """
    if not text:
        return None, ""

    ascii_text = text.translate(_DIGIT_MAP)

    # Prefer natural digit runs first — a message like
    # "فلل ل أهرام ب 50 01016140001" has a price and a phone separated by a
    # space, and collapsing across the gap would merge them into a 13-digit
    # blob that matches nothing. Try uncollapsed here, and only fall back to
    # the collapse trick if no phone-shaped run stands on its own.
    for match in sorted(_DIGIT_RUN.finditer(ascii_text),
                        key=lambda m: len(m.group(0)), reverse=True):
        run = match.group(0)
        wa_id = _to_wa_id(run) or _to_wa_id(run[::-1])
        if wa_id:
            leftover = ascii_text[:match.start()] + " " + ascii_text[match.end():]
            return wa_id, _clean_leftover(leftover)

    # Collapse separators between digits so "0105 008 4115" reads as one run,
    # without gluing together two numbers on separate lines.
    joined = re.sub(r"(?<=\d)[\s\-–—./]+(?=\d)", "", ascii_text)
    for match in sorted(_DIGIT_RUN.finditer(joined),
                        key=lambda m: len(m.group(0)), reverse=True):
        run = match.group(0)
        wa_id = _to_wa_id(run) or _to_wa_id(run[::-1])
        if wa_id:
            leftover = joined[:match.start()] + " " + joined[match.end():]
            return wa_id, _clean_leftover(leftover)

    return None, _clean_leftover(ascii_text)


# Any of the dashes offices type between pickup and dropoff, OR the Arabic
# preposition "ل" (to) surrounded by whitespace — the new office format uses
# "فلل ل أهرام" to mean pickup=فلل, dropoff=أهرام. Whitespace on both sides
# is required so "ل" inside a word (e.g. "الفل") doesn't split the place.
_DEST_SEP = re.compile(r"\s+ل\s+|\s*[-–—_]\s*")
# Trailing integer (Egyptian pound amount). Matches "70", "70 ج.م", "70 جنيه".
# Anchored to end-of-text so a house number mid-address never gets grabbed.
_TRAILING_PRICE = re.compile(
    r"\s*(\d{1,4})\s*(?:ج\s*\.?\s*م|جنيه|جنيها|EGP)?\s*$",
    re.IGNORECASE,
)
# Explicit price marker: "ب 50", "بـ50", "بـ 50 جنيه". The office typed the
# new format as "فلل ل أهرام ب 50 01016140001" — a bare integer on its own
# is ambiguous with a house number, so this marker is what makes it safe.
# Requires whitespace/start before ب so "بنها" and other place names that
# begin with the letter don't get eaten as a price.
_MARKED_PRICE = re.compile(
    r"(?:^|\s)ب\s*ـ?\s*(\d{1,4})\s*(?:ج\s*\.?\s*م|جنيه|جنيها|EGP)?(?=\s|$)",
    re.IGNORECASE,
)


def parse_office_message(text: str) -> dict:
    """Full office paste parser.

    Returns a dict:
        {
          "wa_id": str | None,      # customer phone
          "pickup": str,            # may be ""
          "dropoff": str,           # may be ""
          "price_egp": float | None # None → auto-price from km
        }

    Format the office actually types:
        "فلل ل أهرام ب 50 01016140001"         (pickup ل dropoff ب price phone)
        "01016140001 فلل ل أهرام ب 50"         (phone at start, ل + ب — new)
        "01012818977 سندنهور - بنها 70"        (legacy: dash + trailing price)
        "01012818977 سندنهور"                  (pickup only, price auto)
        "01012818977 - بنها 70"                (dropoff + price, no pickup)
        "01012818977"                          (bare number, blast to captains)

    Any explicit price (marked "ب ..." OR trailing digits) wins over the
    km-based auto-price. The ب-marked form is preferred because a bare
    trailing integer is ambiguous with a house number.
    Any dash character between fields splits pickup from dropoff.
    """
    wa_id, leftover = extract_phone(text)
    if wa_id is None:
        return {"wa_id": None, "pickup": "", "dropoff": "", "price_egp": None}

    body = (leftover or "").strip()
    price: Optional[float] = None

    # "ب 50" marker first — unambiguous, so try it before the trailing
    # integer heuristic. If matched, cut it out anywhere it appears (the
    # office might put it before or after the place name).
    m = _MARKED_PRICE.search(body)
    if m:
        try:
            price = float(m.group(1))
        except ValueError:
            price = None
        if price is not None:
            body = (body[: m.start()] + " " + body[m.end():]).strip(" -–—_/.,،")

    # Trailing price fallback for the legacy format. Skipped if the marked
    # price already fired so we don't double-strip.
    if price is None:
        m = _TRAILING_PRICE.search(body)
        if m:
            try:
                price = float(m.group(1))
            except ValueError:
                price = None
            # Only trim the price token if we successfully parsed it, so
            # "شقة 5" (which would match) still keeps its digits when we
            # can't coerce them cleanly.
            if price is not None:
                body = body[: m.start()].strip(" -–—_/.,،")

    # Split pickup / dropoff on any dash. Missing dash → whole text is pickup.
    parts = _DEST_SEP.split(body, maxsplit=1)
    pickup = (parts[0] or "").strip()
    dropoff = (parts[1] if len(parts) > 1 else "").strip()

    return {"wa_id": wa_id, "pickup": pickup, "dropoff": dropoff,
            "price_egp": price}


def _clean_leftover(text: str) -> str:
    """Whitespace-collapse and drop the punctuation the number left behind.

    A "+2010..." paste leaves a bare "+", which would otherwise be geocoded
    and then stored as the captain's pickup address.
    """
    cleaned = re.sub(r"\s+", " ", text).strip(" +-–—./()،,:")
    return cleaned if re.search(r"\w", cleaned) else ""


def _to_wa_id(run: str) -> Optional[str]:
    if run.startswith("00"):
        run = run[2:]
    if run.startswith("20"):
        run = run[2:]
    if run.startswith("0"):
        run = run[1:]
    return f"20{run}" if _EG_MOBILE_PREFIX.match(run) else None


def clean_place(text: str) -> Optional[str]:
    """Ask Gemini to reduce the leftover text to just the place.

    The office pastes contact names and shorthand along with the location
    ("تامر شركة وصلني", "مهم", "فحص"), which Nominatim chokes on. Gemini only
    ever cleans text here — it never extracts the phone number.

    Returns None when there's no usable place, or when Gemini is unavailable.
    """
    if not text or not current_app.config.get("GEMINI_API_KEY"):
        return None

    from app.services.ai_parser import _call_gemini, _extract_json

    prompt = (
        "أنت بتساعد شركة مواصلات في مدينة بنها المصرية.\n"
        "الرسالة دي مكتوبة من موظف مكتب وفيها اسم مكان الاستلام مخلوط بكلام تاني "
        "زي اسم العميل أو ملاحظات.\n"
        "استخرج اسم المكان في بنها بس، من غير أسماء أشخاص ومن غير ملاحظات.\n"
        'رد بـ JSON بالشكل ده فقط: {"place": "اسم المكان"} '
        'أو {"place": null} لو مفيش مكان واضح.\n\n'
        f"الرسالة: {text}"
    )
    try:
        parsed = _extract_json(_call_gemini(prompt)) or {}
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("office place cleanup failed: %s", e)
        return None

    place = (parsed.get("place") or "").strip()
    return place or None


def resolve_pickup(leftover: str) -> Optional[dict]:
    """Geocode the pasted place inside Benha.

    Returns {"lat", "lng", "label"} or None. Only confident hits count — a
    vague match puts the pin a couple of kilometres off, and a wrong pin is
    worse than no pin when the captain is going to phone the customer anyway.
    """
    if not leftover:
        return None

    from app.services import reverse_geocode as rg

    for query in (leftover, clean_place(leftover)):
        if not query:
            continue
        try:
            hit = rg.geocode_pickup(query)
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("office geocode failed for %r: %s", query, e)
            continue
        if hit and hit.get("confident"):
            return {"lat": hit["lat"], "lng": hit["lng"], "label": hit["label"]}
    return None


# ---------- replies to the office ----------

def _reply(office_wa_id: str, body: str) -> None:
    from app.services import whatsapp
    try:
        whatsapp.send_text(office_wa_id, body)
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning("office reply failed: %s", e)


def notify_office_assigned(ride) -> None:
    """Tell the office which captain took the trip."""
    if not ride.office_wa_id or ride.driver is None:
        return
    d = ride.driver
    car = " ".join(x for x in (d.car_model, d.car_color) if x).strip()
    if d.car_plate:
        car = f"{car} - {d.car_plate}".strip(" -")
    lines = [
        f"الكابتن {d.name or ''} قبل الرحلة".strip(),
        f"موبايل الكابتن: {d.wa_id or '-'}",
    ]
    if car:
        lines.append(f"العربية: {car}")
    if ride.customer is not None:
        lines.append(f"العميل: {ride.customer.wa_id}")
    _reply(ride.office_wa_id, "\n".join(lines))


def notify_office_no_driver(ride) -> None:
    if not ride.office_wa_id:
        return
    phone = ride.customer.wa_id if ride.customer is not None else ""
    _reply(
        ride.office_wa_id,
        f"مفيش كابتن متاح دلوقتي للعميل {phone}. "
        "جرب تبعت الطلب تاني بعد شوية.",
    )


# ---------- pipeline ----------

def handle_office_message(office_wa_id: str, body: str) -> None:
    """Parse one pasted office message and dispatch it. One trip per message.

    Accepted formats (see parse_office_message):
      "01012818977 سندنهور - بنها 70"    (pickup, dropoff, explicit price)
      "01012818977 سندنهور"                (pickup only, auto-price)
      "01012818977"                        (bare number, blast to captains)
    """
    parsed = parse_office_message(body)
    phone = parsed["wa_id"]
    if phone is None:
        _reply(
            office_wa_id,
            "مش قادرين نقرا رقم العميل في الرسالة دي. "
            "ابعت الرقم بصيغة 01xxxxxxxxx ومعاه المكان.",
        )
        return

    customer = _get_or_create_customer(phone)
    pickup_place = resolve_pickup(parsed["pickup"])
    dropoff_place = resolve_pickup(parsed["dropoff"]) if parsed["dropoff"] else None

    from app.services import matching, ride_lifecycle

    try:
        ride, pending_fee_ids = ride_lifecycle.create_ride(
            customer_id=customer.id,
            pickup_lat=(pickup_place["lat"] if pickup_place else None),
            pickup_lng=(pickup_place["lng"] if pickup_place else None),
            dropoff_lat=(dropoff_place["lat"] if dropoff_place else None),
            dropoff_lng=(dropoff_place["lng"] if dropoff_place else None),
            source="office",
            service_kind="private",
            # Pin the visible addresses to what the office typed. Without
            # these overrides, create_ride's reverse-geocode writes whatever
            # Nominatim thinks the coords are — the captain would see
            # "شارع الزراعيه" instead of the office's "فلل".
            pickup_address_override=(parsed["pickup"] or None),
            dropoff_address_override=(parsed["dropoff"] or None),
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.exception("office ride creation failed: %s", e)
        _reply(office_wa_id, "حصل خطأ عندنا وإحنا بنسجل الطلب. جرب تبعته تاني.")
        return

    ride.office_wa_id = office_wa_id

    # Explicit trailing price wins over the km-based auto-quote. Commission
    # is re-derived from the current commission_rate setting so an admin
    # tweak on /pricing is reflected here without a redeploy.
    if parsed["price_egp"] is not None:
        from decimal import Decimal
        from app.services import settings as _settings_svc
        rate = _settings_svc.get_pricing()["commission_rate"]
        override = Decimal(f"{parsed['price_egp']:.2f}")
        ride.price_egp = override
        ride.commission_egp = (override * rate).quantize(Decimal("0.01"))

    db.session.commit()

    current_app.logger.info(
        "office ride %s from %s for %s (pickup_gps=%s dropoff_gps=%s price=%s)",
        ride.id, office_wa_id, phone,
        bool(pickup_place), bool(dropoff_place), parsed["price_egp"],
    )
    matching.start_office_matching(ride.id, pending_fee_ids=pending_fee_ids)


def _get_or_create_customer(wa_id: str) -> Customer:
    customer = Customer.query.filter_by(wa_id=wa_id).first()
    if customer is not None:
        return customer
    customer = Customer(wa_id=wa_id, opted_in=True)
    db.session.add(customer)
    try:
        db.session.commit()
    except IntegrityError:
        # Another worker inserted the same number between our read and write.
        db.session.rollback()
        customer = Customer.query.filter_by(wa_id=wa_id).first()
    return customer
