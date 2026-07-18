"""Admin home / live overview — matches design screen 01_admin_dashboard_home.png."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from flask import Blueprint, render_template
from flask_login import login_required

from app import db
from app.extensions import get_redis
from app.models.ai_session import AdminAlert
from app.models.customer import Customer
from app.models.driver import Driver
from app.models.ride import Ride, RideStatusEvent
from app.models.zone import Zone
from flask import current_app
from sqlalchemy import func


dashboard_bp = Blueprint("dashboard", __name__)


def _live_counters() -> dict:
    r = get_redis(current_app.config.get("REDIS_URL"))

    # Captains online right now: any driver:{id}:status with online=1 that we can enumerate
    # via zone zsets — deduped.
    zones = Zone.query.filter_by(is_active=True).all()
    online_ids: set[int] = set()
    for z in zones:
        for did in r.zrange(f"zone:{z.id}:available_drivers", 0, -1):
            try:
                online_ids.add(int(did))
            except (TypeError, ValueError):
                continue
    captains_online = len(online_ids)

    active_rides = Ride.query.filter(
        Ride.status.in_(("broadcasting", "assigned", "started"))
    ).count()
    pending_broadcasts = Ride.query.filter_by(status="broadcasting").count()
    open_alerts = AdminAlert.query.filter_by(status="open").count()
    ai_handoffs = AdminAlert.query.filter_by(status="open", kind="ai_handoff").count()

    # Financial today
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    completed_today = Ride.query.filter(
        Ride.status == "completed", Ride.completed_at >= today_start
    )
    gross_today = completed_today.with_entities(func.sum(Ride.price_egp)).scalar() or Decimal("0")
    commission_today = completed_today.with_entities(func.sum(Ride.commission_egp)).scalar() or Decimal("0")
    trips_today = completed_today.count()

    return {
        "captains_online": captains_online,
        "active_rides": active_rides,
        "pending_broadcasts": pending_broadcasts,
        "open_alerts": open_alerts,
        "ai_handoffs": ai_handoffs,
        "trips_today": trips_today,
        "gross_today": float(gross_today),
        "commission_today": float(commission_today),
    }


def _recent_activity(limit: int = 15) -> list:
    events = (
        RideStatusEvent.query.order_by(RideStatusEvent.created_at.desc())
        .limit(limit * 3)
        .all()
    )
    ride_ids = {e.ride_id for e in events}
    rides = {r.id: r for r in Ride.query.filter(Ride.id.in_(ride_ids)).all()}
    out = []
    for e in events:
        ride = rides.get(e.ride_id)
        if ride is None:
            continue
        out.append(
            {
                "when": e.created_at,
                "event": e.event,
                "ride_id": e.ride_id,
                "from_zone": ride.from_zone.name_ar if ride.from_zone else "—",
                "to_zone": ride.to_zone.name_ar if ride.to_zone else "—",
                "driver_name": ride.driver.name if ride.driver else None,
            }
        )
        if len(out) >= limit:
            break
    return out


def _revenue_last_7_days() -> list[dict]:
    """Return revenue per day for the last 7 days (oldest → newest)."""
    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    for i in range(6, -1, -1):
        day_start = today - timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        total = (
            Ride.query.filter(
                Ride.status == "completed",
                Ride.completed_at >= day_start,
                Ride.completed_at < day_end,
            )
            .with_entities(func.sum(Ride.price_egp))
            .scalar()
            or Decimal("0")
        )
        days.append({"day": day_start.strftime("%a"), "gross": float(total)})
    return days


@dashboard_bp.route("/")
@login_required
def home():
    return render_template(
        "dashboard/home.html",
        counters=_live_counters(),
        activity=_recent_activity(),
        revenue=_revenue_last_7_days(),
    )
