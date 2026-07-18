"""Admin UI for the zone-to-zone pricing matrix.

Displays a full N×N grid where each cell is an inline-editable price in EGP.
Saving posts the whole matrix at once to keep the UX simple.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.zone import Zone, ZonePricing


pricing_bp = Blueprint("pricing", __name__, url_prefix="/pricing")


def _pricing_map() -> dict[tuple[int, int], Decimal]:
    return {(p.from_zone_id, p.to_zone_id): p.price_egp for p in ZonePricing.query.all()}


@pricing_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    zones = Zone.query.order_by(Zone.id.asc()).all()

    if request.method == "POST":
        if not current_user.is_admin:
            flash("Admins only.", "error")
            return redirect(url_for("pricing.index"))

        existing = { (p.from_zone_id, p.to_zone_id): p for p in ZonePricing.query.all() }
        updated = 0
        for f in zones:
            for t in zones:
                key = f"p_{f.id}_{t.id}"
                raw = request.form.get(key, "").strip()
                if raw == "":
                    continue
                try:
                    val = Decimal(raw)
                except InvalidOperation:
                    continue
                row = existing.get((f.id, t.id))
                if row is None:
                    db.session.add(
                        ZonePricing(from_zone_id=f.id, to_zone_id=t.id, price_egp=val)
                    )
                    updated += 1
                elif row.price_egp != val:
                    row.price_egp = val
                    updated += 1
        db.session.commit()
        flash(f"Saved {updated} cell(s).", "success")
        return redirect(url_for("pricing.index"))

    prices = _pricing_map()
    return render_template("pricing/index.html", zones=zones, prices=prices)
