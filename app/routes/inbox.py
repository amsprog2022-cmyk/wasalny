from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy import or_, func

from app import db
from app.models import Conversation, Message, MessageTemplate, Customer, Driver
from app.services import inbox as inbox_svc
from app.services.whatsapp import WhatsAppError

inbox_bp = Blueprint("inbox", __name__, url_prefix="/inbox")


@inbox_bp.route("/")
@login_required
def index():
    kind = request.args.get("kind", "all")
    q_search = (request.args.get("q") or "").strip()
    unread_only = request.args.get("unread") == "1"
    date_filter = request.args.get("date") or ""   # today / week / month / ''

    q = Conversation.query
    if kind in ("customer", "driver"):
        q = q.filter_by(kind=kind)

    if unread_only:
        q = q.filter(Conversation.unread_count > 0)

    if date_filter == "today":
        q = q.filter(Conversation.last_message_at >= datetime.utcnow().date())
    elif date_filter == "week":
        q = q.filter(Conversation.last_message_at >= datetime.utcnow() - timedelta(days=7))
    elif date_filter == "month":
        q = q.filter(Conversation.last_message_at >= datetime.utcnow() - timedelta(days=30))

    # Search: match customer name/phone OR driver name/phone OR any message body
    if q_search:
        like = f"%{q_search}%"
        # Find conversations where a Message body contains the term
        conv_ids_with_msg = {
            m.conversation_id
            for m in Message.query.filter(Message.body.ilike(like))
            .with_entities(Message.conversation_id).limit(500).all()
        }
        cust_ids = {c.id for c in Customer.query.filter(
            or_(Customer.name.ilike(like), Customer.wa_id.ilike(like))
        ).limit(200).all()}
        driv_ids = {d.id for d in Driver.query.filter(
            or_(Driver.name.ilike(like), Driver.wa_id.ilike(like))
        ).limit(200).all()}
        q = q.filter(or_(
            Conversation.id.in_(conv_ids_with_msg) if conv_ids_with_msg else False,
            Conversation.customer_id.in_(cust_ids) if cust_ids else False,
            Conversation.driver_id.in_(driv_ids) if driv_ids else False,
        ))

    conversations = q.order_by(Conversation.last_message_at.desc()).limit(200).all()
    templates = MessageTemplate.query.filter_by(approved=True).all()

    # Totals for filter chips
    total_all = Conversation.query.count()
    total_customer = Conversation.query.filter_by(kind="customer").count()
    total_driver = Conversation.query.filter_by(kind="driver").count()
    total_unread = Conversation.query.filter(Conversation.unread_count > 0).count()

    return render_template(
        "inbox/index.html",
        conversations=conversations,
        templates=templates,
        active_kind=kind,
        q=q_search,
        unread_only=unread_only,
        date_filter=date_filter,
        totals={
            "all": total_all,
            "customer": total_customer,
            "driver": total_driver,
            "unread": total_unread,
        },
    )


@inbox_bp.route("/<int:conv_id>/messages")
@login_required
def messages(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    msgs = conv.messages.limit(200).all()
    return jsonify(
        {
            "conversation": conv.to_dict(),
            "messages": [m.to_dict() for m in msgs],
        }
    )


@inbox_bp.route("/<int:conv_id>/read", methods=["POST"])
@login_required
def mark_read(conv_id):
    inbox_svc.mark_conversation_read(conv_id)
    return "", 204


@inbox_bp.route("/<int:conv_id>/assign", methods=["POST"])
@login_required
def assign(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    conv.assignee_id = current_user.id
    db.session.commit()
    return jsonify(conv.to_dict())


@inbox_bp.route("/<int:conv_id>/send", methods=["POST"])
@login_required
def send(conv_id):
    body = (request.json or {}).get("body", "").strip()
    if not body:
        return jsonify({"error": "empty body"}), 400
    try:
        msg = inbox_svc.send_outbound_text(conv_id, body, user_id=current_user.id)
    except ValueError as e:
        return jsonify({"error": str(e), "code": "window_closed"}), 409
    except WhatsAppError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(msg.to_dict())


@inbox_bp.route("/<int:conv_id>/send-template", methods=["POST"])
@login_required
def send_template(conv_id):
    data = request.json or {}
    template_name = data.get("template_name")
    variables = data.get("variables", [])
    language = data.get("language", "ar")
    if not template_name:
        return jsonify({"error": "template_name required"}), 400
    try:
        msg = inbox_svc.send_outbound_template(
            conv_id, template_name, language, variables, user_id=current_user.id
        )
    except WhatsAppError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(msg.to_dict())
