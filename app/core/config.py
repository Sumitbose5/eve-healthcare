from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "EVE Diagnostics Booking API"
    environment: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/eve"
    auto_create_tables: bool = True

    # Security
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    bcrypt_rounds: int = 12
    webhook_secret: str = "dev-webhook-secret"
    admin_emails: str = ""

    # Redis / Celery
    redis_url: str | None = None
    celery_broker_url: str | None = None
    cache_ttl_seconds: int = 60

    # Rate limiting (requests per minute)
    rate_limit_enabled: bool = True
    auth_rate_limit_per_minute: int = 10
    payment_rate_limit_per_minute: int = 20

    # Payments / webhooks / bookings
    payment_success_rate: float = 0.8
    allow_simulated_outcome: bool = True
    webhook_max_attempts: int = 5
    pending_booking_ttl_minutes: int = 30

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Neon hands out "postgresql://..." URLs; route them to the psycopg3 driver.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @field_validator("redis_url", "celery_broker_url")
    @classmethod
    def _empty_to_none(cls, v: str | None) -> str | None:
        return v or None

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.jwt_secret.startswith("dev-only"):
        raise RuntimeError("JWT_SECRET must be set in production")
    return settings
