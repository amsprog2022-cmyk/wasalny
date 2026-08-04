"""Admin CRUD for percent-discount promo codes."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, current_user

from app import db
from app.models.coupon import Coupon
from app.services import audit
from app.services import coupons as coupons_svc
from app.services import localtime


coupons_bp = Blueprint("coupons", __name__, url_prefix="/coupons")


def _parse_local(value: str | None) -> datetime | None:
    """Read a browser `datetime-local` value as Cairo wall-clock, store UTC.
    Same conversion the announcements page needs — reading these as UTC put
    every window 2-3 hours out."""
    if not value:
        return None
    try:
        return localtime.cairo_to_utc(datetime.strptime(value, "%Y-%m-%dT%H:%M"))
    except ValueError:
        return None


def _read_form(form) -> tuple[dict | None, str | None]:
    """Shared parse + validate for the create and edit forms.

    Returns (fields, error_ar). `code` is omitted on edit — changing a live
    code out from under customers who already have it would silently break
    every screenshot they've been sent.
    """
    try:
        pct = int(form.get("discount_pct") or 0)
    except ValueError:
        return None, "نسبة الخصم لازم تكون رقم."
    if not 1 <= pct <= 100:
        return None, "نسبة الخصم لازم تكون بين ١ و ١٠٠."

    try:
        max_uses = int(form.get("max_uses_per_customer") or 1)
    except ValueError:
        return None, "عدد مرات الاستخدام لازم يكون رقم."
    if max_uses < 1:
        return None, "عدد مرات الاستخدام لازم يكون ١ على الأقل."

    try:
        min_fare = Decimal(str(form.get("min_fare_egp") or 0))
    except (InvalidOperation, ValueError):
        return None, "أقل سعر لازم يكون رقم."
    if min_fare < 0:
        return None, "أقل سعر مينفعش يكون بالسالب."

    # An empty field means "no limit"; a field the browser accepted but we
    # can't parse must not silently become "no limit" — that would publish a
    # code nobody meant to leave running.
    raw_starts = (form.get("starts_at") or "").strip()
    starts_at = _parse_local(raw_starts)
    if raw_starts and starts_at is None:
        return None, "وقت البداية مش مفهوم."
    raw_ends = (form.get("ends_at") or "").strip()
    ends_at = _parse_local(raw_ends)
    if raw_ends and ends_at is None:
        return None, "وقت النهاية مش مفهوم."
    if starts_at and ends_at and ends_at <= starts_at:
        return None, "وقت النهاية لازم يكون بعد وقت البداية."

    return {
        "discount_pct": pct,
        "description_ar": (form.get("description_ar") or "").strip() or None,
        "max_uses_per_customer": max_uses,
        "min_fare_egp": min_fare,
        "starts_at": starts_at,
        "ends_at": ends_at,
    }, None


@coupons_bp.route("/")
@login_required
def index():
    rows = Coupon.query.order_by(Coupon.created_at.desc()).limit(100).all()
    usage = {c.id: coupons_svc.total_uses(c.id) for c in rows}
    return render_template("coupons/index.html", coupons=rows, usage=usage)


@coupons_bp.route("/new", methods=["POST"])
@login_required
def new():
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("coupons.index"))

    code = coupons_svc.normalize(request.form.get("code"))
    if not code:
        flash("لازم تكتب الكود.", "error")
        return redirect(url_for("coupons.index"))
    if not code.isalnum():
        flash("الكود يبقى حروف وأرقام إنجليزي بس، من غير مسافات.", "error")
        return redirect(url_for("coupons.index"))
    if Coupon.query.filter(db.func.upper(Coupon.code) == code).first():
        flash("الكود ده موجود قبل كده.", "error")
        return redirect(url_for("coupons.index"))

    fields, err = _read_form(request.form)
    if err:
        flash(err, "error")
        return redirect(url_for("coupons.index"))

    c = Coupon(code=code, created_by_user_id=current_user.id, **fields)
    db.session.add(c)
    db.session.flush()
    audit.record(
        "coupon.create", target_kind="coupon", target_id=c.id,
        after={"code": c.code, "discount_pct": c.discount_pct},
    )
    db.session.commit()
    flash(f"الكود {c.code} اتعمل.", "success")
    return redirect(url_for("coupons.index"))


@coupons_bp.route("/<int:coupon_id>/edit", methods=["GET", "POST"])
@login_required
def edit(coupon_id: int):
    c = Coupon.query.get_or_404(coupon_id)
    if request.method == "GET":
        return render_template("coupons/edit.html", coupon=c)

    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("coupons.index"))

    fields, err = _read_form(request.form)
    if err:
        flash(err, "error")
        return redirect(url_for("coupons.edit", coupon_id=c.id))

    before = c.to_dict()
    for key, value in fields.items():
        setattr(c, key, value)
    audit.record(
        "coupon.update", target_kind="coupon", target_id=c.id,
        before=before, after=c.to_dict(),
    )
    db.session.commit()
    flash(f"الكود {c.code} اتعدل.", "success")
    return redirect(url_for("coupons.index"))


@coupons_bp.route("/<int:coupon_id>/toggle", methods=["POST"])
@login_required
def toggle(coupon_id: int):
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("coupons.index"))
    c = Coupon.query.get_or_404(coupon_id)
    c.is_active = not c.is_active
    audit.record(
        "coupon.toggle", target_kind="coupon", target_id=c.id,
        after={"is_active": c.is_active},
    )
    db.session.commit()
    flash(f"الكود {c.code} " + ("اشتغل." if c.is_active else "اتوقف."), "success")
    return redirect(url_for("coupons.index"))


@coupons_bp.route("/<int:coupon_id>/delete", methods=["POST"])
@login_required
def delete(coupon_id: int):
    """Only ever deletes a code nobody used.

    Once a ride carries the id, deleting would orphan a discount that is
    already part of a settled trip — switching it off keeps the money
    history readable.
    """
    if not current_user.is_admin:
        flash("للمدير فقط.", "error")
        return redirect(url_for("coupons.index"))
    c = Coupon.query.get_or_404(coupon_id)
    if coupons_svc.total_uses(c.id) > 0:
        flash("الكود ده اتستخدم قبل كده — اقفله بدل ما تمسحه.", "error")
        return redirect(url_for("coupons.index"))
    audit.record("coupon.delete", target_kind="coupon", target_id=c.id, before=c.to_dict())
    db.session.delete(c)
    db.session.commit()
    flash("الكود اتمسح.", "success")
    return redirect(url_for("coupons.index"))
