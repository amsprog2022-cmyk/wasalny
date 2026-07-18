"""Public legal pages — required by Meta to flip the app to Live mode.

Kept simple. If Ibrahim ever wants a real lawyer-drafted policy, drop the
new content into templates/legal/privacy.html and terms.html.
"""
from flask import Blueprint, render_template


legal_bp = Blueprint("legal", __name__)


@legal_bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html")


@legal_bp.route("/terms")
def terms():
    return render_template("legal/terms.html")


@legal_bp.route("/data-deletion")
def data_deletion():
    """Meta also asks for a data-deletion URL. Serve the same page."""
    return render_template("legal/data_deletion.html")
