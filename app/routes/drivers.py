"""Admin captain management (Decision #13: admin-created only).

Captain lifecycle:
  admin creates → password issued → captain logs in via app → forced change on first login
  admin can: suspend, unsuspend, ban, force-offline, reset password, adjust rating
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
import phonenumbers

from app import db
from app.extensions import get_redis
from app.models.driver import Driver
from app.models.ride import Ride
from app.services import availability as av
from flask import current_app
from sqlalchemy import func


drivers_bp = Blueprint("drivers", __name__, url_prefix="/drivers")


def _normalize_wa_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        num = phonenumbers.parse(raw, "EG")
        return f"{num.country_code}{num.national_number}"
    except phonenumbers.NumberParseException:
        return raw.lstrip("+")


def _random_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _presence_map(drivers: list[Driver]) -> dict[int, dict]:
    """Fetch live Redis presence for a batch of captains."""
    return {d.id: av.get_presence(d.id).__dict__ for d in drivers}


# ---------- list ----------

@drivers_bp.route("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    filter_ = request.args.get("filter", "all")

    query = Driver.query
    if q:
        query = query.filter(
            (Driver.name.ilike(f"%{q}%")) | (Driver.wa_id.ilike(f"%{q}%"))
        )
    if filter_ == "pending":
        query = query.filter(Driver.approval_status == "pending")
    elif filter_ == "suspended":
        query = query.filter(Driver.discipline_status == "suspended")
    elif filter_ == "warned":
        query = query.filter(Driver.discipline_status == "warned")
    elif filter_ == "top":
        query = query.order_by(Driver.rating.desc()).limit(50)

    drivers = query.order_by(Driver.created_at.desc()).limit(200).all()
    pending_count = Driver.query.filter_by(approval_status="pending").count()
    presence = _presence_map(drivers)
    return render_template(
        "drivers/index.html",
        drivers=drivers,
        presence=presence,
        q=q,
        active_filter=filter_,
        pending_count=pending_count,
    )


# ---------- add wizard ----------

@drivers_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("drivers.index"))

    if request.method == "POST":
        wa_id = _normalize_wa_id(request.form.get("wa_id", ""))
        name = request.form.get("name", "").strip()
        if not wa_id or not name:
            flash("Name and phone number are required.", "error")
            return render_template("drivers/form.html", driver=None, generated_password=None)

        if Driver.query.filter_by(wa_id=wa_id).first():
            flash("A captain with this number already exists.", "error")
            return render_template("drivers/form.html", driver=None, generated_password=None)

        password = _random_password()
        driver = Driver(
            wa_id=wa_id,
            name=name,
            national_id=request.form.get("national_id", "").strip() or None,
            license_number=request.form.get("license_number", "").strip() or None,
            car_model=request.form.get("car_model", "").strip() or None,
            car_plate=request.form.get("car_plate", "").strip() or None,
            car_color=request.form.get("car_color", "").strip() or None,
            category=request.form.get("category", "economy"),
            notes=request.form.get("notes", "").strip() or None,
            created_by_admin_id=current_user.id,
            must_change_password=True,
        )
        driver.set_password(password)
        db.session.add(driver)
        db.session.commit()

        flash(f"Captain {name} added. Password: {password} — send via WhatsApp.", "success")
        return redirect(url_for("drivers.show", driver_id=driver.id))

    return render_template("drivers/form.html", driver=None, generated_password=None)


# ---------- profile ----------

@drivers_bp.route("/<int:driver_id>")
@login_required
def show(driver_id: int):
    driver = Driver.query.get_or_404(driver_id)
    recent = (
        Ride.query.filter_by(driver_id=driver_id)
        .order_by(Ride.created_at.desc())
        .limit(20)
        .all()
    )
    stats = (
        db.session.query(
            func.count(Ride.id).label("total"),
            func.sum(Ride.price_egp).label("gross"),
            func.sum(Ride.commission_egp).label("commission"),
        )
        .filter(Ride.driver_id == driver_id, Ride.status == "completed")
        .first()
    )
    presence = av.get_presence(driver.id).__dict__
    return render_template(
        "drivers/show.html",
        driver=driver,
        recent=recent,
        stats={
            "total": stats.total or 0,
            "gross": float(stats.gross or 0),
            "commission": float(stats.commission or 0),
            "net": float((stats.gross or 0) - (stats.commission or 0)),
        },
        presence=presence,
    )


# ---------- actions ----------

@drivers_bp.route("/<int:driver_id>/suspend", methods=["POST"])
@login_required
def suspend(driver_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("drivers.show", driver_id=driver_id))
    driver = Driver.query.get_or_404(driver_id)
    hours = int(request.form.get("hours") or 24)
    driver.discipline_status = "suspended"
    driver.suspended_until = datetime.utcnow() + timedelta(hours=hours)
    db.session.commit()
    av.set_offline(driver.id)
    flash(f"Captain suspended for {hours} h.", "success")
    return redirect(url_for("drivers.show", driver_id=driver_id))


@drivers_bp.route("/<int:driver_id>/unsuspend", methods=["POST"])
@login_required
def unsuspend(driver_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("drivers.show", driver_id=driver_id))
    driver = Driver.query.get_or_404(driver_id)
    driver.discipline_status = "active"
    driver.suspended_until = None
    db.session.commit()
    flash("Captain unsuspended.", "success")
    return redirect(url_for("drivers.show", driver_id=driver_id))


@drivers_bp.route("/<int:driver_id>/force-offline", methods=["POST"])
@login_required
def force_offline(driver_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("drivers.show", driver_id=driver_id))
    driver = Driver.query.get_or_404(driver_id)
    av.set_offline(driver.id)
    # Also clear any stuck ride lock
    r = get_redis(current_app.config.get("REDIS_URL"))
    r.delete(f"driver:{driver.id}:current_ride")
    flash("Captain forced offline. Any stuck ride lock cleared.", "success")
    return redirect(url_for("drivers.show", driver_id=driver_id))


@drivers_bp.route("/<int:driver_id>/approve", methods=["POST"])
@login_required
def approve(driver_id: int):
    if not current_user.is_admin:
        flash("للأدمن بس.", "error")
        return redirect(url_for("drivers.show", driver_id=driver_id))
    driver = Driver.query.get_or_404(driver_id)
    driver.approval_status = "approved"
    driver.approved_by_user_id = current_user.id
    driver.approved_at = datetime.utcnow()
    driver.is_active = True
    db.session.commit()
    flash(f"تم قبول الكابتن {driver.name}. كلمة السر الافتراضية شغالة.", "success")
    return redirect(url_for("drivers.show", driver_id=driver_id))


@drivers_bp.route("/<int:driver_id>/reject", methods=["POST"])
@login_required
def reject(driver_id: int):
    if not current_user.is_admin:
        flash("للأدمن بس.", "error")
        return redirect(url_for("drivers.show", driver_id=driver_id))
    driver = Driver.query.get_or_404(driver_id)
    driver.approval_status = "rejected"
    driver.is_active = False
    db.session.commit()
    flash("تم رفض طلب الكابتن.", "success")
    return redirect(url_for("drivers.show", driver_id=driver_id))


@drivers_bp.route("/<int:driver_id>/reset-password", methods=["POST"])
@login_required
def reset_password(driver_id: int):
    if not current_user.is_admin:
        flash("للأدمن بس.", "error")
        return redirect(url_for("drivers.show", driver_id=driver_id))
    driver = Driver.query.get_or_404(driver_id)
    from flask import current_app
    driver.set_password(current_app.config["DEFAULT_CAPTAIN_PASSWORD"])
    driver.must_change_password = True
    db.session.commit()
    flash("تم رجوع كلمة السر للافتراضية.", "success")
    return redirect(url_for("drivers.show", driver_id=driver_id))
