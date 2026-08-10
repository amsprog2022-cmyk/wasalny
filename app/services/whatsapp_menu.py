"""WhatsApp service menu + app-promo copy.

Every WhatsApp conversation opens with a tappable list of four rows.
Two of them start a ride (`service_kind`) and drive the whole booking
state machine:

  private   → auto-broadcast to the nearest captains (the original flow)
  intercity → never matched to a captain. The customer's free text is
              filed on the /intercity admin board and someone calls back.

The other two never touch the ride table — they hand back an "info"
action string that the booking pipeline uses to short-circuit:

  services → send back the depot phone numbers from ServiceNumber and
             end the session, so the customer calls Suzuki/delivery/etc
             directly.
  inquiry  → file an AdminAlert(kind="inquiry") and reply "someone will
             call you." No captain, no phone list — just a queued callback.

Customers on old WhatsApp builds see the list as plain text and type
their answer instead, so `match_service_kind` / `match_info_action`
also accept the number, the Arabic name, and common misspellings.
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
INFO_ID_PREFIX = "info_"

# Every row carries either a `kind` (starts a ride) or an `info` (short-
# circuits into a canned handler that never touches the ride table). Exactly
# one of the two is set per row.
MENU_ROWS = [
    {"id": f"{ROW_ID_PREFIX}private",   "kind": "private",   "info": None,       "title": "ملاكي داخل بنها 🚗", "description": "عربية خاصة توصلك جوه بنها"},
    {"id": f"{ROW_ID_PREFIX}intercity", "kind": "intercity", "info": None,       "title": "سفر خارج بنها 🛣️",  "description": "ملاكي للسفر برة بنها"},
    {"id": f"{INFO_ID_PREFIX}services", "kind": None,        "info": "services", "title": "خدمات تانية 🚚",    "description": "سوزوكي، دليفري، تيوتا، سبع راكب، زفاف"},
    {"id": f"{INFO_ID_PREFIX}inquiry",  "kind": None,        "info": "inquiry",  "title": "استفسارات ℹ️",      "description": "استفسار عام لحد من الإدارة"},
]

_ROW_ID_TO_KIND = {r["id"]: r["kind"] for r in MENU_ROWS if r["kind"]}
_ROW_ID_TO_INFO = {r["id"]: r["info"] for r in MENU_ROWS if r["info"]}

# Typed fallbacks. Keys are already Arabic-normalized + lowercased.
# suzuki/delivery/vip are gone from the menu, so their old numbers must
# not linger here — a stale "3" would otherwise select a dead service.
_TEXT_ALIASES = {
    "private":   ["1", "١", "ملاكي", "ملاكى", "عربيه", "عربية", "خاصه", "تاكسي", "taxi", "private", "car"],
    "intercity": ["2", "٢", "سفر", "سفريه", "سفرية", "خارج بنها", "بره بنها", "برة بنها", "travel", "intercity"],
}

# Info rows also accept typed replies. Keep them short and unambiguous —
# "سوزوكي لو سمحت" is a customer picking option 3, not a booking sentence.
_INFO_TEXT_ALIASES = {
    "services": [
        "3", "٣", "خدمات", "خدمه", "خدمة",
        "سوزوكي", "توك توك", "توكتوك", "دليفري", "دليڤري", "توصيل",
        "تيوتا", "تويوتا", "سبع راكب", "٧ راكب", "7 راكب", "زفاف", "فرح",
    ],
    "inquiry":  ["4", "٤", "استفسار", "استفسارات", "سؤال", "مشكله", "مشكلة", "شكوى"],
}

# Only these may match inside a longer reply. The rest are everyday words
# — "عايز عربية" is someone asking for a ride, not picking a menu row, so
# it must still get the menu.
#
# intercity is checked first on purpose: "ملاكي للسفر خارج بنها" contains
# both patterns and the destination is what actually distinguishes it.
_SUBSTRING_ALIASES = {
    "intercity": ["خارج بنها", "بره بنها", "برة بنها", "سفر"],
    "private":   ["ملاكي", "ملاكى"],
}

_INFO_LABELS_AR = {
    "services": "خدمات تانية",
    "inquiry":  "استفسارات",
}

# Normalize the alias tables once at import so lookups are cheap.
_TEXT_ALIASES = {k: [_normalize(a) for a in v] for k, v in _TEXT_ALIASES.items()}
_INFO_TEXT_ALIASES = {k: [_normalize(a) for a in v] for k, v in _INFO_TEXT_ALIASES.items()}
_SUBSTRING_ALIASES = {k: [_normalize(a) for a in v] for k, v in _SUBSTRING_ALIASES.items()}

MENU_BODY = (
    "اهلا بيك.\n\n"
    "محتاج ايه؟\n"
    "١. عربية ملاكي داخل بنها\n"
    "٢. محتاج عربية للسفر خارج بنها\n"
    "٣. لخدمة السوزوكي والدليفري والتيوتا والسبع راكب والزفاف\n"
    "٤. للاستفسارات\n\n"
    "دوس على الزرار تحت واختار، أو ابعتلنا رقم الخدمة"
)

PROMO_THROTTLE_DAYS = 7


def kind_from_row_id(row_id: str) -> str | None:
    """Map a tapped list-reply id back to a service_kind slug (or None
    if the row is an info row rather than a ride starter)."""
    return _ROW_ID_TO_KIND.get((row_id or "").strip())


def info_action_from_row_id(row_id: str) -> str | None:
    """Map a tapped list-reply id back to an info-action ('services' /
    'inquiry'). Returns None when the row starts a ride instead."""
    return _ROW_ID_TO_INFO.get((row_id or "").strip())


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


def match_info_action(text: str) -> str | None:
    """Same as `match_service_kind` but for the two info rows (services /
    inquiry). Exact-match only, so a booking sentence like "عايز سوزوكي
    من محطة القطار" doesn't get swallowed as an info tap."""
    norm = _normalize(text or "")
    if not norm or len(norm) > 25:
        return None
    for action, aliases in _INFO_TEXT_ALIASES.items():
        if norm in aliases:
            return action
    return None


def label_ar(kind: str) -> str:
    return SERVICE_KIND_LABELS_AR.get(kind, kind)


def info_label_ar(action: str) -> str:
    return _INFO_LABELS_AR.get(action, action)


def send_service_menu(customer: Customer) -> None:
    """Send the tappable service list. Falls back to plain text if the
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
        "🎉 جرب تطبيق وصلني بنها واحجز أسرع من غير ما تكلم حد.\n\n"
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
