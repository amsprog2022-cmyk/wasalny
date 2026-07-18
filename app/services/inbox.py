"""Inbox service: process incoming WhatsApp webhooks + send outgoing messages."""
from datetime import datetime
from typing import Optional

from flask_socketio import emit

from app import db, socketio
from app.models import Customer, Driver, Conversation, Message
from app.services import whatsapp


def _get_or_create_customer(wa_id: str, profile_name: Optional[str] = None) -> Customer:
    customer = Customer.query.filter_by(wa_id=wa_id).first()
    if customer:
        if profile_name and not customer.name:
            customer.name = profile_name
        return customer
    customer = Customer(wa_id=wa_id, name=profile_name)
    db.session.add(customer)
    db.session.flush()
    return customer


def _find_driver(wa_id: str) -> Optional[Driver]:
    return Driver.query.filter_by(wa_id=wa_id).first()


def _get_or_create_conversation(peer_wa_id: str, profile_name: Optional[str] = None) -> Conversation:
    """Match incoming wa_id: if it's a known driver, use driver conversation, else customer."""
    driver = _find_driver(peer_wa_id)
    if driver:
        conv = Conversation.query.filter_by(driver_id=driver.id, kind="driver").first()
        if not conv:
            conv = Conversation(kind="driver", driver_id=driver.id)
            db.session.add(conv)
            db.session.flush()
        return conv

    customer = _get_or_create_customer(peer_wa_id, profile_name)
    conv = Conversation.query.filter_by(customer_id=customer.id, kind="customer").first()
    if not conv:
        conv = Conversation(kind="customer", customer_id=customer.id)
        db.session.add(conv)
        db.session.flush()
    return conv


def handle_incoming_message(msg: dict, contact: dict) -> Optional[Message]:
    """Process one incoming message payload from Meta webhook."""
    wa_id = msg.get("from")
    if not wa_id:
        return None

    profile_name = None
    if contact and contact.get("profile"):
        profile_name = contact["profile"].get("name")

    conv = _get_or_create_conversation(wa_id, profile_name)

    msg_type = msg.get("type", "text")
    body = None
    media_url = None

    if msg_type == "text":
        body = msg.get("text", {}).get("body", "")
    elif msg_type in ("image", "audio", "video", "document"):
        media = msg.get(msg_type, {})
        body = media.get("caption") or f"[{msg_type}]"
        media_url = media.get("id")  # media ID; download separately if needed
    elif msg_type == "location":
        loc = msg.get("location", {})
        body = f"📍 Location: {loc.get('latitude')}, {loc.get('longitude')}"
    else:
        body = f"[{msg_type} message]"

    message = Message(
        conversation_id=conv.id,
        wa_message_id=msg.get("id"),
        direction="inbound",
        msg_type=msg_type,
        body=body,
        media_url=media_url,
        status="delivered",
    )
    db.session.add(message)

    conv.last_message_preview = (body or "")[:500]
    conv.last_message_at = datetime.utcnow()
    conv.last_inbound_at = datetime.utcnow()
    conv.unread_count = (conv.unread_count or 0) + 1
    if conv.status == "closed":
        conv.status = "open"

    db.session.commit()

    _broadcast_new_message(conv, message)
    return message


def handle_status_update(status: dict) -> None:
    """Update outbound message status from webhook (sent/delivered/read/failed)."""
    wa_message_id = status.get("id")
    new_status = status.get("status")
    if not wa_message_id or not new_status:
        return

    msg = Message.query.filter_by(wa_message_id=wa_message_id).first()
    if not msg:
        return

    msg.status = new_status
    if new_status == "failed":
        errors = status.get("errors", [])
        if errors:
            msg.error = str(errors[0])
    db.session.commit()


def send_outbound_text(conversation_id: int, body: str, user_id: Optional[int] = None) -> Message:
    conv = Conversation.query.get_or_404(conversation_id)
    if not conv.within_free_window():
        raise ValueError(
            "Cannot send free-form text: 24h window is closed. "
            "Use a template instead."
        )

    resp = whatsapp.send_text(conv.peer_wa_id(), body)
    wa_msg_id = resp.get("messages", [{}])[0].get("id")

    msg = Message(
        conversation_id=conv.id,
        wa_message_id=wa_msg_id,
        direction="outbound",
        msg_type="text",
        body=body,
        status="sent",
        sent_by_user_id=user_id,
    )
    db.session.add(msg)

    conv.last_message_preview = body[:500]
    conv.last_message_at = datetime.utcnow()
    db.session.commit()

    _broadcast_new_message(conv, msg)
    return msg


def send_outbound_template(
    conversation_id: int,
    template_name: str,
    language: str = "ar",
    variables: Optional[list] = None,
    user_id: Optional[int] = None,
) -> Message:
    conv = Conversation.query.get_or_404(conversation_id)
    resp = whatsapp.send_template(conv.peer_wa_id(), template_name, language, variables)
    wa_msg_id = resp.get("messages", [{}])[0].get("id")

    preview = f"[template: {template_name}]"
    msg = Message(
        conversation_id=conv.id,
        wa_message_id=wa_msg_id,
        direction="outbound",
        msg_type="template",
        template_name=template_name,
        body=preview,
        status="sent",
        sent_by_user_id=user_id,
    )
    db.session.add(msg)

    conv.last_message_preview = preview
    conv.last_message_at = datetime.utcnow()
    db.session.commit()

    _broadcast_new_message(conv, msg)
    return msg


def mark_conversation_read(conversation_id: int) -> None:
    conv = Conversation.query.get_or_404(conversation_id)
    conv.unread_count = 0
    db.session.commit()
    socketio.emit("conversation_read", {"conversation_id": conv.id}, namespace="/inbox")


def _broadcast_new_message(conv: Conversation, msg: Message) -> None:
    """Push new message + conversation update to all connected inbox clients."""
    payload = {
        "conversation": conv.to_dict(),
        "message": msg.to_dict(),
    }
    socketio.emit("new_message", payload, namespace="/inbox")
