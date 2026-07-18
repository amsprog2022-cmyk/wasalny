"""Phase 4b ops-layer models: complaints, SOS, bans, credits, broadcasts,
announcements, and the immutable audit log."""
from __future__ import annotations

from datetime import datetime
from app import db


# ---------- Complaints ----------

COMPLAINT_CATEGORIES = (
    "missing_item", "overcharge", "rude", "no_show",
    "wrong_route", "safety", "other",
)

COMPLAINT_STATUSES = ("open", "in_progress", "waiting_user", "resolved", "closed")

RESOLUTION_ACTIONS = (
    "refund", "credit", "warn", "suspend", "ban", "none",
)


class Complaint(db.Model):
    __tablename__ = "complaints"

    id = db.Column(db.Integer, primary_key=True)
    # Filer can be a customer, a captain, or an admin (opened proactively)
    filed_by_kind = db.Column(db.String(10), nullable=False, index=True)  # customer/driver/admin
    filed_by_id = db.Column(db.Integer, nullable=False)

    subject = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(30), default="other", nullable=False, index=True)

    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"), index=True)
    assigned_to_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    status = db.Column(db.String(20), default="open", nullable=False, index=True)
    resolution = db.Column(db.Text)
    resolution_action = db.Column(db.String(20), default="none")
    sla_breach = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    resolved_at = db.Column(db.DateTime)

    ride = db.relationship("Ride", foreign_keys=[ride_id])
    assignee = db.relationship("User", foreign_keys=[assigned_to_user_id])


class ComplaintComment(db.Model):
    __tablename__ = "complaint_comments"

    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaints.id"), nullable=False, index=True)
    author_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    body = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    author = db.relationship("User")


# ---------- SOS ----------

SOS_STATUSES = ("open", "acknowledged", "resolved")


class SosAlert(db.Model):
    __tablename__ = "sos_alerts"

    id = db.Column(db.Integer, primary_key=True)
    ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    driver_id = db.Column(db.Integer, db.ForeignKey("drivers.id"))
    message = db.Column(db.Text)

    status = db.Column(db.String(20), default="open", nullable=False, index=True)
    acknowledged_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    acknowledged_at = db.Column(db.DateTime)
    resolved_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    ride = db.relationship("Ride")
    customer = db.relationship("Customer")
    driver = db.relationship("Driver")


# ---------- Bans ----------

class Ban(db.Model):
    __tablename__ = "bans"

    id = db.Column(db.Integer, primary_key=True)
    target_kind = db.Column(db.String(10), nullable=False, index=True)  # customer/driver
    target_id = db.Column(db.Integer, nullable=False, index=True)
    reason = db.Column(db.String(200))

    banned_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    expires_at = db.Column(db.DateTime)  # null = permanent

    lifted_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    lifted_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ---------- Credit adjustments (refunds / bonuses) ----------

class CreditAdjustment(db.Model):
    __tablename__ = "credit_adjustments"

    id = db.Column(db.Integer, primary_key=True)
    target_kind = db.Column(db.String(10), nullable=False)  # customer/driver
    target_id = db.Column(db.Integer, nullable=False, index=True)
    amount_egp = db.Column(db.Numeric(8, 2), nullable=False)
    direction = db.Column(db.String(10), nullable=False)  # credit/debit
    reason = db.Column(db.String(200), nullable=False)

    from_complaint_id = db.Column(db.Integer, db.ForeignKey("complaints.id"))
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    applied_to_ride_id = db.Column(db.Integer, db.ForeignKey("rides.id"))
    expires_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ---------- Marketing broadcasts ----------

BROADCAST_KINDS = ("whatsapp_marketing", "inapp_banner")
BROADCAST_AUDIENCES = ("all_customers", "vip", "dormant", "by_zone", "custom")


class AdminBroadcast(db.Model):
    __tablename__ = "admin_broadcasts"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False)
    template_name = db.Column(db.String(120))
    message_ar = db.Column(db.Text)
    message_en = db.Column(db.Text)

    audience_filter_json = db.Column(db.Text, default="{}")

    scheduled_for = db.Column(db.DateTime)
    sent_at = db.Column(db.DateTime)
    recipient_count = db.Column(db.Integer, default=0)
    delivered_count = db.Column(db.Integer, default=0)
    failed_count = db.Column(db.Integer, default=0)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ---------- In-app announcements ----------

ANNOUNCEMENT_AUDIENCES = ("customer", "driver", "both")
ANNOUNCEMENT_PRIORITIES = ("info", "warning", "critical")


class Announcement(db.Model):
    __tablename__ = "announcements"

    id = db.Column(db.Integer, primary_key=True)
    audience = db.Column(db.String(10), nullable=False, default="both")
    title_ar = db.Column(db.String(200))
    title_en = db.Column(db.String(200))
    body_ar = db.Column(db.Text)
    body_en = db.Column(db.Text)

    starts_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ends_at = db.Column(db.DateTime)
    priority = db.Column(db.String(15), default="info", nullable=False)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ---------- Immutable audit log ----------

class AuditLog(db.Model):
    """Every admin write action. Never updated, never deleted (enforced in code)."""
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(60), nullable=False, index=True)
    target_kind = db.Column(db.String(40), index=True)
    target_id = db.Column(db.Integer, index=True)
    before_json = db.Column(db.Text)
    after_json = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    actor = db.relationship("User")
