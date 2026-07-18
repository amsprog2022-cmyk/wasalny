from flask import Flask, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_jwt_extended import JWTManager

from config import Config

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
socketio = SocketIO(cors_allowed_origins="*", async_mode="eventlet")
jwt = JWTManager()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to continue."
    socketio.init_app(app)
    jwt.init_app(app)

    from app.models.user import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    from app.routes.auth import auth_bp
    from app.routes.inbox import inbox_bp
    from app.routes.drivers import drivers_bp
    from app.routes.rides import rides_bp
    from app.routes.users import users_bp
    from app.routes.webhook import webhook_bp
    from app.routes.zones import zones_bp
    from app.routes.pricing import pricing_bp
    from app.routes.alerts import alerts_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.customers import customers_bp
    from app.routes.captain_signup import captain_signup_bp
    from app.routes.complaints import complaints_bp
    from app.routes.sos import sos_bp
    from app.routes.reports import reports_bp
    from app.routes.marketing import marketing_bp
    from app.routes.audit import audit_bp
    from app.api.v1 import api_v1_bp
    from app.api.rides_api import rides_api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(inbox_bp)
    app.register_blueprint(drivers_bp)
    app.register_blueprint(rides_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(zones_bp)
    app.register_blueprint(pricing_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(captain_signup_bp)
    app.register_blueprint(complaints_bp)
    app.register_blueprint(sos_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(marketing_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(api_v1_bp)
    app.register_blueprint(rides_api_bp)

    from app.sockets import inbox_socket  # noqa: F401
    from app.sockets import driver_socket  # noqa: F401
    from app.sockets import customer_socket  # noqa: F401

    @app.route("/")
    def index():
        return redirect(url_for("dashboard.home"))

    with app.app_context():
        db.create_all()
        _bootstrap_admin(app)
        _bootstrap_zones(app)

    return app


def _bootstrap_admin(app):
    from app.models.user import User

    if User.query.first() is None:
        admin = User(
            email=app.config["ADMIN_EMAIL"],
            name="Admin",
            role="admin",
        )
        admin.set_password(app.config["ADMIN_PASSWORD"])
        db.session.add(admin)
        db.session.commit()
        print(f"[bootstrap] Created initial admin: {admin.email}")


def _bootstrap_zones(app):
    """Seed the 5 test Benha zones + 25 price rows if the table is empty.

    Runs once on first boot in production so the app is immediately usable
    without manually running seed scripts.
    """
    from decimal import Decimal
    from app.models.zone import Zone, ZonePricing

    if Zone.query.count() > 0:
        return

    test_zones = [
        ("ramla",          "الرملة",       "El Ramla"),
        ("downtown",       "وسط البلد",     "Downtown"),
        ("university",     "جامعة بنها",    "Benha University"),
        ("sarayat",        "السرايات",     "El Sarayat"),
        ("damanhour_road", "طريق دمنهور",  "Damanhour Road"),
    ]
    zones = []
    for slug, name_ar, name_en in test_zones:
        z = Zone(slug=slug, name_ar=name_ar, name_en=name_en, is_active=True)
        db.session.add(z)
        zones.append(z)
    db.session.commit()

    same, cross = Decimal("20.00"), Decimal("25.00")
    for f in zones:
        for t in zones:
            db.session.add(
                ZonePricing(
                    from_zone_id=f.id, to_zone_id=t.id,
                    price_egp=(same if f.id == t.id else cross),
                )
            )
    db.session.commit()
    print(f"[bootstrap] Seeded {len(zones)} zones + {len(zones)**2} price rows")
