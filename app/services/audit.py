"""Audit log helper — call `record()` from any admin-side write.

Keeps the API tiny on purpose so it's easy to sprinkle everywhere without
noise. Full request context (IP, UA) is picked up automatically when
called inside a Flask request.
"""
from __future__ import annotations

import json
from typing import Any

from flask import has_request_context, request
from flask_login import current_user

from app import db
from app.models.ops import AuditLog


def record(
    action: str,
    *,
    target_kind: str | None = None,
    target_id: int | None = None,
    before: Any = None,
    after: Any = None,
) -> None:
    """Write one audit row. Commits immediately with its own row — safe to
    call from within another transaction as long as db.session is open.
    """
    actor_id = None
    ip = None
    ua = None
    if has_request_context():
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        ua = (request.user_agent.string or "")[:300] if request.user_agent else None
        try:
            if current_user.is_authenticated:
                actor_id = current_user.id
        except Exception:
            actor_id = None

    row = AuditLog(
        actor_user_id=actor_id,
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        before_json=json.dumps(before, default=str, ensure_ascii=False) if before is not None else None,
        after_json=json.dumps(after, default=str, ensure_ascii=False) if after is not None else None,
        ip_address=ip,
        user_agent=ua,
    )
    db.session.add(row)
    # Flush now so caller can rollback their own tx without losing the audit
    db.session.flush()
