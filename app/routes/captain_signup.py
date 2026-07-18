"""Public captain self-registration.

Unauthenticated. Captain submits phone + name + car details → we create a
Driver row with approval_status='pending' and the default password from env.
Admin reviews in /drivers?filter=pending and approves.
"""
from __future__ import annotations

from flask import (
    Blueprint, current_app, flash, redirect, render_template, request, url_for,
)

import phonenumbers

from app import db
from app.models.driver import Driver


captain_signup_bp = Blueprint("captain_signup", __name__)


def _normalize_wa_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        num = phonenumbers.parse(raw, "EG")
        return f"{num.country_code}{num.national_number}"
    except phonenumbers.NumberParseException:
        return raw.lstrip("+").replace(" ", "")


@captain_signup_bp.route("/captain/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        wa_id = _normalize_wa_id(request.form.get("wa_id", ""))
        name = (request.form.get("name") or "").strip()
        car_model = (request.form.get("car_model") or "").strip()
        car_plate = (request.form.get("car_plate") or "").strip()
        car_color = (request.form.get("car_color") or "").strip()

        if not (wa_id and name and car_model and car_plate):
            flash("برجاء ملء جميع الحقول المطلوبة.", "error")
            return render_template("captain_signup/register.html", form=request.form)

        if Driver.query.filter_by(wa_id=wa_id).first():
            flash("رقم الهاتف مسجل بالفعل. لو نسيت كلمة السر تواصل مع الإدارة.", "error")
            return render_template("captain_signup/register.html", form=request.form)

        driver = Driver(
            wa_id=wa_id,
            name=name,
            car_model=car_model,
            car_plate=car_plate,
            car_color=car_color or None,
            category="economy",
            approval_status="pending",
            signup_source="public",
            is_active=False,      # can't work until approved
            must_change_password=True,
        )
        driver.set_password(current_app.config["DEFAULT_CAPTAIN_PASSWORD"])
        db.session.add(driver)
        db.session.commit()

        return render_template("captain_signup/success.html", driver=driver)

    return render_template("captain_signup/register.html", form={})
