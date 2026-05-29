"""Application settings loaded from environment variables.

Region validation lives in app.regions — re-exported here so existing imports
of `from app.config import validate_region` keep working.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.regions import validate_region  # noqa: F401 — re-export for backwards compat


class Settings(BaseSettings):
    """Runtime configuration.

    All values come from environment variables (or .env in local dev). The
    compose file is the source of truth for what gets set in each
    environment.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Environment ---------------------------------------------------------
    whumpf_env: str = "dev"

    # --- Database ------------------------------------------------------------
    database_url: str
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_s: int = 1800

    # --- Auth ----------------------------------------------------------------
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_min: int = 60

    # --- S3 / MinIO ----------------------------------------------------------
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket_dem_cogs: str = "dem-cogs"
    s3_bucket_dem_raw: str = "dem-raw"
    s3_bucket_user_uploads: str = "user-uploads"

    # --- Upstream tile servers ----------------------------------------------
    titiler_url: str = "http://titiler:8000"
    martin_url: str = "http://martin:3000"

    # --- Strava OAuth --------------------------------------------------------
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_redirect_uri: str = "http://localhost:8000/auth/strava/callback"
    strava_success_url: str = "http://localhost:5173"
    # Fernet key (base64url-encoded 32 bytes) used to encrypt Strava OAuth
    # tokens at rest. Empty string disables encryption (legacy plaintext path).
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    strava_token_key: str = ""

    # --- CORS ---------------------------------------------------------------
    cors_allow_origins: str = "http://localhost:5173"

    # --- Email / account lifecycle ------------------------------------------
    # Base URL of the frontend; used to build verify/reset links.
    app_base_url: str = "http://localhost:5173"
    # "console" (logs URL to stdout, useful in dev/staging) or "resend".
    mail_provider: str = "console"
    mail_from: str = "whumpf <no-reply@whumpf.local>"
    # Resend.com API key (https://resend.com), only required when
    # mail_provider == "resend".
    resend_api_key: str = ""
    # Token TTLs (seconds).
    email_verification_ttl_s: int = 24 * 3600
    password_reset_ttl_s: int = 3600

    # --- Rate limiting -------------------------------------------------------
    # Redis URL for cross-worker rate limit state. Defaults to in-process
    # memory (fine for single-worker dev). Set to redis://localhost:6379 in prod.
    redis_url: str = ""

    # --- Admin / status page ------------------------------------------------
    # CSV of email addresses that get admin powers automatically. The
    # users.is_admin column is the other path; either grants admin.
    admin_emails: str = ""

    # --- Registration -------------------------------------------------------
    # Set to True to auto-verify email on registration (useful before a real
    # mail provider is wired up). Flip back to False once MAIL_PROVIDER=resend.
    skip_email_verification: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
