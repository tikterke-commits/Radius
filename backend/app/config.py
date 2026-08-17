from enum import Enum

from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthzFailMode(str, Enum):
    """What authorize() returns when the backend can't reach its own store in time.

    OPEN: grant access on failure. Never take the network offline for a backend
    hiccup, at the cost of possibly letting a suspended/over-quota subscriber
    through until the next authorize cycle.
    CLOSED: deny access on failure. Never let policy be silently bypassed, at
    the cost of a backend outage becoming a network outage.

    This is deliberately explicit and per-deployment configurable rather than
    hardcoded, per A1's "explicit, configurable fail-open/fail-closed policy."
    """

    OPEN = "open"
    CLOSED = "closed"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://radius:radius@localhost:5432/radius_platform"
    redis_url: str = "redis://localhost:6379/0"
    clickhouse_url: str = "http://localhost:8123"
    clickhouse_user: str = "radius"
    clickhouse_password: str = ""
    clickhouse_db: str = "radius_telemetry"

    authz_fail_mode: AuthzFailMode = AuthzFailMode.CLOSED
    authz_backend_timeout_ms: int = 40  # leaves headroom under the 50ms p99 budget
    authz_decision_cache_ttl_s: int = 30

    jwt_secret: str = "change-me-in-.env"
    jwt_algorithm: str = "HS256"

    import_enabled: bool = False  # Part F: flag-gated, off after cutover


settings = Settings()
