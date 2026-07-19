"""Debug endpoints for verifying Firebase Cloud Messaging setup on Railway.

Reveals only booleans + non-sensitive metadata (project_id is public per
Firebase docs). The /send route needs a DEBUG_TOKEN header so nobody but
you can trigger pushes. Remove this file once production is stable.
"""
from __future__ import annotations

import hmac
import json
import os

from flask import Blueprint, current_app, jsonify, request

from app import db
from app.models.customer import Customer
from app.models.driver import Driver
from app.services import push_notifications as push

debug_api_bp = Blueprint("debug_api", __name__, url_prefix="/api/v1/debug")


def _require_debug_token() -> bool:
    """Constant-time comparison against DEBUG_TOKEN env var.

    Returning False = unauthorised; the view should short-circuit.
    """
    expected = os.getenv("DEBUG_TOKEN", "")
    if not expected:
        return False
    provided = request.headers.get("X-Debug-Token", "") or request.args.get("token", "")
    return hmac.compare_digest(expected, provided)


@debug_api_bp.get("/firebase-status")
def firebase_status():
    """Show the state of Firebase config on this Railway instance.

    Safe to expose — only reveals whether env vars are set and what
    project_id was loaded (project_id is public).
    """
    raw = current_app.config.get("FIREBASE_SERVICE_ACCOUNT_JSON") or ""
    raw = raw.strip()

    info = {
        "env_var_present": bool(raw),
        "env_var_length": len(raw),
        "env_var_looks_like_base64": bool(raw) and not raw.startswith("{"),
        "firebase_project_id_config": current_app.config.get("FIREBASE_PROJECT_ID"),
        "firebase_admin_installed": False,
        "firebase_admin_initialized": False,
        "firebase_project_id_loaded": None,
        "load_error": None,
    }

    try:
        import firebase_admin  # noqa: WPS433
        info["firebase_admin_installed"] = True
        info["firebase_admin_initialized"] = bool(firebase_admin._apps)
        if firebase_admin._apps:
            default = firebase_admin.get_app()
            proj = getattr(default, "project_id", None) or default.options.get("projectId")
            info["firebase_project_id_loaded"] = proj
    except ImportError as e:
        info["load_error"] = f"firebase-admin not installed: {e}"

    # Optional: try to decode the env var without initializing, to help debug
    # bad-JSON / bad-base64 issues without leaking the private key
    if raw:
        try:
            data = raw if raw.startswith("{") else __import__("base64").b64decode(raw).decode("utf-8")
            parsed = json.loads(data)
            info["parsed_project_id"] = parsed.get("project_id")
            info["parsed_client_email"] = parsed.get("client_email")
            info["parsed_has_private_key"] = bool(parsed.get("private_key"))
            info["parsed_type"] = parsed.get("type")
        except Exception as e:  # noqa: BLE001
            info["parse_error"] = str(e)[:200]

    # Show token counts so we know if any devices have registered
    info["customers_with_fcm_token"] = db.session.query(
        db.func.count(Customer.id)
    ).filter(Customer.fcm_token.isnot(None)).scalar()
    info["drivers_with_fcm_token"] = db.session.query(
        db.func.count(Driver.id)
    ).filter(Driver.fcm_token.isnot(None)).scalar()

    return jsonify(info)


@debug_api_bp.post("/firebase-send")
def firebase_send():
    """Send a test push to a specific customer/driver or raw token.

    Auth: X-Debug-Token header must equal DEBUG_TOKEN env var.
    Body (JSON): one of
      { "token": "<raw fcm token>", "title": "...", "body": "..." }
      { "customer_id": 123, "title": "...", "body": "..." }
      { "driver_id":   456, "title": "...", "body": "..." }
    """
    if not _require_debug_token():
        return jsonify({"error": "unauthorized",
                        "hint": "Set DEBUG_TOKEN on Railway, then send it as X-Debug-Token header."}), 401

    data = request.json or {}
    title = (data.get("title") or "Wassalny test").strip()
    body = (data.get("body") or "لو وصلتك الرسالة يبقى الـ FCM شغال ✅").strip()
    payload = data.get("data") or {"kind": "debug_test"}

    result = {"title": title, "body": body, "delivered": False, "target": None}

    if data.get("token"):
        raw_token = data["token"]
        ok = push._send(raw_token, title, body, payload)
        result["target"] = f"raw_token:{raw_token[:12]}..."
        result["delivered"] = ok
    elif data.get("customer_id"):
        cid = int(data["customer_id"])
        c = db.session.get(Customer, cid)
        result["target"] = f"customer:{cid}"
        result["customer_has_token"] = bool(c and c.fcm_token)
        if c and c.fcm_token:
            result["delivered"] = push.send_to_customer(cid, title=title, body=body, data=payload)
    elif data.get("driver_id"):
        did = int(data["driver_id"])
        d = db.session.get(Driver, did)
        result["target"] = f"driver:{did}"
        result["driver_has_token"] = bool(d and d.fcm_token)
        if d and d.fcm_token:
            result["delivered"] = push.send_to_driver(did, title=title, body=body, data=payload)
    else:
        return jsonify({"error": "must provide one of: token, customer_id, driver_id"}), 400

    return jsonify(result)
