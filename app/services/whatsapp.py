"""WhatsApp Cloud API client + webhook helpers.

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""
import hmac
import hashlib
import logging
from typing import Optional

import requests
from flask import current_app

log = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"


class WhatsAppError(Exception):
    pass


def _base_url() -> str:
    version = current_app.config["WHATSAPP_API_VERSION"]
    phone_id = current_app.config["WHATSAPP_PHONE_NUMBER_ID"]
    return f"{GRAPH_URL}/{version}/{phone_id}"


def _headers() -> dict:
    token = current_app.config["WHATSAPP_ACCESS_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def send_text(to_wa_id: str, body: str) -> dict:
    """Send free-form text. Only allowed within a 24h customer service window."""
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_wa_id,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    return _post(payload)


def send_template(
    to_wa_id: str,
    template_name: str,
    language: str = "ar",
    body_variables: Optional[list] = None,
) -> dict:
    """Send an approved template. Use this to initiate conversations."""
    components = []
    if body_variables:
        components.append(
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": str(v)} for v in body_variables
                ],
            }
        )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_wa_id,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }
    return _post(payload)


def mark_as_read(wa_message_id: str) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": wa_message_id,
    }
    return _post(payload)


def _post(payload: dict) -> dict:
    url = f"{_base_url()}/messages"
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
    except requests.RequestException as e:
        log.exception("WhatsApp API network error")
        raise WhatsAppError(f"Network error: {e}") from e

    if resp.status_code >= 400:
        log.error("WhatsApp API error %s: %s", resp.status_code, resp.text)
        raise WhatsAppError(f"{resp.status_code}: {resp.text}")

    return resp.json()


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verify X-Hub-Signature-256 from Meta.

    Robust to common paste issues (whitespace, surrounding quotes) and logs
    enough detail to diagnose mismatches without leaking the full secret.
    """
    # Escape hatch for debugging bad configs — set WHATSAPP_SKIP_SIGNATURE_CHECK=true
    # to bypass entirely. USE ONLY WHEN TESTING; never in real production.
    if str(current_app.config.get("WHATSAPP_SKIP_SIGNATURE_CHECK", "")).lower() in ("1", "true", "yes"):
        log.warning("Webhook signature check bypassed via WHATSAPP_SKIP_SIGNATURE_CHECK")
        return True

    app_secret_raw = current_app.config.get("WHATSAPP_APP_SECRET", "") or ""
    # Strip whitespace and any accidental surrounding quotes from a paste
    app_secret = app_secret_raw.strip().strip('"').strip("'")

    if not app_secret:
        log.warning("Webhook signature check skipped (no WHATSAPP_APP_SECRET set)")
        return True

    if not signature_header:
        log.warning("Webhook rejected: signature header missing (secret is set)")
        return False

    if not signature_header.startswith("sha256="):
        log.warning("Webhook rejected: signature header wrong format: %s", signature_header[:20])
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.split("=", 1)[1].strip()

    if hmac.compare_digest(expected, provided):
        return True

    # Loud diagnostic on mismatch — only first 8 chars of each hash + secret hash
    # so we don't leak values but can spot obvious differences.
    log.warning(
        "Signature mismatch. expected=%s… provided=%s… body_len=%d secret_hash=%s…",
        expected[:8],
        provided[:8],
        len(raw_body),
        hashlib.sha256(app_secret.encode("utf-8")).hexdigest()[:8],
    )
    return False
