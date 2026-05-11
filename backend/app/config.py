"""Application settings loaded from environment variables."""

from functools import lru_cache

from fastapi import HTTPException, status
from pydantic_settings import BaseSettings, SettingsConfigDict

# Regions for which we host DEM/COG data. Endpoints accepting a `region` query
# parameter must validate against this set — passing arbitrary strings would
# attempt arbitrary S3 lookups and bypass cost controls as the product expands.
ALLOWED_REGIONS: frozenset[str] = frozenset({"colorado"})


def validate_region(region: str) -> str:
    """Raise 400 if `region` isn't in the allowlist. Returns the validated region."""
    if region not in ALLOWED_REGIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown region: {region}")
    return region


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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
