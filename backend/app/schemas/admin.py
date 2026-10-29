"""Admin API shapes (consumed by Atlas). Enum-like fields are Literal-validated
here — the database stores them as plain strings (see app/models/plan.py).

Update models are generated from the create models with every field made
optional (partial()), so a PATCH only touches the fields it sends and the
constraints stay defined in exactly one place.
"""

import copy
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from ipaddress import IPv4Address
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from app.actuation.coa import known_attribute
from app.policy.decision import RESERVED_ATTRIBUTES

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_DAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HHMM = r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$"
LIST_NAME = r"^[A-Za-z0-9._-]{1,64}$"
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2}){5}$")
USERNAME = r"^[A-Za-z0-9._@-]{1,64}$"


def partial(model: type[BaseModel], name: str) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for fname, finfo in model.model_fields.items():
        f = copy.copy(finfo)
        f.default = None
        f.default_factory = None
        fields[fname] = (Optional[finfo.annotation], f)
    return create_model(name, __base__=model, **fields)


def normalize_mac(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not MAC_RE.match(value):
        raise ValueError("not a MAC address")
    hexes = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))


# --- shared pieces ----------------------------------------------------------

class FupTierIn(BaseModel):
    after_bytes: int = Field(ge=0)
    rx_limit_bps: int = Field(gt=0)
    tx_limit_bps: int = Field(gt=0)


class CustomAttribute(BaseModel):
    attribute: str
    value: str | int

    @field_validator("attribute")
    @classmethod
    def _known(cls, v: str) -> str:
        if v in RESERVED_ATTRIBUTES:
            raise ValueError(f"{v} is computed by the engine; set it through the profile fields instead")
        if not known_attribute(v):
            raise ValueError(f"{v} is not in the RADIUS dictionary (app/actuation/dictionary)")
        return v


class WindowIn(BaseModel):
    start: str = Field(pattern=HHMM)
    end: str = Field(pattern=HHMM)
    days: list[Day] = Field(default_factory=lambda: list(ALL_DAYS))


class TodWindowIn(WindowIn):
    applies_to: str = Field(default="all", pattern=r"^[A-Za-z0-9_*-]{1,32}$")
    speed_multiplier: float = Field(default=1.0, gt=0, le=1)
    rx_limit_bps: int | None = Field(default=None, gt=0)
    tx_limit_bps: int | None = Field(default=None, gt=0)
    address_list: str | None = Field(default=None, pattern=LIST_NAME)


class Page(BaseModel):
    items: list[Any]
    total: int
    page: int
    page_size: int


# --- profiles ---------------------------------------------------------------

class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    profile_name_normalized: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=255)
    enabled: bool = True
    price: Decimal = Field(default=Decimal("0"), ge=0)
    validity_days: int = Field(default=30, ge=1, le=3650)

    rx_limit_bps: int = Field(gt=0, description="subscriber upload")
    tx_limit_bps: int = Field(gt=0, description="subscriber download")
    burst_rx_bps: int | None = Field(default=None, gt=0)
    burst_tx_bps: int | None = Field(default=None, gt=0)
    burst_threshold_rx_bps: int | None = Field(default=None, gt=0)
    burst_threshold_tx_bps: int | None = Field(default=None, gt=0)
    burst_time_s: int | None = Field(default=None, ge=1, le=3600)
    priority: int | None = Field(default=None, ge=1, le=8)
    min_rx_bps: int | None = Field(default=None, gt=0)
    min_tx_bps: int | None = Field(default=None, gt=0)

    ip_pool: str | None = Field(default=None, pattern=LIST_NAME)
    mikrotik_group: str | None = Field(default=None, pattern=LIST_NAME)
    address_list: str | None = Field(default=None, pattern=LIST_NAME)
    session_timeout_s: int | None = Field(default=None, ge=60)
    idle_timeout_s: int | None = Field(default=None, ge=60)
    interim_interval_s: int | None = Field(default=None, ge=60, le=86400)
    enforce_quota_on_nas: bool = False
    custom_attributes: list[CustomAttribute] = Field(default_factory=list)

    quota_rx_monthly: int | None = Field(default=None, ge=0)
    quota_tx_monthly: int | None = Field(default=None, ge=0)
    quota_rxtx_monthly: int | None = Field(default=None, ge=0)
    quota_uptime_monthly_s: int | None = Field(default=None, ge=0)
    quota_rx_daily: int | None = Field(default=None, ge=0)
    quota_tx_daily: int | None = Field(default=None, ge=0)
    quota_rxtx_daily: int | None = Field(default=None, ge=0)
    quota_uptime_daily_s: int | None = Field(default=None, ge=0)
    monthly_reset: Literal["renewal", "calendar"] = "renewal"

    on_quota_exhausted: Literal["block", "throttle", "walled_garden"] = "block"
    exhausted_rx_bps: int | None = Field(default=None, gt=0)
    exhausted_tx_bps: int | None = Field(default=None, gt=0)
    on_expired: Literal["block", "walled_garden"] = "block"

    fup_tiers: list[FupTierIn] = Field(default_factory=list)
    daily_policy_id: uuid.UUID | None = None
    time_of_day_policy_id: uuid.UUID | None = None
    default_concurrent_sessions: int = Field(default=1, ge=1, le=16)


ProfilePatch = partial(ProfileIn, "ProfilePatch")


