from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import login_required, current_user

from app import db
from app.models import Conversation, Message, MessageTemplate
from app.services import inbox as inbox_svc
from app.services.whatsapp import WhatsAppError

inbox_bp = Blueprint("inbox", __name__, url_prefix="/inbox")


@inbox_bp.route("/")
@login_required
def index():
    kind = request.args.get("kind", "all")
    q = Conversation.query
    if kind in ("customer", "driver"):
        q = q.filter_by(kind=kind)
    conversations = q.order_by(Conversation.last_message_at.desc()).limit(100).all()
    templates = MessageTemplate.query.filter_by(approved=True).all()
    return render_template(
        "inbox/index.html",
        conversations=conversations,
        templates=templates,
        active_kind=kind,
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
