"""Reverse OTP — the customer proves phone ownership by sending us a message.

Instead of us sending a code to the customer (which costs money per
WhatsApp auth template and can be defeated by SIM-swap or lock-screen
notification scraping), we mint a code, hand it to the app, and the app
opens WhatsApp with the message prefilled. The customer taps send. Our
webhook then matches the code against the sender's own wa_id.

The security property: a code minted *for* wa_id X is only ever accepted
from an inbound message *from* wa_id X. Possession of the WhatsApp
account is the proof, and inbound messages are free.

Polling uses `request_id` (uuid4 hex), never the 6-digit code — otherwise
a 6-digit space would be trivially brute-forceable over the status
endpoint.

Redis keys (all TTL'd, nothing to garbage-collect):
  waverify:req:{request_id}      JSON {wa_id, code, purpose, verified, ticket}
  waverify:code:{wa_id}:{code}   -> request_id
  waverify:pending:{wa_id}       -> request_id  (newest, for bare "تفعيل")
  waverify:ticket:{ticket}       JSON {wa_id, purpose}
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from typing import Optional
from urllib.parse import quote

from flask import current_app

from app.extensions import get_redis

log = logging.getLogger(__name__)

PURPOSES = ("customer_register", "customer_reset", "driver_reset")

KEYWORD = "تفعيل"
_CODE_RE = re.compile(rf"{KEYWORD}\s*[:\-]?\s*(\d{{6}})")
_BARE_RE = re.compile(rf"^\s*{KEYWORD}\s*$")

CONFIRM_TEXT = "تم تفعيل رقمك. ارجع للتطبيق وكمل."


def _r():
    return get_redis(current_app.config.get("REDIS_URL"))


def _code_ttl() -> int:
    return int(current_app.config.get("VERIFY_CODE_TTL_SECONDS", 600))


def _ticket_ttl() -> int:
    return int(current_app.config.get("VERIFY_TICKET_TTL_SECONDS", 900))


def normalize_wa_id(raw: str) -> str:
    """Strip everything but digits so '+20 101 234 5678' == '201012345678'."""
    return re.sub(r"\D", "", raw or "")


def build_deeplink(code: str) -> str:
    number = normalize_wa_id(current_app.config.get("WHATSAPP_BUSINESS_NUMBER", ""))
    return f"https://wa.me/{number}?text={quote(f'{KEYWORD} {code}')}"


def start(wa_id: str, purpose: str) -> dict:
    """Mint a code + request_id for this phone and return the wa.me deeplink."""
    if purpose not in PURPOSES:
        raise ValueError(f"unknown verification purpose: {purpose}")

    wa_id = normalize_wa_id(wa_id)
    code = f"{secrets.randbelow(1_000_000):06d}"
    request_id = uuid.uuid4().hex
    ttl = _code_ttl()

    record = {"wa_id": wa_id, "code": code, "purpose": purpose, "verified": False}
    r = _r()
    pipe = r.pipeline()
    pipe.setex(f"waverify:req:{request_id}", ttl, json.dumps(record))
    pipe.setex(f"waverify:code:{wa_id}:{code}", ttl, request_id)
    pipe.setex(f"waverify:pending:{wa_id}", ttl, request_id)
    pipe.execute()

    return {
        "request_id": request_id,
        "code": code,
        "deeplink": build_deeplink(code),
        "expires_in": ttl,
    }


def resolve_inbound(sender_wa_id: str, text: str) -> Optional[str]:
    """Match an inbound WhatsApp message against a pending code.

    Returns the purpose on success (so the webhook knows this message was
    consumed and must not reach the AI booking pipeline), else None.
    """
    if not text or KEYWORD not in text:
        return None

    sender = normalize_wa_id(sender_wa_id)
    r = _r()

    match = _CODE_RE.search(text)
    if match:
        request_id = r.get(f"waverify:code:{sender}:{match.group(1)}")
    elif _BARE_RE.match(text):
        # Customer retyped "تفعيل" without the digits. Fall back to their
        # newest pending request — still scoped to their own wa_id, so this
        # doesn't weaken the ownership proof.
        request_id = r.get(f"waverify:pending:{sender}")
    else:
        return None

    if not request_id:
        return None

    raw = r.get(f"waverify:req:{request_id}")
    if not raw:
        return None

    record = json.loads(raw)
    if normalize_wa_id(record.get("wa_id", "")) != sender:
        return None

    ticket = uuid.uuid4().hex
    record["verified"] = True
    record["ticket"] = ticket

    pipe = r.pipeline()
    pipe.setex(f"waverify:req:{request_id}", _code_ttl(), json.dumps(record))
    pipe.setex(
        f"waverify:ticket:{ticket}",
        _ticket_ttl(),
        json.dumps({"wa_id": record["wa_id"], "purpose": record["purpose"]}),
    )
    pipe.delete(f"waverify:code:{sender}:{record['code']}")
    pipe.delete(f"waverify:pending:{sender}")
    pipe.execute()

    return record["purpose"]


def status(request_id: str) -> dict:
    """Poll target for the app. Never exposes the code."""
    raw = _r().get(f"waverify:req:{request_id}")
    if not raw:
        return {"verified": False, "expired": True, "ticket": None}
    record = json.loads(raw)
    return {
        "verified": bool(record.get("verified")),
        "expired": False,
        "ticket": record.get("ticket"),
    }


def consume_ticket(ticket: str, wa_id: str, purpose: str) -> bool:
    """Single-use redemption. Deletes the key so a replay always fails."""
    if not ticket:
        return False
    r = _r()
    key = f"waverify:ticket:{ticket}"
    raw = r.get(key)
    if not raw:
        return False
    record = json.loads(raw)
    if normalize_wa_id(record.get("wa_id", "")) != normalize_wa_id(wa_id):
        return False
    if record.get("purpose") != purpose:
        return False
    r.delete(key)
    return True
