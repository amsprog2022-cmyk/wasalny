import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


def _normalize_db_url(url: str) -> str:
    # Railway/Render sometimes give "postgres://" but SQLAlchemy needs "postgresql://"
    if url and url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", SECRET_KEY)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=7)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)

    SQLALCHEMY_DATABASE_URI = _normalize_db_url(
        os.getenv("DATABASE_URL", "sqlite:///wassalny.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Redis (real-time hot path). Empty → in-memory fakeredis for local dev.
    REDIS_URL = os.getenv("REDIS_URL", "")

    # Driver availability
    # 5 minutes so a captain whose phone briefly backgrounds the app is still
    # counted as available and reachable via FCM push. Was 60s which was too
    # aggressive — iOS backgrounds apps within ~30s of inactivity.
    DRIVER_HEARTBEAT_TIMEOUT_SECONDS = int(
        os.getenv("DRIVER_HEARTBEAT_TIMEOUT_SECONDS", "300")
    )

    # GPS tracking (phase 1: captain streams position, matching still zone-based).
    # last_position_at older than this counts as stale in the debug view.
    DRIVER_POSITION_TTL_SECONDS = int(os.getenv("DRIVER_POSITION_TTL_SECONDS", "600"))
    # Nearest-N radius used by matching (phase 2 onwards).
    GEO_SEARCH_RADIUS_KM = float(os.getenv("GEO_SEARCH_RADIUS_KM", "5"))

    # Map tiles — MapTiler free tier (100k loads/month) is enough for both
    # the admin live map and the captain/customer app maps at current scale.
    # Same key baked into the captain app; can rotate via env var without a
    # code change if it ever leaks.
    MAPTILER_KEY = os.getenv("MAPTILER_KEY", "parhG5kKaSBCSzASEX3O")

    # Distance-based pricing (Phase 2 customer app). Straight-line Haversine
    # for now — captain can override on arrival for edge cases.
    PRICING_BASE_EGP   = float(os.getenv("PRICING_BASE_EGP",   "10"))
    PRICING_PER_KM_EGP = float(os.getenv("PRICING_PER_KM_EGP", "3"))
    PRICING_MIN_EGP    = float(os.getenv("PRICING_MIN_EGP",    "15"))
    PRICING_MAX_EGP    = float(os.getenv("PRICING_MAX_EGP",    "500"))

    # GPS matching (Phase 2). How many captains to offer to at once + the
    # cascading radii we escalate through if nobody's close.
    GEO_MATCH_TOP_N    = int(os.getenv("GEO_MATCH_TOP_N",       "3"))
    GEO_MATCH_RADII_KM = os.getenv("GEO_MATCH_RADII_KM",        "3,6,10")

    # Business rules (Decisions #10, #14, config §Appendix B)
    WASSALNY_COMMISSION_RATE = os.getenv("WASSALNY_COMMISSION_RATE", "0.15")
    # Fallback price when a zone pair isn't in the pricing matrix. With ~350
    # hyperlocal Benha regions we can't maintain a full N×N matrix; captains
    # override on-the-fly when needed.
    DEFAULT_ZONE_PRICE_EGP = os.getenv("DEFAULT_ZONE_PRICE_EGP", "25")
    NO_SHOW_FEE_EGP = os.getenv("NO_SHOW_FEE_EGP", "10")
    NO_SHOW_ENABLE_AFTER_MINUTES = int(os.getenv("NO_SHOW_ENABLE_AFTER_MINUTES", "5"))
    BROADCAST_ACCEPT_WINDOW_SECONDS = int(
        os.getenv("BROADCAST_ACCEPT_WINDOW_SECONDS", "30")
    )
    MATCHING_MAX_ROUNDS = int(os.getenv("MATCHING_MAX_ROUNDS", "3"))
    CUSTOMER_RATE_LIMIT_PER_10MIN = int(os.getenv("CUSTOMER_RATE_LIMIT_PER_10MIN", "3"))

    # Firebase Cloud Messaging (push notifications)
    # Full service account JSON — either raw JSON or base64-encoded (recommended for Railway).
    FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
    FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "wasalny-de5bf")

    # Sentry error tracking. Silent no-op when unset (local dev).
    SENTRY_DSN = os.getenv("SENTRY_DSN", "")

    # Captain end-of-trip extra charge (waiting time / detour / etc.).
    # Rides are cash, so this simply raises what the customer hands over.
    CAPTAIN_EXTRA_MIN_EGP = float(os.getenv("CAPTAIN_EXTRA_MIN_EGP", "5"))
    CAPTAIN_EXTRA_MAX_EGP = float(os.getenv("CAPTAIN_EXTRA_MAX_EGP", "100"))

    # Unpaid commission a captain may carry before he is blocked from going
    # online. Rides settle in cash, so this debt is the only lever we have to
    # make him come in and hand the money over.
    CAPTAIN_MAX_DEBT_EGP = float(os.getenv("CAPTAIN_MAX_DEBT_EGP", "300"))

    # AI parser — gemini-2.0-flash was retired mid-2026, so we default to
    # the -latest alias which always follows Google's current fast model.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    # 8s covers cold-start + the richer multi-intent prompt. Was 3s pre-upgrade.
    GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "8"))
    # Per-phone Gemini call cap so one abuser can't burn the whole quota.
    # 100/hour = ~1.7/min average — well over any legitimate human pattern.
    GEMINI_RATE_LIMIT_PER_HOUR = int(
        os.getenv("GEMINI_RATE_LIMIT_PER_HOUR", "100")
    )
    AI_SESSION_TTL_MINUTES = int(os.getenv("AI_SESSION_TTL_MINUTES", "30"))

    # WhatsApp Cloud API
    WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")
    WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")
    # Escape hatch for debugging bad secrets — do not use in production.
    WHATSAPP_SKIP_SIGNATURE_CHECK = os.getenv("WHATSAPP_SKIP_SIGNATURE_CHECK", "")

    # Reverse OTP. The customer sends a prefilled code FROM their WhatsApp TO
    # this number, so it must be the dialable business line in international
    # format (e.g. 201012345678) — NOT WHATSAPP_PHONE_NUMBER_ID, which is
    # Meta's internal id and cannot be used in a wa.me link.
    WHATSAPP_BUSINESS_NUMBER = os.getenv("WHATSAPP_BUSINESS_NUMBER", "")
    # Off by default so already-installed app builds keep registering after
    # this deploys. Flip to "1" on Railway once the new builds are adopted.
    REQUIRE_PHONE_VERIFICATION = os.getenv("REQUIRE_PHONE_VERIFICATION", "") == "1"
    VERIFY_CODE_TTL_SECONDS = int(os.getenv("VERIFY_CODE_TTL_SECONDS", "600"))
    VERIFY_TICKET_TTL_SECONDS = int(os.getenv("VERIFY_TICKET_TTL_SECONDS", "900"))

    # Initial admin bootstrap
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@wassalny.com")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")

    # Default password assigned to captains at public self-signup.
    # Every new captain gets this. Captain app forces a change on first login.
    DEFAULT_CAPTAIN_PASSWORD = os.getenv("DEFAULT_CAPTAIN_PASSWORD", "wassalny-2026")
