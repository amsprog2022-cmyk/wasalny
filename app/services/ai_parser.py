"""Gemini 2.0 Flash Arabic parser (PLAN §12).

Takes an incoming customer WhatsApp message and returns a structured intent:

    {
      "intent": "book_ride" | "clarify" | "chat" | "unknown",
      "from_zone_slug": "ramla" | null,
      "to_zone_slug": "university" | null,
      "confidence": 0.0-1.0,
      "reply_ar": "من فين لفين؟"     # only when clarify / chat / unknown
    }

`chat` covers general questions (working hours, service area, how it works)
where Gemini should reply as a friendly Wassalny support agent in Egyptian
Arabic. See `_build_prompt` for the persona + guardrails.

Reliability rules (Decision #4):
  - 3 second hard timeout.
  - On any error, timeout, or low confidence → intent="unknown" → the caller
    creates an admin handoff alert. Better to bother an agent than to book a
    trip to the wrong place.
  - Prompt lists only ACTIVE zones so we never emit a dead slug.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

import requests
from flask import current_app

from app.models.zone import Zone


@dataclass
class ParseResult:
    intent: str
    from_zone_slug: Optional[str]
    to_zone_slug: Optional[str]
    # Free-text place names the AI extracted from the customer message.
    # These are what the GPS booking pipeline geocodes via Nominatim.
    # Zone slugs are kept as a legacy fallback for existing sessions.
    pickup_text: Optional[str]
    dropoff_text: Optional[str]
    confidence: float
    reply_ar: str
    raw_response: str
    used_fallback: bool = False
    complaint_summary: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "from_zone_slug": self.from_zone_slug,
            "to_zone_slug": self.to_zone_slug,
            "pickup_text": self.pickup_text,
            "dropoff_text": self.dropoff_text,
            "confidence": self.confidence,
            "reply_ar": self.reply_ar,
            "used_fallback": self.used_fallback,
            "complaint_summary": self.complaint_summary,
        }


def _build_prompt(
    user_message: str,
    prior: dict | None = None,
    active_ride: dict | None = None,
    service_kind: str | None = None,
) -> str:
    """Compose the Gemini prompt.

    `prior`        — partial state from a prior turn ({from, to})
    `active_ride`  — dict describing the customer's current in-flight ride if any,
                     so the AI can answer 'where is my captain?' contextually
                     and route cancel/complaint intents to the right ride.
    `service_kind` — which of the four services the customer already picked
                     off the WhatsApp menu. Changes what we still need to ask
                     for: private rides need only a pickup, the admin-dispatched
                     kinds need a destination too.
    """
    zones = Zone.query.filter_by(is_active=True).order_by(Zone.id.asc()).all()
    zone_lines = "\n".join(f"- {z.name_ar}  (slug: {z.slug})" for z in zones)

    prior_line = ""
    if prior and (prior.get("from") or prior.get("to")):
        parts = []
        if prior.get("from"): parts.append(f"من: {prior['from']}")
        if prior.get("to"):   parts.append(f"إلى: {prior['to']}")
        prior_line = f"\nمعلومات سابقة من نفس العميل: {'، '.join(parts)}\n"

    ride_line = ""
    if active_ride:
        status_ar = {
            "broadcasting": "بندور على كابتن",
            "assigned": "الكابتن في الطريق",
            "started": "في الرحلة دلوقتي",
        }.get(active_ride.get("status", ""), active_ride.get("status", ""))
        ride_line = (
            f"\n⚠️ العميل عنده رحلة نشطة دلوقتي:\n"
            f"  - رقم الرحلة: {active_ride.get('id')}\n"
            f"  - الحالة: {status_ar}\n"
            f"  - من: {active_ride.get('from_zone_ar')}\n"
            f"  - إلى: {active_ride.get('to_zone_ar')}\n"
            f"  - السعر: {active_ride.get('price_egp')} ج.م\n"
        )
        if active_ride.get("driver_name"):
            ride_line += f"  - الكابتن: {active_ride['driver_name']}\n"

    service_line = ""
    if service_kind:
        from app.models.driver import SERVICE_KIND_LABELS_AR
        label = SERVICE_KIND_LABELS_AR.get(service_kind, service_kind)
        service_line = (
            f"\n✅ العميل اختار خدمة: {label}. ماتسألوش تاني عن نوع الخدمة.\n"
        )
        if service_kind == "private":
            service_line += (
                "  المطلوب منه دلوقتي: مكان الاستقلال (pickup_text) والوجهة "
                "(dropoff_text) — الاتنين لازم عشان نحسبله السعر ونبعت الرحلة "
                "للكابتن بالتفاصيل الكاملة.\n"
                "  لو معاك مكان الاستقلال بس، اسأله عن الوجهة. لو معاك الاتنين، "
                "استخرجهم كلهم في نفس التيرن.\n"
                "  لو الرسالة اللي جاية دلوقتي مجرد اسم مكان مختصر (مثال: 'بنها'، "
                "'جامعة بنها'، 'محطة القطار')، اعتبرها dropoff_text لأننا كنا "
                "سألنا عن الوجهة قبلها.\n"
            )
        elif service_kind == "delivery":
            service_line += (
                "  المطلوب منه دلوقتي: مكان استلام الطلب + العنوان اللي "
                "الطلب رايح ليه. الاتنين لازم.\n"
            )
        else:
            service_line += (
                "  المطلوب منه دلوقتي: مكان الاستقلال + الوجهة. الاتنين لازم "
                "لأن الإدارة هي اللي هتبعتله كابتن.\n"
            )

    return f"""أنت مساعد ودود لتطبيق وصلني بنها للأجرة في مدينة بنها بمصر.
