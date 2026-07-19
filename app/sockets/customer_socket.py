"""Real-time Socket.IO namespace for the customer Flutter app.

Events (server → client):
  trip_status_changed  {ride: {...}}
  trip_assigned        {ride: {...}, driver: {...}}
  trip_cancelled       {ride: {...}, reason: "..."}

Auth: JWT in query string (?token=…) or Socket.IO auth payload.
"""
from __future__ import annotations

from flask import request
from flask_socketio import Namespace, emit, disconnect, join_room
from flask_jwt_extended import decode_token

from app import socketio, db
from app.models.customer import Customer


NAMESPACE = "/customer"


def _customer_id_from_token() -> int | None:
    token = request.args.get("token")
    if not token:
        auth = getattr(request, "auth", None) or {}
        if isinstance(auth, dict):
            token = auth.get("token")
    if not token:
        return None
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if payload.get("kind") != "customer":
        return None
    sub = payload.get("sub") or ""
    if isinstance(sub, str) and sub.startswith("customer:"):
        try:
            return int(sub.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


class CustomerNamespace(Namespace):
    def on_connect(self):
        cid = _customer_id_from_token()
        if not cid:
            emit("customer:error", {"message": "unauthenticated"})
            disconnect()
            return
        if db.session.get(Customer, cid) is None:
            emit("customer:error", {"message": "customer not found"})
            disconnect()
            return
        join_room(f"customer:{cid}")
        emit("customer:connected", {"customer_id": cid})

    def on_disconnect(self, reason=None):
        # reason arg added in python-socketio 5.12+; accept it to stay compatible.
        pass


socketio.on_namespace(CustomerNamespace(NAMESPACE))