class ProfileOut(ProfileIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    profile_name_normalized: str
    created_at: datetime
    subscriber_count: int = 0
    rate_limit_preview: str = ""


class SimulateIn(BaseModel):
    at: datetime | None = None
    today_bytes: int = Field(default=0, ge=0)
    period_bytes: int = Field(default=0, ge=0)


# --- daily / time-of-day policies -------------------------------------------

class DailyPolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=255)
    counted: Literal["rxtx", "tx", "rx"] = "rxtx"
    tiers: list[FupTierIn] = Field(default_factory=list)
    daily_quota_bytes: int | None = Field(default=None, gt=0)
    on_exhaust: Literal["block", "throttle", "walled_garden"] = "throttle"
    exhausted_rx_bps: int | None = Field(default=None, gt=0)
    exhausted_tx_bps: int | None = Field(default=None, gt=0)
    free_windows: list[WindowIn] = Field(default_factory=list)


DailyPolicyPatch = partial(DailyPolicyIn, "DailyPolicyPatch")


class DailyPolicyOut(DailyPolicyIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    profile_count: int = 0


class TimePolicyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    windows: list[TodWindowIn] = Field(default_factory=list)


TimePolicyPatch = partial(TimePolicyIn, "TimePolicyPatch")


class TimePolicyOut(TimePolicyIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    active_now: list[int] = Field(default_factory=list)
    profile_count: int = 0


# --- NAS --------------------------------------------------------------------

def _check_secret(v: str | None) -> str | None:
    if v is None:
        return v
    if len(v) < 8 or len(v) > 64 or '"' in v or "\\" in v or not v.isprintable():
        raise ValueError("secret must be 8-64 printable characters without quotes or backslashes")
    return v


class NasIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    ip: IPv4Address
    secret: str
    ros_version: str = Field(min_length=1, max_length=20)
    coa_port: int = Field(default=3799, ge=1, le=65535)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=255)
    capability_matrix: dict = Field(default_factory=dict)

    _secret = field_validator("secret")(classmethod(lambda cls, v: _check_secret(v)))


NasPatch = partial(NasIn, "NasPatch")


class NasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    ip: str
    ros_version: str
    coa_port: int
    enabled: bool
    description: str | None
    capability_matrix: dict
    created_at: datetime
    online_sessions: int = 0

    @field_validator("ip", mode="before")
    @classmethod
    def _ip_str(cls, v):
        return str(v)


# --- subscribers ------------------------------------------------------------

class SubscriberEditable(BaseModel):
    firstname: str | None = Field(default=None, max_length=80)
    lastname: str | None = Field(default=None, max_length=80)
    company: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=160)
    address: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=80)
    notes: str | None = None
    national_id: str | None = Field(default=None, max_length=60)
    contract_id: str | None = Field(default=None, max_length=60)
    static_ip: IPv4Address | None = None
    mac_binding: str | None = None
    concurrent_session_limit: int | None = Field(default=None, ge=1, le=16)
    rx_limit_override_bps: int | None = Field(default=None, gt=0)
    tx_limit_override_bps: int | None = Field(default=None, gt=0)
    custom_attributes: list[CustomAttribute] = Field(default_factory=list)
    exempt_time_of_day: bool = False
    exempt_fup: bool = False
    enabled: bool = True

    @field_validator("mac_binding", mode="before")
    @classmethod
    def _mac(cls, v):
        return normalize_mac(v)


SubscriberPatch = partial(SubscriberEditable, "SubscriberPatch")


class SubscriberCreate(SubscriberEditable):
    username: str = Field(pattern=USERNAME)
    password: str | None = Field(default=None, min_length=6, max_length=128)
    generate_password: bool = False
    profile_id: uuid.UUID
    expiration: datetime | None = None
    validity_days: int | None = Field(default=None, ge=1, le=3650)


class SubscriberRow(BaseModel):
    id: uuid.UUID
    username: str
    firstname: str | None
    lastname: str | None
    phone: str | None
    city: str | None
    profile_id: uuid.UUID | None
    profile_name: str | None
    expiration: datetime | None
    status: Literal["active", "expired", "disabled"]
    online: bool
    static_ip: str | None
    mac_binding: str | None
    last_online: datetime | None
    created_at: datetime


class PasswordIn(BaseModel):
    password: str | None = Field(default=None, min_length=6, max_length=128)
    generate: bool = False


class RenewIn(BaseModel):
    days: int | None = Field(default=None, ge=1, le=3650)
    reset_quota: bool | None = None


class ChangeProfileIn(BaseModel):
    profile_id: uuid.UUID
    when: Literal["now", "next_renewal", "at"] = "now"
    at: datetime | None = None
    reset_quota: bool = True


class QuotaResetIn(BaseModel):
    period: Literal["daily", "monthly", "all"] = "all"


class TopUpIn(BaseModel):
    bytes: int = Field(gt=0)
    kind: Literal["rxtx", "rx", "tx"] = "rxtx"
    period: Literal["monthly", "daily"] = "monthly"


class MacIn(BaseModel):
    action: Literal["bind_current", "set", "clear"]
    mac: str | None = None

    @field_validator("mac", mode="before")
    @classmethod
    def _mac(cls, v):
        return normalize_mac(v)


class BulkIn(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=1000)
    action: Literal["enable", "disable", "renew", "disconnect", "change_profile"]
    profile_id: uuid.UUID | None = None
    days: int | None = Field(default=None, ge=1, le=3650)


class PanicIn(BaseModel):
    on: bool


class UsageDay(BaseModel):
    day: date
    rx_bytes: int
    tx_bytes: int
    uptime_s: int
    counted_rx_bytes: int
    counted_tx_bytes: int
