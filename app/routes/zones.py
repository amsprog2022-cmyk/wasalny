"""Admin CRUD for zones — the Benha neighborhoods used across the platform."""
from __future__ import annotations

import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.zone import Zone, ZonePricing
from app.services import availability as av


zones_bp = Blueprint("zones", __name__, url_prefix="/zones")


def _slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


@zones_bp.route("/")
@login_required
def index():
    zones = Zone.query.order_by(Zone.id.asc()).all()
    counts = av.zone_counts([z.id for z in zones])
    return render_template("zones/index.html", zones=zones, counts=counts)


@zones_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("zones.index"))

    if request.method == "POST":
        name_ar = request.form.get("name_ar", "").strip()
        name_en = request.form.get("name_en", "").strip()
        slug = _slugify(request.form.get("slug") or name_en)
        if not name_ar or not name_en or not slug:
            flash("All fields required.", "error")
            return redirect(url_for("zones.new"))
        if Zone.query.filter_by(slug=slug).first():
            flash(f"Slug '{slug}' already exists.", "error")
            return redirect(url_for("zones.new"))
        z = Zone(slug=slug, name_ar=name_ar, name_en=name_en)
        db.session.add(z)
        db.session.commit()
        # Auto-seed pricing rows with existing zones (both directions).
        _seed_pricing_for_new_zone(z)
        flash(f"Zone '{name_en}' added.", "success")
        return redirect(url_for("zones.index"))

    return render_template("zones/form.html", zone=None)


@zones_bp.route("/<int:zone_id>/toggle", methods=["POST"])
@login_required
def toggle(zone_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("zones.index"))
    z = Zone.query.get_or_404(zone_id)
    z.is_active = not z.is_active
    db.session.commit()
    flash(f"'{z.name_en}' is now {'active' if z.is_active else 'inactive'}.", "success")
    return redirect(url_for("zones.index"))


def _seed_pricing_for_new_zone(new_zone: Zone, default_same: float = 20.0, default_cross: float = 25.0) -> None:
    """When a zone is added, create pricing rows against every other active zone."""
    others = Zone.query.filter(Zone.id != new_zone.id).all()
    rows = [ZonePricing(from_zone_id=new_zone.id, to_zone_id=new_zone.id, price_egp=default_same)]
    for o in others:
        rows.append(ZonePricing(from_zone_id=new_zone.id, to_zone_id=o.id, price_egp=default_cross))
        rows.append(ZonePricing(from_zone_id=o.id, to_zone_id=new_zone.id, price_egp=default_cross))
    db.session.add_all(rows)
    db.session.commit()
