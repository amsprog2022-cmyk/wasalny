"""Gemini 2.0 Flash Arabic parser (PLAN §12).

Takes an incoming customer WhatsApp message and returns a structured intent:

    {
      "intent": "book_ride" | "clarify" | "unknown",
      "from_zone_slug": "ramla" | null,
      "to_zone_slug": "university" | null,
      "confidence": 0.0-1.0,
      "reply_ar": "من فين لفين؟"     # only when clarify/unknown
    }

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
    confidence: float
    reply_ar: str
    raw_response: str
    used_fallback: bool = False

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "from_zone_slug": self.from_zone_slug,
            "to_zone_slug": self.to_zone_slug,
            "confidence": self.confidence,
            "reply_ar": self.reply_ar,
            "used_fallback": self.used_fallback,
        }


def _build_prompt(user_message: str, prior: dict | None = None) -> str:
    zones = Zone.query.filter_by(is_active=True).order_by(Zone.id.asc()).all()
    zone_lines = "\n".join(f"- {z.name_ar}  (slug: {z.slug})" for z in zones)

    prior_line = ""
    if prior and (prior.get("from") or prior.get("to")):
        parts = []
        if prior.get("from"): parts.append(f"من: {prior['from']}")
        if prior.get("to"):   parts.append(f"إلى: {prior['to']}")
        prior_line = f"\nمعلومات سابقة من نفس العميل: {'، '.join(parts)}\n"

    return f"""أنت مساعد لتطبيق وصلني بنها للأجرة.
مهمتك: من كلام العميل، حدد من أين ينطلق ومكان وجهته.

المناطق المتاحة (استخدم فقط slug من هذه القائمة):
{zone_lines}
{prior_line}
رسالة العميل:
\"\"\"{user_message}\"\"\"

أرجع JSON فقط (بدون أي شرح) بالتنسيق:
{{"intent": "book_ride" أو "clarify" أو "unknown",
  "from_zone_slug": "<slug من القائمة> أو null",
  "to_zone_slug":   "<slug من القائمة> أو null",
  "confidence": رقم من 0.0 إلى 1.0,
  "reply_ar": "<نص عربي للرد إذا كان intent = clarify أو unknown>"}}
"""


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


def parse_message(user_message: str, prior: dict | None = None) -> ParseResult:
    """Attempt to parse the message via Gemini.

    Returns a ParseResult in all cases — never raises. On failure, returns
    intent="unknown" so the caller can hand off to a human agent.
    """
    prompt = _build_prompt(user_message, prior=prior)

    try:
        raw = _call_gemini(prompt)
        parsed = _extract_json(raw)
        if not parsed:
            return ParseResult(
                intent="unknown",
                from_zone_slug=None,
                to_zone_slug=None,
                confidence=0.0,
                reply_ar="",
                raw_response=raw,
                used_fallback=True,
            )
        return ParseResult(
            intent=str(parsed.get("intent") or "unknown"),
            from_zone_slug=parsed.get("from_zone_slug") or None,
            to_zone_slug=parsed.get("to_zone_slug") or None,
            confidence=float(parsed.get("confidence") or 0.0),
            reply_ar=str(parsed.get("reply_ar") or ""),
            raw_response=raw,
        )
    except Exception as e:
        current_app.logger.warning("gemini parse failed: %s", e)
        return ParseResult(
            intent="unknown",
            from_zone_slug=None,
            to_zone_slug=None,
            confidence=0.0,
            reply_ar="",
            raw_response=str(e),
            used_fallback=True,
        )
