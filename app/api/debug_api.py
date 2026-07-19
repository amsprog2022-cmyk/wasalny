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

from sqlalchemy import inspect, text

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


@debug_api_bp.get("/fcm-readiness")
def fcm_readiness():
    """Full readiness check: are we ready to send push notifications?

    Runs 8 discrete checks and returns pass/fail + overall verdict.
    Safe to expose — reveals only presence/absence, no secrets.
    """
    checks = []

    # 1. env var present
    raw = (current_app.config.get("FIREBASE_SERVICE_ACCOUNT_JSON") or "").strip()
    checks.append({
        "name": "FIREBASE_SERVICE_ACCOUNT_JSON env var set",
        "pass": bool(raw),
        "detail": f"length={len(raw)}" if raw else "MISSING — add on Railway → Variables",
    })

    # 2. env var parses as valid JSON
    parsed = None
    parse_err = None
    if raw:
        try:
            data = raw if raw.startswith("{") else __import__("base64").b64decode(raw).decode("utf-8")
            parsed = json.loads(data)
        except Exception as e:  # noqa: BLE001
            parse_err = str(e)[:200]
    checks.append({
        "name": "Service account JSON parses",
        "pass": parsed is not None,
        "detail": parse_err or (f"project={parsed.get('project_id')}, type={parsed.get('type')}"
                                 if parsed else "n/a"),
    })

    # 3. service account has private key
    has_key = bool(parsed and parsed.get("private_key") and parsed.get("type") == "service_account")
    checks.append({
        "name": "Service account has private_key + correct type",
        "pass": has_key,
        "detail": "OK" if has_key else "JSON missing private_key or type != service_account",
    })

    # 4. firebase-admin library installed
    try:
        import firebase_admin  # noqa: WPS433
        fb_installed = True
    except ImportError:
        firebase_admin = None
        fb_installed = False
    checks.append({
        "name": "firebase-admin library installed",
        "pass": fb_installed,
        "detail": "OK" if fb_installed else "pip install firebase-admin==6.5.0",
    })

    # 5. firebase-admin initialized at boot
    fb_initialized = fb_installed and bool(firebase_admin._apps)
    loaded_project = None
    if fb_initialized:
        default = firebase_admin.get_app()
        loaded_project = getattr(default, "project_id", None) or default.options.get("projectId")
    checks.append({
        "name": "firebase-admin initialized on boot",
        "pass": fb_initialized,
        "detail": f"project={loaded_project}" if fb_initialized else "boot log should show '[firebase] Admin SDK initialized'",
    })

    # 6. DB columns present on customers + drivers
    inspector = inspect(db.engine)
    required_cols = {"fcm_token", "fcm_platform", "fcm_updated_at"}
    customer_cols = {c["name"] for c in inspector.get_columns("customers")}
    driver_cols = {c["name"] for c in inspector.get_columns("drivers")}
    cust_missing = required_cols - customer_cols
    driv_missing = required_cols - driver_cols
    checks.append({
        "name": "DB migration: fcm_* columns exist on customers + drivers",
        "pass": not cust_missing and not driv_missing,
        "detail": (f"customers missing: {cust_missing}, drivers missing: {driv_missing}"
                   if cust_missing or driv_missing else "all 3 columns on both tables"),
    })

    # 7. at least one device has registered a token
    cust_with_tok = db.session.query(db.func.count(Customer.id)).filter(
        Customer.fcm_token.isnot(None)
    ).scalar()
    driv_with_tok = db.session.query(db.func.count(Driver.id)).filter(
        Driver.fcm_token.isnot(None)
    ).scalar()
    checks.append({
        "name": "At least one device has registered an FCM token",
        "pass": (cust_with_tok + driv_with_tok) > 0,
        "detail": f"customers={cust_with_tok}, drivers={driv_with_tok}",
    })

    # 8. DEBUG_TOKEN set (needed for the /send test route)
    debug_tok_set = bool(os.getenv("DEBUG_TOKEN", "").strip())
    checks.append({
        "name": "DEBUG_TOKEN env var set (for /firebase-send)",
        "pass": debug_tok_set,
        "detail": "OK" if debug_tok_set else "add DEBUG_TOKEN=<random-string> on Railway",
    })

    # Overall verdict
    core_ready = all(c["pass"] for c in checks[:6])  # 1-6 are the mandatory ones
    full_ready = all(c["pass"] for c in checks)

    verdict = (
        "✅ READY — you can send push notifications now"
        if core_ready and cust_with_tok + driv_with_tok > 0
        else "⚠️  PARTIAL — Firebase is wired but no devices have logged in yet to register tokens"
        if core_ready
        else "❌ NOT READY — see failing checks below"
    )

    return jsonify({
        "verdict": verdict,
        "core_ready": core_ready,
        "fully_ready": full_ready,
        "checks": checks,
        "next_action": _next_action(checks, cust_with_tok, driv_with_tok, debug_tok_set),
    })


@debug_api_bp.get("/whatsapp-status")
def whatsapp_status():
    """Show whether WhatsApp env vars are set on this Railway instance.

    Reveals only presence + last 4 chars of the access token (safe).
    No secrets exposed.
    """
    tok = current_app.config.get("WHATSAPP_ACCESS_TOKEN") or ""
    phone_id = current_app.config.get("WHATSAPP_PHONE_NUMBER_ID") or ""
    business_id = current_app.config.get("WHATSAPP_BUSINESS_ACCOUNT_ID") or ""
    verify = current_app.config.get("WHATSAPP_VERIFY_TOKEN") or ""
    app_secret = current_app.config.get("WHATSAPP_APP_SECRET") or ""

    return jsonify({
        "access_token_set": bool(tok),
        "access_token_length": len(tok),
        "access_token_last4": tok[-4:] if len(tok) > 8 else None,
        "phone_number_id_set": bool(phone_id),
        "phone_number_id": phone_id,
        "business_account_id_set": bool(business_id),
        "business_account_id": business_id,
        "verify_token_set": bool(verify),
        "app_secret_set": bool(app_secret),
        "api_version": current_app.config.get("WHATSAPP_API_VERSION"),
        "ready": all([tok, phone_id, business_id, verify]),
    })


def _next_action(checks, cust_tokens, driv_tokens, debug_tok_set):
    """Give the user one clear thing to do next."""
    for c in checks[:6]:
        if not c["pass"]:
            return f"Fix: {c['name']} — {c['detail']}"
    if cust_tokens + driv_tokens == 0:
        return "Log into the customer or captain app on your phone so it registers an FCM token"
    if not debug_tok_set:
        return "Optional: set DEBUG_TOKEN on Railway if you want to use /firebase-send. Everything else is ready."
    return "All checks passed. Try sending: POST /api/v1/debug/firebase-send with X-Debug-Token header."
