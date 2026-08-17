"""Quota engine (A3): four quota types — remaining_rx, remaining_tx,
remaining_rxtx, remaining_uptime — each with independent monthly and daily
variants. Pure logic, no I/O: takes a ledger snapshot and an accounting
event, returns the updated ledger. The caller is responsible for persisting
the result (Postgres for durable state, Redis for the hot-path mirror —
see docs/00-part-h-design.md §2).
"""

from dataclasses import dataclass, replace
from enum import Enum


class QuotaKind(str, Enum):
    RX = "rx"
    TX = "tx"
    RXTX = "rxtx"
    UPTIME = "uptime"


class QuotaPeriod(str, Enum):
    MONTHLY = "monthly"
    DAILY = "daily"


@dataclass(frozen=True)
class QuotaLedger:
    """None for any field means "unlimited" for that kind/period — matches
    the gate engine's convention so the two modules agree on what "no quota
    configured" means.
    """

    rx_monthly: int | None
    tx_monthly: int | None
    rxtx_monthly: int | None
    uptime_monthly_s: int | None
    rx_daily: int | None
    tx_daily: int | None
    rxtx_daily: int | None
    uptime_daily_s: int | None

    def is_exhausted(self, kind: QuotaKind, period: QuotaPeriod) -> bool:
        value = self._get(kind, period)
        return value is not None and value <= 0

    def _get(self, kind: QuotaKind, period: QuotaPeriod) -> int | None:
        return getattr(self, quota_field_name(kind, period))


def quota_field_name(kind: QuotaKind, period: QuotaPeriod) -> str:
    if kind == QuotaKind.UPTIME:
        return "uptime_monthly_s" if period == QuotaPeriod.MONTHLY else "uptime_daily_s"
    return f"{kind.value}_{period.value}"


@dataclass(frozen=True)
class AccountingDelta:
    """One accounting interim-update or Stop record, already normalized to
    "subscriber's own" rx/tx (see Part E direction note: rx is what the
    router receives from the subscriber i.e. subscriber upload; tx is what
    the router sends to the subscriber i.e. subscriber download — this
    struct's field names follow that convention throughout the platform,
    not the reversed one).
    """

    rx_bytes: int
    tx_bytes: int
    session_seconds: int


def apply_usage(ledger: QuotaLedger, delta: AccountingDelta) -> QuotaLedger:
    """Decrement every configured quota field by the relevant portion of this
    accounting delta. rxtx gets the sum of rx+tx. Fields already at/below
    zero are clamped at zero rather than going negative — a late or
    duplicate accounting packet after exhaustion shouldn't produce a
    ledger that claims a negative debt.
    """
    rxtx_bytes = delta.rx_bytes + delta.tx_bytes

    return replace(
        ledger,
        rx_monthly=_decrement(ledger.rx_monthly, delta.rx_bytes),
        tx_monthly=_decrement(ledger.tx_monthly, delta.tx_bytes),
        rxtx_monthly=_decrement(ledger.rxtx_monthly, rxtx_bytes),
        uptime_monthly_s=_decrement(ledger.uptime_monthly_s, delta.session_seconds),
        rx_daily=_decrement(ledger.rx_daily, delta.rx_bytes),
        tx_daily=_decrement(ledger.tx_daily, delta.tx_bytes),
        rxtx_daily=_decrement(ledger.rxtx_daily, rxtx_bytes),
        uptime_daily_s=_decrement(ledger.uptime_daily_s, delta.session_seconds),
    )


def _decrement(remaining: int | None, amount: int) -> int | None:
    if remaining is None:
        return None
    return max(0, remaining - amount)


@dataclass(frozen=True)
class FupTier:
    after_bytes: int  # cumulative rxtx bytes consumed this period before this tier kicks in
    rx_limit_bps: int
    tx_limit_bps: int


def apply_fup(
    base_rx_limit_bps: int,
    base_tx_limit_bps: int,
    fup_tiers: list[FupTier],
    consumed_rxtx_bytes: int,
) -> tuple[int, int]:
    """Tiered FUP speed degradation (A4). Tiers are evaluated in ascending
    after_bytes order; the highest tier whose threshold has been crossed
    wins. No matching tier (fresh period, low usage) returns the plan's
    base rate unchanged.
    """
    applicable = [t for t in fup_tiers if consumed_rxtx_bytes >= t.after_bytes]
    if not applicable:
        return base_rx_limit_bps, base_tx_limit_bps
    winning = max(applicable, key=lambda t: t.after_bytes)
    return winning.rx_limit_bps, winning.tx_limit_bps
