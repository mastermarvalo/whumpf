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
    # Refresh tokens: lifetime from issue (each rotation starts a new one).
    refresh_token_ttl_days: int = 60

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

    # --- NOAA NOHRSC snowfall (app/jobs/snowfall_refresh.py) ----------------
    # Sent with every download. NOAA states no policy; a contact helps them.
    nohrsc_user_agent: str = "whumpf/0.1 (+https://whumpf.co)"

    # --- NOAA weather layers and spot forecast (docs/runbook.md §9) ---------
    # Sent to NSIDC, MRMS, RTMA and api.weather.gov. api.weather.gov requires
    # a User-Agent and asks for contact details in it.
    weather_user_agent: str = "whumpf/0.1 (+https://whumpf.co)"

    # --- Mountain cameras (app/jobs/cameras_refresh.py, docs/runbook.md §10) -
    # Free keys. A source with no key is left out of the layer. Both are sent
    # as headers only, never in a URL, so they never reach a log.
    windy_webcams_api_key: str = ""
    nps_api_key: str = ""

    # --- Road conditions (app/jobs/roads_refresh.py, docs/runbook.md §11) ---
    # CDOT COtrip data feed key (manage-api.cotrip.org). Empty turns the layer
    # off. COtrip takes it only in the URL, so the jobs never log URLs and
    # app/log_redact.py strips it from every log line.
    cotrip_api_key: str = ""

    # --- Strava OAuth --------------------------------------------------------
    strava_client_id: str = ""
    strava_client_secret: str = ""
    strava_redirect_uri: str = "http://localhost:8000/auth/strava/callback"
    strava_success_url: str = "http://localhost:5173"
    # Fernet key (base64url-encoded 32 bytes) used to encrypt Strava OAuth
    # tokens at rest. Empty string disables encryption (legacy plaintext path).
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    strava_token_key: str = ""
    # Also accept exp://... and http://localhost:8081/... as the Strava connect
    # return_to (Expo dev builds). Off in prod.
    strava_return_to_allow_dev: bool = False

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
    # Link-token TTLs (seconds). Codes have a fixed 15-minute TTL.
    email_verification_ttl_s: int = 24 * 3600
    password_reset_ttl_s: int = 3600
    # Also send (and accept) the old ?verify= / ?reset= email links, alongside
    # 6-digit codes. On while the old website is still up.
    legacy_email_links: bool = True

    # --- Rate limiting -------------------------------------------------------
    # Redis URL for cross-worker rate limit state. Defaults to in-process
    # memory (fine for single-worker dev). Set to redis://localhost:6379 in prod.
    redis_url: str = ""
    # Let /tiles/* requests without a ?t= tile token through (strict per-IP
    # limit). On while the old website, which sends no tokens, is still up.
    tiles_allow_tokenless: bool = True

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
