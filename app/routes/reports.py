"""Admin reports & analytics. One page, everything.

Includes CSV export for each section (?export=csv).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from decimal import Decimal

from flask import Blueprint, Response, render_template, request
from flask_login import login_required
from sqlalchemy import func, case

from app import db
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.ride import Ride
from app.models.zone import Zone
from app.models.ops import Complaint


reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def _range_from_request() -> tuple[datetime, datetime, str]:
    """Return (start, end, label) based on ?range=today|7d|30d (default 7d)."""
    rng = request.args.get("range", "7d")
    now = datetime.utcnow()
    end = now
    if rng == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif rng == "30d":
        start = now - timedelta(days=30)
    else:
        rng = "7d"
        start = now - timedelta(days=7)
    return start, end, rng


def _financial(start: datetime, end: datetime) -> dict:
    completed = Ride.query.filter(
        Ride.status == "completed", Ride.completed_at >= start, Ride.completed_at < end
    )
    trips = completed.count()
    gross = completed.with_entities(func.sum(Ride.price_egp)).scalar() or Decimal("0")
    commission = completed.with_entities(func.sum(Ride.commission_egp)).scalar() or Decimal("0")
    no_show_fees = (
        Ride.query.filter(
            Ride.status == "cancelled_no_show",
            Ride.cancelled_at >= start,
            Ride.cancelled_at < end,
        )
        .with_entities(func.sum(Ride.no_show_fee_egp))
        .scalar()
        or Decimal("0")
    )
    return {
        "trips": trips,
        "gross": float(gross),
        "commission": float(commission),
        "captain_net": float(gross - commission),
        "no_show_fees": float(no_show_fees),
    }


def _top_captains(start: datetime, end: datetime, limit: int = 10) -> list[dict]:
    rows = (
        db.session.query(
            Driver.id,
            Driver.name,
            Driver.rating,
            func.count(Ride.id).label("trips"),
            func.sum(Ride.price_egp).label("gross"),
        )
        .join(Ride, Ride.driver_id == Driver.id)
        .filter(Ride.status == "completed", Ride.completed_at >= start, Ride.completed_at < end)
        .group_by(Driver.id)
        .order_by(func.count(Ride.id).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "name": r.name,
            "rating": float(r.rating) if r.rating is not None else None,
            "trips": r.trips,
            "gross": float(r.gross or 0),
        }
        for r in rows
    ]


def _customer_summary(start: datetime, end: datetime) -> dict:
    total_customers = Customer.query.count()
    active_ids = {
        r.customer_id
        for r in db.session.query(Ride.customer_id)
        .filter(Ride.created_at >= start, Ride.created_at < end)
        .distinct()
        .all()
    }
    return {"total": total_customers, "active": len(active_ids)}


def _popular_routes(start: datetime, end: datetime, limit: int = 10) -> list[dict]:
    z_ar = {z.id: z.name_ar for z in Zone.query.all()}
    rows = (
        db.session.query(
            Ride.from_zone_id,
            Ride.to_zone_id,
            func.count(Ride.id).label("trips"),
        )
        .filter(Ride.status == "completed", Ride.completed_at >= start, Ride.completed_at < end)
        .group_by(Ride.from_zone_id, Ride.to_zone_id)
        .order_by(func.count(Ride.id).desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "from": z_ar.get(r.from_zone_id, "—"),
            "to": z_ar.get(r.to_zone_id, "—"),
            "trips": r.trips,
        }
        for r in rows
    ]


def _complaint_summary(start: datetime, end: datetime) -> dict:
    total = Complaint.query.filter(Complaint.created_at >= start, Complaint.created_at < end).count()
    open_ = Complaint.query.filter(
        Complaint.created_at >= start,
        Complaint.created_at < end,
        Complaint.status.in_(("open", "in_progress", "waiting_user")),
    ).count()
    by_category = dict(
        db.session.query(Complaint.category, func.count(Complaint.id))
        .filter(Complaint.created_at >= start, Complaint.created_at < end)
        .group_by(Complaint.category)
        .all()
    )
    return {"total": total, "open": open_, "by_category": by_category}


@reports_bp.route("/")
@login_required
def index():
    start, end, rng = _range_from_request()

    if request.args.get("export") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["metric", "value"])
        for k, v in _financial(start, end).items():
            w.writerow([k, v])
        w.writerow([])
        w.writerow(["top captains"])
        w.writerow(["id", "name", "trips", "gross_egp", "rating"])
        for row in _top_captains(start, end):
            w.writerow([row["id"], row["name"], row["trips"], row["gross"], row["rating"]])
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=wassalny_report_{rng}.csv"},
        )

    return render_template(
        "reports/index.html",
        rng=rng,
        start=start,
        end=end,
        financial=_financial(start, end),
        top_captains=_top_captains(start, end),
        customers=_customer_summary(start, end),
        popular_routes=_popular_routes(start, end),
        complaints=_complaint_summary(start, end),
    )
