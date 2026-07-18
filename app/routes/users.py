from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app import db
from app.models import User

users_bp = Blueprint("users", __name__, url_prefix="/users")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@users_bp.route("/")
@admin_required
def index():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("users/index.html", users=users)


@users_bp.route("/new", methods=["GET", "POST"])
@admin_required
def create():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "agent")

        if not email or not name or not password:
            flash("All fields are required.", "error")
            return render_template("users/form.html", user=None)

        if User.query.filter_by(email=email).first():
            flash("A user with that email already exists.", "error")
            return render_template("users/form.html", user=None)

        if role not in ("admin", "dispatcher", "agent"):
            role = "agent"

        user = User(email=email, name=name, role=role)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(f"User {name} created.", "success")
        return redirect(url_for("users.index"))

    return render_template("users/form.html", user=None)


@users_bp.route("/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_active(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("users.index"))
    user.is_active_user = not user.is_active_user
    db.session.commit()
    flash("Updated.", "success")
    return redirect(url_for("users.index"))
