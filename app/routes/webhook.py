"""Meta WhatsApp webhook: verification (GET) + incoming events (POST)."""
import logging

import eventlet
from flask import Blueprint, request, current_app, abort

from app.services import whatsapp
from app.services.inbox import handle_incoming_message, handle_status_update
from app.services import whatsapp_booking

log = logging.getLogger(__name__)

webhook_bp = Blueprint("webhook", __name__, url_prefix="/webhook")


@webhook_bp.route("", methods=["GET"])
def verify():
    """Meta verification handshake."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == current_app.config["WHATSAPP_VERIFY_TOKEN"]:
        return challenge or "", 200
    abort(403)


@webhook_bp.route("", methods=["POST"])
def receive():
    raw = request.get_data()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not whatsapp.verify_webhook_signature(raw, signature):
        log.warning("Invalid webhook signature")
        abort(403)

    data = request.get_json(silent=True) or {}

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {c.get("wa_id"): c for c in value.get("contacts", [])}

            for msg in value.get("messages", []):
                contact = contacts.get(msg.get("from"))
                try:
                    persisted = handle_incoming_message(msg, contact)
                except Exception:
                    log.exception("Failed to handle incoming message %s", msg.get("id"))
                    continue

                # Route customer text messages through the AI booking pipeline.
                # Drivers use the app directly, so their inbound messages stay in
                # the human agent inbox only.
                if (
                    persisted is not None
                    and persisted.msg_type == "text"
                    and persisted.body
                    and persisted.conversation
                    and persisted.conversation.kind == "customer"
                    and persisted.conversation.customer is not None
                ):
                    _spawn_ai_booking(persisted.conversation.customer.id, persisted.body)

            for status in value.get("statuses", []):
                try:
                    handle_status_update(status)
                except Exception:
                    log.exception("Failed to handle status %s", status.get("id"))

    # Always 200 — otherwise Meta will retry aggressively
    return "", 200


def _spawn_ai_booking(customer_id: int, body: str) -> None:
    """Run the Gemini booking pipeline in a greenlet so the webhook returns fast."""
    app = current_app._get_current_object()

    def _work():
        with app.app_context():
            from app import db
            from app.models.customer import Customer
            customer = db.session.get(Customer, customer_id)
            if customer is None:
                return
            try:
                whatsapp_booking.process_incoming(customer, body)
            except Exception:
                app.logger.exception("whatsapp_booking failed for customer %s", customer_id)

    eventlet.spawn_n(_work)