مهمتك تتعرف على قصد العميل من كلامه وترجعه في JSON منظّم.

الأقصاد المدعومة (intent):
  - "book_ride"     → العميل عايز كابتن. طول ما فيه منطقة انطلاق (from_zone_slug) واضحة، رجّع book_ride حتى لو الوجهة مش معروفة.
  - "clarify"       → عايز يحجز بس ماذكرش من فين هو دلوقتي. اسأله عن مكانه الحالي بس.
  - "ride_status"   → بيسأل عن حالة رحلة نشطة عنده (فين الكابتن؟ إمتى يوصل؟).
  - "cancel_ride"   → عايز يلغي رحلته النشطة (لغي / كنسل / اعذر / مش عايز).
  - "complaint"     → بيشتكي من كابتن أو رحلة (بيسوق بسرعة، رفع السعر بدون سبب، إلخ).
  - "chat"          → سؤال عام (مواعيد الشغل، إزاي أحجز، ...) مش متعلق بحجز أو رحلة.
  - "unknown"       → مش فاهم أو الرسالة مبهمة.

قواعد مهمة للحجز عبر واتساب (طلبات قصيرة، سؤال أو اتنين على الأكتر):
  - **أهم قاعدة**: لو العميل ذكر أي مكان انطلاق (pickup) أو وصول (dropoff) → intent="book_ride" فوراً.
  - **pickup_text**: استخرج مكان الاستقلال زي ما كتبه العميل بالظبط (مثال: "محطة القطار بنها"، "مستشفى بنها التخصصي"، "شارع النصر جنب مسجد النور"، "بجانب مدرسة كذا"). ماتحاولش تختصر أو تحوله لـ slug — الباك اند هيبحث عنه في الخريطة بنفسه.
  - **dropoff_text**: نفس الحكاية — استخرج الوجهة زي ما هي.
  - **from_zone_slug / to_zone_slug**: اتركهم null دلوقتي، الباك اند مبقاش بيعتمد عليهم.
  - لو العميل قال "عايز كابتن" أو "عايز رحلة" من غير ما يذكر أي مكان → intent="clarify" و reply_ar = "🌟 تحب تروح فين؟".
  - لو ذكر الوجهة بس ومكانه لأ → intent="clarify" و reply_ar = "تمام. وحضرتك دلوقتي فين؟ لو صعب توصف مكانك ابعت 📍 من الواتس (دوس على 📎 المرفقات ← الموقع ← ابعت موقعك الحالي). 🚗".
  - الخدمة متاحة في بنها والقليوبية والقاهرة الكبرى بس. لو العميل عايز يسافر خارج ده (إسكندرية، أسوان، إلخ) → intent="chat" واعتذر بأدب.
  - ماتكررش الأسئلة. لو مش عارف تحدد قصده أبدًا → intent="unknown" (البرنامج هيحوله لموظف).

للمرجعية بس، ده بعض المناطق المخدومة (اتركهم null في الـ slug، بس ممكن تستخدمهم لتفهم مصر أفضل):
{zone_lines}
{prior_line}{ride_line}{service_line}
رسالة العميل:
\"\"\"{user_message}\"\"\"

قواعد الردود (reply_ar):
  - رد قصير (جملة أو اتنين على الأكتر) وباللهجة المصرية زي موظف خدمة عملاء ودود.
  - لا تذكر سعر محدد بالجنيه إلا لو السعر موجود في معلومات الرحلة النشطة أعلاه.
  - لا تعد بوقت وصول محدد بالدقايق؛ قل "الكابتن هيتواصل معاك في أقرب وقت".
  - ماتبدأش الرد بإيموجي؛ لو حابب تستخدم إيموجي زي (🌟 🚗 🙂 ✅ ⚠️) خليه في آخر الجملة مش أولها.
  - ممنوع نهائيًا استخدام علامة التعجب (!) في أي رد. استخدم النقطة بدلًا منها.
  - لـ ride_status: اذكر حالة الرحلة الحالية بوضوح واسم الكابتن لو متاح.
  - لـ cancel_ride: أكد للعميل إن الطلب اتسجل وسنلغي الرحلة.
  - لـ complaint: اعتذر بأدب وأكد إن الشكوى راحت للإدارة.

