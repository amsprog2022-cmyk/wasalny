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
    DRIVER_HEARTBEAT_TIMEOUT_SECONDS = int(
        os.getenv("DRIVER_HEARTBEAT_TIMEOUT_SECONDS", "60")
    )

    # Business rules (Decisions #10, #14, config §Appendix B)
    WASSALNY_COMMISSION_RATE = os.getenv("WASSALNY_COMMISSION_RATE", "0.15")
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

    # AI parser — gemini-2.0-flash was retired mid-2026, so we default to
    # the -latest alias which always follows Google's current fast model.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "3"))
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

    # Initial admin bootstrap
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@wassalny.com")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")

    # Default password assigned to captains at public self-signup.
    # Every new captain gets this. Captain app forces a change on first login.
    DEFAULT_CAPTAIN_PASSWORD = os.getenv("DEFAULT_CAPTAIN_PASSWORD", "wassalny-2026")
