"""Admin CRUD for the WhatsApp numbers that dispatch trips as an office."""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.office import OfficeNumber
from app.models.ride import Ride
from app.services import audit
from app.services import office_dispatch


office_numbers_bp = Blueprint(
    "office_numbers", __name__, url_prefix="/office-numbers"
)


@office_numbers_bp.route("/")
@login_required
def index():
    rows = OfficeNumber.query.order_by(OfficeNumber.created_at.desc()).all()
    counts = dict(
        db.session.query(Ride.office_wa_id, db.func.count(Ride.id))
        .filter(Ride.office_wa_id.isnot(None))
        .group_by(Ride.office_wa_id)
        .all()
    )
    return render_template(
        "office_numbers/index.html", numbers=rows, counts=counts,
    )


@office_numbers_bp.route("/new", methods=["POST"])
@login_required
def new():
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("office_numbers.index"))

    # Normalised through the same helper the inbound parser uses, so a number
    # typed as "+20 105 008 4115" can never fail to match the "201050084115"
    # that arrives on the webhook.
    wa_id = office_dispatch.normalize_wa_id(request.form.get("wa_id"))
    if not wa_id:
        flash("الرقم مش مظبوط. اكتبه بصيغة 01xxxxxxxxx.", "error")
        return redirect(url_for("office_numbers.index"))
    if OfficeNumber.query.filter_by(wa_id=wa_id).first():
        flash("الرقم ده مضاف قبل كده.", "error")
        return redirect(url_for("office_numbers.index"))

    row = OfficeNumber(
        wa_id=wa_id,
        label=(request.form.get("label") or "").strip() or None,
        is_active=True,
        created_by_user_id=current_user.id,
    )
    db.session.add(row)
    db.session.flush()
    audit.record(
        "office_number.create", target_kind="office_number", target_id=row.id,
        after=row.to_dict(),
    )
    db.session.commit()
    office_dispatch.invalidate_cache(wa_id)
    flash(f"الرقم {wa_id} اتضاف.", "success")
    return redirect(url_for("office_numbers.index"))


@office_numbers_bp.route("/<int:number_id>/toggle", methods=["POST"])
@login_required
def toggle(number_id: int):
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("office_numbers.index"))
    row = OfficeNumber.query.get_or_404(number_id)
    row.is_active = not row.is_active
    audit.record(
        "office_number.toggle", target_kind="office_number", target_id=row.id,
        after={"is_active": row.is_active},
    )
    db.session.commit()
    office_dispatch.invalidate_cache(row.wa_id)
    flash(f"الرقم {row.wa_id} " + ("اشتغل." if row.is_active else "اتوقف."), "success")
    return redirect(url_for("office_numbers.index"))


@office_numbers_bp.route("/<int:number_id>/delete", methods=["POST"])
@login_required
def delete(number_id: int):
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("office_numbers.index"))
    row = OfficeNumber.query.get_or_404(number_id)
    audit.record(
        "office_number.delete", target_kind="office_number", target_id=row.id,
        before=row.to_dict(),
    )
    wa_id = row.wa_id
    db.session.delete(row)
    db.session.commit()
    office_dispatch.invalidate_cache(wa_id)
    flash("الرقم اتمسح.", "success")
    return redirect(url_for("office_numbers.index"))
