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
    from app.routes.legal import legal_bp
    from app.routes.complaints import complaints_bp
    from app.routes.sos import sos_bp
    from app.routes.reports import reports_bp
    from app.routes.marketing import marketing_bp
    from app.routes.audit import audit_bp
    from app.routes.live_map import live_map_bp
    from app.api.v1 import api_v1_bp
    from app.api.rides_api import rides_api_bp
    from app.api.debug_api import debug_api_bp

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
    app.register_blueprint(legal_bp)
    app.register_blueprint(complaints_bp)
    app.register_blueprint(sos_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(marketing_bp)
    app.register_blueprint(audit_bp)
    app.register_blueprint(live_map_bp)
    app.register_blueprint(api_v1_bp)
    app.register_blueprint(rides_api_bp)
    app.register_blueprint(debug_api_bp)

    from app.sockets import inbox_socket  # noqa: F401
    from app.sockets import driver_socket  # noqa: F401
    from app.sockets import customer_socket  # noqa: F401

    @app.route("/")
    def index():
        return redirect(url_for("dashboard.home"))

    # Import all models so db.create_all() sees them
    from app.models import gemini_call as _gc  # noqa: F401
    from app.models import trip_chat as _tc    # noqa: F401

    with app.app_context():
        db.create_all()
        _apply_lightweight_migrations(app)
        _bootstrap_admin(app)
        _bootstrap_zones(app)
        _bootstrap_benha_regions(app)
        _bootstrap_stickers(app)
        _init_firebase_admin(app)

    return app


def _bootstrap_benha_regions(app):
    """Ensure every region from ops/benha_regions.py exists as a Zone.

    Idempotent — only inserts zones whose name_ar isn't in the table yet.
    Runs after _bootstrap_zones so the initial test zones stay untouched.
    Uses the Arabic name as slug (utf-8 is fine for Postgres/SQLite) and
    also as name_en since there's no reliable English transliteration for
    ~350 hyperlocal spots.
    """
    from app.models.zone import Zone
    try:
        from ops.benha_regions import REGIONS
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap] regions import failed: {e}")
        return

    existing_names = {n for (n,) in db.session.query(Zone.name_ar).all()}
    inserted = 0
    for name_ar in REGIONS:
        if name_ar in existing_names:
            continue
        # Slug must be unique + short-ish; use a compact hash of the Arabic
        # name so we don't have to transliterate. Prefix with 'r-' to avoid
        # colliding with the seeded slugs ('ramla', 'downtown', ...).
        import hashlib as _h
        slug = "r-" + _h.md5(name_ar.encode("utf-8")).hexdigest()[:10]
        db.session.add(Zone(slug=slug, name_ar=name_ar, name_en=name_ar, is_active=True))
        inserted += 1
    if inserted:
        db.session.commit()
    print(f"[bootstrap] Benha regions: +{inserted} inserted, {Zone.query.count()} total")


def _bootstrap_stickers(app):
    """Ensure default sticker DB rows exist so the 'booked' ack fires
    on every deploy without needing to run seed_stickers manually."""
    try:
        from app.services.stickers import bootstrap_default_stickers
        bootstrap_default_stickers()
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap] stickers: {e}")