أرجع JSON فقط (بدون أي شرح) بالتنسيق التالي:
{{"intent": "<one of the intents above>",
  "pickup_text":   "<اسم مكان الاستقلال زي ما كتبه العميل، أو null>",
  "dropoff_text":  "<اسم الوجهة زي ما كتبها العميل، أو null>",
  "from_zone_slug": null,
  "to_zone_slug":   null,
  "confidence": 0.0-1.0,
  "reply_ar": "<نص الرد لو intent مش book_ride>",
  "complaint_summary": "<ملخص الشكوى بالعربي لو intent = complaint>"}}
"""


def _strip_exclamations(text: str) -> str:
    """Enforce the no-"!" house style on anything Gemini writes.

    The prompt asks for it too, but a prompt is a request and this is a
    guarantee. Collapses "!!!" and a "!" already followed by punctuation
    so we never emit a stray ".." or a dangling space before a newline.
    """
    if not text:
        return text
    text = re.sub(r"[ \t]*[!！]+(?=\s*[.،,؟?])", "", text)
    text = re.sub(r"[ \t]*[!！]+", ".", text)
    return re.sub(r"[ \t]+(?=\n|$)", "", text)


def _extract_json(text: str) -> dict | None:
    """Extract a JSON object from Gemini's response.

    Handles three cases:
      1. Clean JSON: ``{"intent": ...}``
      2. Markdown-wrapped: ```` ```json {...} ``` ````
      3. Missing trailing ``}`` (Gemini sometimes truncates in structured mode)
    """
    text = text.strip()
    # Strip common markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Fast path: whole string is valid JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Auto-repair: add a trailing `}` if we saw `{` but never a closing `}`.
    if text.startswith("{") and not text.rstrip().endswith("}"):
        try:
            return json.loads(text + "}")
        except json.JSONDecodeError:
            pass

    # Fallback: greedy match a { ... } inside the string
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _call_gemini(prompt: str) -> str:
    api_key = current_app.config.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("no_api_key")
    model = current_app.config.get("GEMINI_MODEL", "gemini-2.0-flash")
    timeout = float(current_app.config.get("GEMINI_TIMEOUT_SECONDS", 3))

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Newer Gemini models split responses across multiple `parts` entries
    # (e.g. thoughtSignature chunks + text chunks). Concatenate every part
    # that has a `text` field so we never lose trailing characters.
    parts = data["candidates"][0]["content"].get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def parse_message(
    user_message: str,
    prior: dict | None = None,
    active_ride: dict | None = None,
    service_kind: str | None = None,
) -> ParseResult:
    """Attempt to parse the message via Gemini.

    Returns a ParseResult in all cases — never raises. On failure, returns
    intent="unknown" so the caller can hand off to a human agent.

    `active_ride` — optional dict describing the customer's current in-flight
    ride so Gemini can answer 'where is my captain?' contextually and route
    cancel/complaint intents to the right ride.
    """
    prompt = _build_prompt(
        user_message, prior=prior, active_ride=active_ride, service_kind=service_kind,
    )

    try:
        raw = _call_gemini(prompt)
        parsed = _extract_json(raw)
        if not parsed:
            return ParseResult(
                intent="unknown",
                from_zone_slug=None,
                to_zone_slug=None,
                pickup_text=None,
                dropoff_text=None,
                confidence=0.0,
                reply_ar="",
                raw_response=raw,
                used_fallback=True,
            )
        return ParseResult(
            intent=str(parsed.get("intent") or "unknown"),
            from_zone_slug=parsed.get("from_zone_slug") or None,
            to_zone_slug=parsed.get("to_zone_slug") or None,
            pickup_text=(parsed.get("pickup_text") or None),
            dropoff_text=(parsed.get("dropoff_text") or None),
            confidence=float(parsed.get("confidence") or 0.0),
            reply_ar=_strip_exclamations(str(parsed.get("reply_ar") or "")),
            raw_response=raw,
            complaint_summary=parsed.get("complaint_summary") or None,
        )
    except Exception as e:
        current_app.logger.warning("gemini parse failed: %s", e)
        return ParseResult(
            intent="unknown",
            from_zone_slug=None,
            to_zone_slug=None,
            pickup_text=None,
            dropoff_text=None,
            confidence=0.0,
            reply_ar="",
            raw_response=str(e),
            used_fallback=True,
        )
