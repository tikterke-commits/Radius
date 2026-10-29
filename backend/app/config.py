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
    # A bare fail-open grant with no reply attributes means an *uncapped* pipe
    # for however long the backend stays unreachable — not just "access
    # continues," but "every plan's speed ceiling disappears at once" during
    # exactly the kind of incident where the network is already stressed.
    # Applied only when fail-open triggers with no cached decision to fall
    # back on (see app/api/radius.py).
    authz_fail_open_default_rate_bps: int = 5_000_000  # conservative floor, not a plan's real rate

    jwt_secret: str = "change-me-in-.env"
    jwt_algorithm: str = "HS256"

    import_enabled: bool = False  # Part F: flag-gated, off after cutover

    # --- Policy clock -----------------------------------------------------
    # Every time-of-day window, free-hours window and daily reset is local
    # time. RADIUS timestamps are UTC; policy evaluation converts first.
    timezone: str = "Asia/Beirut"
    daily_reset_time: str = "00:00"  # local HH:MM when daily quotas/usage roll over

    # --- Admin API (consumed by Atlas) ------------------------------------
    # Empty = admin API disabled (every /api/v1 call returns 503). Atlas holds
    # this key; RBAC for humans lives in Atlas, the engine trusts the key and
    # records the X-Actor header in the audit log.
    admin_api_key: str = ""
    default_tenant_name: str = "Data Plus Lebanon"

    # Fernet key for NAS shared secrets at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    nas_secret_key: str = ""
    # Where the backend renders FreeRADIUS client definitions from nas_devices.
    # Shared volume with the freeradius container, which restarts radiusd
    # when the file's checksum changes (radius/entrypoint.sh).
    radius_clients_path: str = "/etc/freeradius-clients/nas.conf"

    # --- Actuation (CoA / Disconnect toward the NAS fleet) ----------------
    # Atlas doctrine: dry-run by default, change ceiling, panic switch.
    actuation_dry_run: bool = True
    actuation_max_per_minute: int = 120
    coa_timeout_s: float = 2.0
    coa_retries: int = 1
    # Attributes a MikroTik applies mid-session via CoA. A change to anything
    # else (Framed-Pool, Mikrotik-Group, Framed-IP-Address...) can only take
    # effect on a new session, so it's actuated as Disconnect + reconnect.
    # Add Mikrotik-Address-List here only after verifying it on your ROS
    # builds with a test account.
    coa_mutable_attributes: list[str] = ["Mikrotik-Rate-Limit", "Session-Timeout"]

    # A session with no accounting packet for this long is treated as dead
    # (lost Stop). Should be > 2x the NAS interim-update interval.
    session_stale_after_s: int = 900

    # Walled garden: the address list a router uses to redirect expired /
    # exhausted subscribers to a portal page, plus the rate they get there.
    walled_garden_address_list: str = "walled-garden"
    walled_garden_rx_bps: int = 256_000
    walled_garden_tx_bps: int = 256_000

    auth_log_retention_days: int = 30
    worker_tick_s: int = 15


settings = Settings()
