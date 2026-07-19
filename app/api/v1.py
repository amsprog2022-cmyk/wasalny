"""JWT-based REST API for the customer and captain mobile apps.

Auth model:
  - Team users log in with email + password → JWT access token (7d) + refresh (30d).
  - Captains authenticate via their WhatsApp phone number + a one-time code we send them
    (bootstrap endpoint below; simplified for MVP).
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    jwt_required,
    get_jwt_identity,
    get_jwt,
)

from app import db
from app.models import User, Driver, Conversation, Message, RideRequest, Customer
from app.services import inbox as inbox_svc
from app.services.whatsapp import WhatsAppError

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


# ---------- Auth ----------

@api_v1_bp.route("/auth/team/login", methods=["POST"])
def team_login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password) or not user.is_active_user:
        return jsonify({"error": "invalid_credentials"}), 401

    claims = {"role": user.role, "kind": "team"}
    return jsonify(
        {
            "access_token": create_access_token(identity=str(user.id), additional_claims=claims),
            "refresh_token": create_refresh_token(identity=str(user.id), additional_claims=claims),
            "user": user.to_dict(),
        }
    )


@api_v1_bp.route("/auth/driver/login", methods=["POST"])
def driver_login():
    """Captain login: phone + password. Rejects pending/rejected captains."""
    data = request.json or {}
    wa_id = (data.get("wa_id") or "").strip().lstrip("+")
    password = data.get("password") or ""
    if not wa_id:
        return jsonify({"error": "wa_id required"}), 400

    driver = Driver.query.filter_by(wa_id=wa_id).first()
    if not driver:
        return jsonify({"error": "not_found"}), 404

    if driver.deleted_at is not None:
        return jsonify({"error": "account_deleted", "message_ar": "الحساب اتحذف."}), 403
    if driver.approval_status == "pending":
        return jsonify({"error": "pending_approval", "message_ar": "حسابك تحت المراجعة. برجاء الانتظار."}), 403
    if driver.approval_status == "rejected" or not driver.is_active:
        return jsonify({"error": "not_active", "message_ar": "الحساب غير مفعل."}), 403

    # If a password is set on the driver, enforce it. For legacy/dev drivers
    # created without a password, allow phone-only match (backward compat).
    if driver.password_hash and not driver.check_password(password):
        return jsonify({"error": "invalid_credentials"}), 401

    claims = {"kind": "driver"}
    return jsonify(
        {
            "access_token": create_access_token(identity=f"driver:{driver.id}", additional_claims=claims),
            "driver": driver.to_dict(),
            "must_change_password": bool(driver.must_change_password),
        }
    )


@api_v1_bp.route("/auth/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh():
    identity = get_jwt_identity()
    claims = get_jwt()
    new_token = create_access_token(
        identity=identity,
        additional_claims={"role": claims.get("role"), "kind": claims.get("kind")},
    )
    return jsonify({"access_token": new_token})


# ---------- Team endpoints ----------

def _require_team():
    if get_jwt().get("kind") != "team":
        return jsonify({"error": "team_token_required"}), 403


@api_v1_bp.route("/conversations")
@jwt_required()
def list_conversations():
    err = _require_team()
    if err: return err
    kind = request.args.get("kind")
    q = Conversation.query
    if kind in ("customer", "driver"):
        q = q.filter_by(kind=kind)
    convs = q.order_by(Conversation.last_message_at.desc()).limit(100).all()
    return jsonify([c.to_dict() for c in convs])


@api_v1_bp.route("/conversations/<int:conv_id>/messages")
@jwt_required()
def list_messages(conv_id):
    err = _require_team()
    if err: return err
    conv = Conversation.query.get_or_404(conv_id)
    msgs = conv.messages.limit(200).all()
    return jsonify(
        {"conversation": conv.to_dict(), "messages": [m.to_dict() for m in msgs]}
    )


@api_v1_bp.route("/conversations/<int:conv_id>/messages", methods=["POST"])
@jwt_required()
def send_message(conv_id):
    err = _require_team()
    if err: return err
    data = request.json or {}
    user_id = int(get_jwt_identity())

    try:
        if data.get("template_name"):
            msg = inbox_svc.send_outbound_template(
                conv_id,
                data["template_name"],
                data.get("language", "ar"),
                data.get("variables", []),
                user_id=user_id,
            )
        else:
            msg = inbox_svc.send_outbound_text(conv_id, data.get("body", ""), user_id=user_id)
    except ValueError as e:
        return jsonify({"error": str(e), "code": "window_closed"}), 409
    except WhatsAppError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify(msg.to_dict()), 201


# Ride endpoints moved to app/api/rides_api.py (Phase 2 Ride model).


# ---------- Driver-facing endpoints ----------

@api_v1_bp.route("/driver/me")
@jwt_required()
def driver_me():
    if get_jwt().get("kind") != "driver":
        return jsonify({"error": "driver_token_required"}), 403
    driver_id = int(get_jwt_identity().split(":", 1)[1])
    driver = Driver.query.get_or_404(driver_id)
    return jsonify(driver.to_dict())


@api_v1_bp.route("/driver/rides")
@jwt_required()
def driver_rides():
    if get_jwt().get("kind") != "driver":
        return jsonify({"error": "driver_token_required"}), 403
    from app.models.ride import Ride
    driver_id = int(get_jwt_identity().split(":", 1)[1])
    rides = (
        Ride.query.filter_by(driver_id=driver_id)
        .order_by(Ride.created_at.desc())
        .limit(50)
        .all()
    )
    return jsonify([r.to_dict() for r in rides])


@api_v1_bp.route("/driver/status", methods=["POST"])
@jwt_required()
def driver_set_status():
    if get_jwt().get("kind") != "driver":
        return jsonify({"error": "driver_token_required"}), 403
    driver_id = int(get_jwt_identity().split(":", 1)[1])
    driver = Driver.query.get_or_404(driver_id)
    new_status = (request.json or {}).get("status")
    if new_status not in ("available", "busy", "offline"):
        return jsonify({"error": "invalid_status"}), 400
    driver.status = new_status
    db.session.commit()
    return jsonify(driver.to_dict())