def _apply_lightweight_migrations(app):
    """Additive column migrations that must run every boot.

    `db.create_all()` won't add new columns to tables that already exist,
    so we run explicit `ADD COLUMN IF NOT EXISTS` for Postgres or a
    PRAGMA-guarded ALTER for SQLite. Safe to run repeatedly.
    """
    from sqlalchemy import text

    fcm_columns = [
        ("fcm_token", "TEXT"),
        ("fcm_platform", "VARCHAR(16)"),
        ("fcm_updated_at", "TIMESTAMP"),
    ]
    tables = ("customers", "drivers")

    dialect = db.engine.dialect.name
    with db.engine.begin() as conn:
        for table in tables:
            for col, coltype in fcm_columns:
                if dialect == "postgresql":
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"
                    ))
                elif dialect == "sqlite":
                    existing = {row[1] for row in conn.execute(
                        text(f"PRAGMA table_info({table})")
                    ).fetchall()}
                    if col not in existing:
                        conn.execute(text(
                            f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"
                        ))
        # Customer password: nullable so legacy accounts are prompted to set
        # a password on next login rather than being locked out.
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255)"
            ))
        elif dialect == "sqlite":
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(customers)")
            ).fetchall()}
            if "password_hash" not in existing:
                conn.execute(text(
                    "ALTER TABLE customers ADD COLUMN password_hash VARCHAR(255)"
                ))

        # Soft-delete columns for App/Play Store account-deletion requirement.
        for table in ("customers", "drivers"):
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).fetchall()}
                if "deleted_at" not in existing:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN deleted_at TIMESTAMP"
                    ))

        # WhatsApp rides start without a destination — captain sets it on
        # arrival. Drop the NOT NULL on Postgres. SQLite handles nullable
        # by default via ALTER anyway; we skip the strict check.
        if dialect == "postgresql":
            try:
                conn.execute(text(
                    "ALTER TABLE rides ALTER COLUMN to_zone_id DROP NOT NULL"
                ))
            except Exception as e:  # noqa: BLE001
                # Already nullable — Postgres raises when there's nothing to drop.
                print(f"[migrate] rides.to_zone_id nullable: {e}")

        # AI session clarify counter — bumped every time we ask the customer
        # a follow-up. After 2 unsuccessful rounds we escalate to admin.
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS clarify_count INTEGER DEFAULT 0 NOT NULL"
            ))
        elif dialect == "sqlite":
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(ai_sessions)")
            ).fetchall()}
            if "clarify_count" not in existing:
                conn.execute(text(
                    "ALTER TABLE ai_sessions ADD COLUMN clarify_count INTEGER DEFAULT 0 NOT NULL"
                ))

        # Live GPS columns on drivers. Redis is the hot path; these are the
        # durable snapshot updated on lifecycle events. Nullable so existing
        # rows don't fail the migration.
        position_columns = [
            ("latitude", "DOUBLE PRECISION"),
            ("longitude", "DOUBLE PRECISION"),
            ("last_position_at", "TIMESTAMP"),
        ]
        for col, coltype in position_columns:
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE drivers ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text("PRAGMA table_info(drivers)")
                ).fetchall()}
                if col not in existing:
                    # SQLite has no DOUBLE PRECISION — use REAL for both.
                    sqlite_type = "REAL" if coltype == "DOUBLE PRECISION" else coltype
                    conn.execute(text(
                        f"ALTER TABLE drivers ADD COLUMN {col} {sqlite_type}"
                    ))
    print("[migrate] FCM + password_hash + deleted_at + nullable to_zone_id + clarify_count + driver_position ensured")


def _init_firebase_admin(app):
    """Initialize firebase-admin from the FIREBASE_SERVICE_ACCOUNT_JSON env var.

    Accepts either raw JSON or a base64-encoded JSON blob. Silent-fails
    when the env var is missing so local dev boots without needing FCM.
    """
    raw = app.config.get("FIREBASE_SERVICE_ACCOUNT_JSON") or ""
    if not raw.strip():
        print("[firebase] service account env var not set — push disabled")
        return

    import json, base64
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        print("[firebase] firebase-admin not installed — push disabled")
        return

    if firebase_admin._apps:
        return

    try:
        data = raw.strip()
        if not data.startswith("{"):
            data = base64.b64decode(data).decode("utf-8")
        info = json.loads(data)
        cred = credentials.Certificate(info)
        firebase_admin.initialize_app(cred)
        print(f"[firebase] Admin SDK initialized for project {info.get('project_id')}")
    except Exception as e:
        print(f"[firebase] init failed: {e}")


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
