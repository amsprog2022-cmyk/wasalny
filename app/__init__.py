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

    # Sentry must init before anything that might throw, so early exceptions
    # get captured too. No-op when SENTRY_DSN isn't set.
    _init_sentry(app)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to continue."
    # message_queue lets emits from one gunicorn worker reach clients connected
    # to a different worker — required now that we run with -w 2.
    socketio.init_app(app, message_queue=app.config.get("REDIS_URL") or None)
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
    from app.routes.intercity import intercity_bp
    from app.routes.wallets import wallets_bp
    from app.routes.reports import reports_bp
    from app.routes.marketing import marketing_bp
    from app.routes.coupons import coupons_bp
    from app.routes.office_numbers import office_numbers_bp
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
    app.register_blueprint(intercity_bp)
    app.register_blueprint(wallets_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(marketing_bp)
    app.register_blueprint(coupons_bp)
    app.register_blueprint(office_numbers_bp)
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

    @app.template_filter("cairo")
    def _cairo(value, fmt="%H:%M · %d %b"):
        """Render a naive-UTC column in Cairo wall-clock."""
        if value is None:
            return "—"
        from app.services.localtime import utc_to_cairo
        return utc_to_cairo(value).strftime(fmt)

    # Import all models so db.create_all() sees them
    from app.models import gemini_call as _gc  # noqa: F401
    from app.models import trip_chat as _tc    # noqa: F401
    from app.models import wallet as _wl       # noqa: F401
    from app.models import intercity_request as _ic  # noqa: F401
    from app.models import coupon as _cp        # noqa: F401
    from app.models import office as _of        # noqa: F401

    with app.app_context():
        db.create_all()
        _apply_lightweight_migrations(app)
        _bootstrap_admin(app)
        _bootstrap_zones(app)
        _bootstrap_benha_regions(app)
        _bootstrap_stickers(app)
        _bootstrap_office_numbers(app)
        _init_firebase_admin(app)

    # Background sweeper — catches rides stuck in `broadcasting` because their
    # matching greenlet died (worker recycled mid-round, deploy, crash). Runs
    # in every worker but uses a Redis lock so only one worker sweeps per tick.
    from app.services.matching import start_sweeper
    start_sweeper(app)

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


def _bootstrap_office_numbers(app):
    """Seed the known Wassalny office WhatsApp numbers, first boot only.

    Guarded on the table being empty rather than on each number being absent,
    otherwise a number an admin deliberately deleted would come back on the
    next deploy.
    """
    from app.models.office import OfficeNumber

    if OfficeNumber.query.first() is not None:
        return
    for wa_id, label in (("201050084115", "مكتب وصلني"),
                         ("201012818977", "رقم تجريبي")):
        db.session.add(OfficeNumber(wa_id=wa_id, label=label, is_active=True))
    db.session.commit()
    print("[bootstrap] office numbers seeded: 201050084115, 201012818977")


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

        # Phase 2 ride GPS columns. Nullable so WhatsApp / legacy zone-only
        # bookings continue to work with these NULL.
        ride_gps_columns = [
            ("pickup_lat",  "DOUBLE PRECISION"),
            ("pickup_lng",  "DOUBLE PRECISION"),
            ("dropoff_lat", "DOUBLE PRECISION"),
            ("dropoff_lng", "DOUBLE PRECISION"),
        ]
        for col, coltype in ride_gps_columns:
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE rides ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text("PRAGMA table_info(rides)")
                ).fetchall()}
                if col not in existing:
                    sqlite_type = "REAL" if coltype == "DOUBLE PRECISION" else coltype
                    conn.execute(text(
                        f"ALTER TABLE rides ADD COLUMN {col} {sqlite_type}"
                    ))

        # Phase 2.5 reverse-geocoded address strings for the trip UI.
        # Nullable — populated on ride create for GPS bookings only.
        ride_address_columns = [
            ("pickup_address",  "TEXT"),
            ("dropoff_address", "TEXT"),
        ]
        for col, coltype in ride_address_columns:
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE rides ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text("PRAGMA table_info(rides)")
                ).fetchall()}
                if col not in existing:
                    conn.execute(text(
                        f"ALTER TABLE rides ADD COLUMN {col} {coltype}"
                    ))

        # Phase 4.5 — service_kind on drivers + rides. Existing rows default
        # to 'private' so the current auto-broadcast flow is unchanged.
        for table in ("drivers", "rides"):
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS service_kind "
                    "VARCHAR(20) DEFAULT 'private' NOT NULL"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).fetchall()}
                if "service_kind" not in existing:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN service_kind "
                        "VARCHAR(20) DEFAULT 'private' NOT NULL"
                    ))

        # Phase 3.1 — WhatsApp GPS booking session state. We stash the free
        # text + geocoded coords across turns so a customer can send pickup
        # in one message and dropoff in the next.
        ai_session_gps_columns = [
            ("partial_pickup_text",   "VARCHAR(255)"),
            ("partial_pickup_lat",    "DOUBLE PRECISION"),
            ("partial_pickup_lng",    "DOUBLE PRECISION"),
            ("partial_dropoff_text",  "VARCHAR(255)"),
            ("partial_dropoff_lat",   "DOUBLE PRECISION"),
            ("partial_dropoff_lng",   "DOUBLE PRECISION"),
        ]
        for col, coltype in ai_session_gps_columns:
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text("PRAGMA table_info(ai_sessions)")
                ).fetchall()}
                if col not in existing:
                    sqlite_type = "REAL" if coltype == "DOUBLE PRECISION" else coltype
                    conn.execute(text(
                        f"ALTER TABLE ai_sessions ADD COLUMN {col} {sqlite_type}"
                    ))

        # WhatsApp service menu — which of the four kinds the customer picked
        # for this session, and the throttle timestamp for the app advert.
        wa_menu_columns = [
            ("ai_sessions", "service_kind",      "VARCHAR(20)"),
            ("customers",   "app_promo_sent_at", "TIMESTAMP"),
        ]
        for table, col, coltype in wa_menu_columns:
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).fetchall()}
                if col not in existing:
                    sqlite_type = "DATETIME" if coltype == "TIMESTAMP" else coltype
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {sqlite_type}"
                    ))
        # captain_extra_egp on existing rides rows — defaults to 0 so old
        # trips just show no extra charge on the receipt.
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE rides ADD COLUMN IF NOT EXISTS captain_extra_egp "
                "NUMERIC(8,2) DEFAULT 0 NOT NULL"
            ))
        elif dialect == "sqlite":
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(rides)")
            ).fetchall()}
            if "captain_extra_egp" not in existing:
                conn.execute(text(
                    "ALTER TABLE rides ADD COLUMN captain_extra_egp "
                    "NUMERIC(8,2) DEFAULT 0 NOT NULL"
                ))

        # Reverse-OTP phone verification. Nullable everywhere — existing
        # accounts predate verification and must not be locked out.
        for table in ("customers", "drivers"):
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                    "phone_verified_at TIMESTAMP"
                ))
            elif dialect == "sqlite":
                existing = {row[1] for row in conn.execute(
                    text(f"PRAGMA table_info({table})")
                ).fetchall()}
                if "phone_verified_at" not in existing:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN phone_verified_at DATETIME"
                    ))

        # How much wallet credit came off the cash the customer hands over.
        # 0 on every historical ride, which is correct — the wallet was
        # never spendable before.
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE rides ADD COLUMN IF NOT EXISTS wallet_discount_egp "
                "NUMERIC(8,2) DEFAULT 0 NOT NULL"
            ))
        elif dialect == "sqlite":
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(rides)")
            ).fetchall()}
            if "wallet_discount_egp" not in existing:
                conn.execute(text(
                    "ALTER TABLE rides ADD COLUMN wallet_discount_egp "
                    "NUMERIC(8,2) DEFAULT 0 NOT NULL"
                ))

        # Change the captain parked in the customer's wallet instead of
        # handing back coins. 0 on every historical ride.
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE rides ADD COLUMN IF NOT EXISTS change_credit_egp "
                "NUMERIC(8,2) DEFAULT 0 NOT NULL"
            ))
        elif dialect == "sqlite":
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(rides)")
            ).fetchall()}
            if "change_credit_egp" not in existing:
                conn.execute(text(
                    "ALTER TABLE rides ADD COLUMN change_credit_egp "
                    "NUMERIC(8,2) DEFAULT 0 NOT NULL"
                ))

        # Promo-code discount. No FK in the ALTER — SQLite can't add one to an
        # existing table, and the model-level relationship is enough.
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE rides ADD COLUMN IF NOT EXISTS coupon_id INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE rides ADD COLUMN IF NOT EXISTS coupon_discount_egp "
                "NUMERIC(8,2) DEFAULT 0 NOT NULL"
            ))
        elif dialect == "sqlite":
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(rides)")
            ).fetchall()}
            if "coupon_id" not in existing:
                conn.execute(text("ALTER TABLE rides ADD COLUMN coupon_id INTEGER"))
            if "coupon_discount_egp" not in existing:
                conn.execute(text(
                    "ALTER TABLE rides ADD COLUMN coupon_discount_egp "
                    "NUMERIC(8,2) DEFAULT 0 NOT NULL"
                ))

        # Which office WhatsApp number dispatched this ride, so the accept
        # confirmation knows where to reply. NULL on app + WhatsApp rides.
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE rides ADD COLUMN IF NOT EXISTS office_wa_id VARCHAR(20)"
            ))
        elif dialect == "sqlite":
            existing = {row[1] for row in conn.execute(
                text("PRAGMA table_info(rides)")
            ).fetchall()}
            if "office_wa_id" not in existing:
                conn.execute(text(
                    "ALTER TABLE rides ADD COLUMN office_wa_id VARCHAR(20)"
                ))
    # The wallet tables (customer + driver) are new tables — db.create_all()
    # handles them automatically; no ALTER needed. Print here so we can see it
    # ran on every boot.
    print("[migrate] FCM + password_hash + deleted_at + nullable to_zone_id + clarify_count + driver_position + ride_gps + ride_addresses + ai_session_gps + service_kind + wa_menu + wallet + captain_extra + phone_verified_at + driver_wallet + change_credit + coupon + office_wa_id ensured")


def _init_sentry(app):
    """Wire up Sentry error tracking. Silent no-op when SENTRY_DSN is unset
    (local dev) or when the SDK isn't installed."""
    import os as _os
    dsn = (app.config.get("SENTRY_DSN") or "").strip()
    if not dsn:
        print("[sentry] SENTRY_DSN not set — error tracking disabled")
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        print("[sentry] sentry-sdk not installed — error tracking disabled")
        return
    try:
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            # Errors only. Perf traces would eat the free-tier quota fast.
            traces_sample_rate=0.0,
            environment=_os.getenv("RAILWAY_ENVIRONMENT", "production"),
            release=(_os.getenv("RAILWAY_GIT_COMMIT_SHA") or "unknown")[:12],
        )
        print(f"[sentry] initialized for env={_os.getenv('RAILWAY_ENVIRONMENT', 'production')}")
    except Exception as e:  # noqa: BLE001
        print(f"[sentry] init failed: {e}")


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
