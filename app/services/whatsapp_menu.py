"""WhatsApp service menu + app-promo copy.

Every WhatsApp conversation now opens with a tappable list of the four
service kinds. Whichever the customer picks is stashed on the AiSession
and drives the rest of the booking:

  private  → auto-broadcast to the nearest captains (the original flow)
  others   → queued to the admin dispatch board

Customers on old WhatsApp builds see the list as plain text and type
their answer instead, so `match_service_kind` also accepts the number,
the Arabic name, and common misspellings.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from flask import current_app

from app import db
from app.models.customer import Customer
from app.models.driver import SERVICE_KIND_LABELS_AR
from app.services.reverse_geocode import _normalize

ANDROID_URL = "https://play.google.com/store/apps/details?id=com.wassalny.wassalny_customer"
IOS_URL = "https://apps.apple.com/eg/app/wasalny-benha/id6792322962"

# Row IDs come back verbatim in the webhook's list_reply payload.
ROW_ID_PREFIX = "svc_"

MENU_ROWS = [
    {"id": f"{ROW_ID_PREFIX}private",  "kind": "private",  "title": "🚗 ملاكي",           "description": "عربية خاصة توصلك لأي مكان"},
    {"id": f"{ROW_ID_PREFIX}suzuki",   "kind": "suzuki",   "title": "🛺 سوزوكي",          "description": "توك توك للمشاوير القريبة"},
    {"id": f"{ROW_ID_PREFIX}delivery", "kind": "delivery", "title": "🏍️ دليفري موتوسيكل", "description": "توصيل طلبات وأغراض"},
    {"id": f"{ROW_ID_PREFIX}vip",      "kind": "vip",      "title": "✨ VIP",             "description": "عربية فاخرة وخدمة مميزة"},
]

_ROW_ID_TO_KIND = {r["id"]: r["kind"] for r in MENU_ROWS}

# Typed fallbacks. Keys are already Arabic-normalized + lowercased.
_TEXT_ALIASES = {
    "private":  ["1", "١", "ملاكي", "ملاكى", "عربيه", "عربية", "خاصه", "تاكسي", "taxi", "private", "car"],
    "suzuki":   ["2", "٢", "سوزوكي", "سوزوكى", "توك توك", "توكتوك", "تكتك", "suzuki", "tuktuk"],
    "delivery": ["3", "٣", "دليفري", "ديليفري", "توصيل", "موتوسيكل", "موتسيكل", "delivery"],
    "vip":      ["4", "٤", "vip", "في اي بي", "فاخره", "فاخرة"],
}

# Only these may match inside a longer reply. The rest are everyday words
# — "عايز عربية" is someone asking for a ride, not picking a menu row, so
# it must still get the menu.
_SUBSTRING_ALIASES = {
    "private":  ["ملاكي", "ملاكى"],
    "suzuki":   ["سوزوكي", "سوزوكى", "توك توك", "توكتوك", "تكتك", "suzuki", "tuktuk"],
    "delivery": ["دليفري", "ديليفري", "موتوسيكل", "موتسيكل", "delivery"],
    "vip":      ["في اي بي"],
}

# Normalize the alias tables once at import so lookups are cheap.
_TEXT_ALIASES = {k: [_normalize(a) for a in v] for k, v in _TEXT_ALIASES.items()}
_SUBSTRING_ALIASES = {k: [_normalize(a) for a in v] for k, v in _SUBSTRING_ALIASES.items()}

MENU_BODY = (
    "أهلاً بيك في وصلني بنها 🚖\n\n"
    "قولنا محتاج إيه النهاردة واختار الخدمة من القائمة:\n\n"
    "1️⃣ ملاكي — عربية خاصة\n"
    "2️⃣ سوزوكي — توك توك\n"
    "3️⃣ دليفري موتوسيكل — توصيل طلبات\n"
    "4️⃣ VIP — عربية فاخرة\n\n"
    "دوس على الزرار تحت واختار، أو ابعتلنا رقم الخدمة."
)

PROMO_THROTTLE_DAYS = 7


def kind_from_row_id(row_id: str) -> str | None:
    """Map a tapped list-reply id back to a service_kind slug."""
    return _ROW_ID_TO_KIND.get((row_id or "").strip())


def match_service_kind(text: str) -> str | None:
    """Best-effort match of a typed reply to a service kind.

    Deliberately strict — it only fires on a short message that is
    essentially just the service name or its number. A sentence like
    "عايز عربية من محطة القطار" must fall through to Gemini so we don't
    swallow a real booking message as a menu tap.
    """
    norm = _normalize(text or "")
    if not norm or len(norm) > 25:
        return None
    for kind, aliases in _TEXT_ALIASES.items():
        for alias in aliases:
            if norm == alias:
                return kind
    # Second pass: allow "عايز سوزوكي" / "سوزوكي لو سمحت" style short replies,
    # but only for names unambiguous enough to appear mid-sentence.
    for kind, aliases in _SUBSTRING_ALIASES.items():
        for alias in aliases:
            if alias in norm:
                return kind
    return None


def label_ar(kind: str) -> str:
    return SERVICE_KIND_LABELS_AR.get(kind, kind)


def send_service_menu(customer: Customer) -> None:
    """Send the tappable 4-service list. Falls back to plain text if the
    interactive send is rejected (unsupported client, template issues)."""
    from app.services import whatsapp
    from app.services.whatsapp import WhatsAppError
    from app.services import whatsapp_booking

    body_for_log = MENU_BODY
    try:
        resp = whatsapp.send_interactive_list(
            customer.wa_id,
            body=MENU_BODY,
            button_text="اختار الخدمة",
            rows=[{k: r[k] for k in ("id", "title", "description")} for r in MENU_ROWS],
            header="وصلني بنها",
            footer="خدمة 24 ساعة في بنها",
            section_title="خدماتنا",
        )
    except WhatsAppError as e:
        current_app.logger.warning("service menu list send failed, falling back: %s", e)
        whatsapp_booking._try_send(customer.wa_id, MENU_BODY, customer=customer)
        return

    wa_msg_id = (resp.get("messages") or [{}])[0].get("id") if isinstance(resp, dict) else None
    whatsapp_booking._persist_outbound(
        customer, body_for_log, msg_type="interactive", wa_message_id=wa_msg_id,
    )


def _promo_body() -> str:
    return (
        "🎉 جرب تطبيق وصلني بنها واحجز أسرع من غير ما تكلم حد!\n\n"
        "🎁 خصم 10% على رحلتك الجاية من التطبيق.\n\n"
        f"📱 أندرويد:\n{ANDROID_URL}\n\n"
        f"🍏 آيفون:\n{IOS_URL}"
    )


def send_app_promo(customer: Customer) -> bool:
    """Send the "download the app" nudge with the 10% discount line.

    Throttled per customer so a regular WhatsApp rider doesn't get the
    same advert after every single trip. Returns True when actually sent.
    """
    if customer is None or not customer.wa_id:
        return False

    last = getattr(customer, "app_promo_sent_at", None)
    if last and datetime.utcnow() - last < timedelta(days=PROMO_THROTTLE_DAYS):
        return False

    from app.services import whatsapp
    from app.services.whatsapp import WhatsAppError
    from app.services import whatsapp_booking

    body = _promo_body()
    try:
        resp = whatsapp.send_text(customer.wa_id, body)
    except WhatsAppError as e:
        current_app.logger.warning("app promo send failed: %s", e)
        return False

    try:
        customer.app_promo_sent_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.warning("app promo timestamp save failed: %s", e)

    wa_msg_id = (resp.get("messages") or [{}])[0].get("id") if isinstance(resp, dict) else None
    whatsapp_booking._persist_outbound(
        customer, body, msg_type="text", wa_message_id=wa_msg_id,
    )
    return True
