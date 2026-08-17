"""The four-gate authorization model (A2). Deliberately not a single boolean
`active` — each gate is independently computed and independently queryable so
support can be shown exactly which gate failed, matching the legacy system's
{status, traffic, expiration, uptime} composite without collapsing it.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SubscriberSnapshot:
    """Everything the gate computation needs, already fetched — kept dumb and
    dependency-free so this is a pure function, unit-testable without a DB
    or Redis in the loop.
    """

    enabled: bool
    expiration: datetime | None
    quota_rx_remaining: int | None  # None = unlimited
    quota_tx_remaining: int | None
    quota_rxtx_remaining: int | None
    quota_uptime_remaining_s: int | None
    now: datetime


@dataclass(frozen=True)
class GateResult:
    status_gate: bool
    traffic_gate: bool
    expiration_gate: bool
    uptime_gate: bool

    @property
    def all_pass(self) -> bool:
        return self.status_gate and self.traffic_gate and self.expiration_gate and self.uptime_gate

    def failed_gates(self) -> list[str]:
        gates = {
            "status": self.status_gate,
            "traffic": self.traffic_gate,
            "expiration": self.expiration_gate,
            "uptime": self.uptime_gate,
        }
        return [name for name, passed in gates.items() if not passed]


def evaluate_gates(snapshot: SubscriberSnapshot) -> GateResult:
    status_gate = snapshot.enabled

    expiration_gate = snapshot.expiration is None or snapshot.expiration > snapshot.now

    traffic_gate = _quota_gate_passes(
        snapshot.quota_rx_remaining
    ) and _quota_gate_passes(snapshot.quota_tx_remaining) and _quota_gate_passes(
        snapshot.quota_rxtx_remaining
    )

    uptime_gate = _quota_gate_passes(snapshot.quota_uptime_remaining_s)

    return GateResult(
        status_gate=status_gate,
        traffic_gate=traffic_gate,
        expiration_gate=expiration_gate,
        uptime_gate=uptime_gate,
    )


def _quota_gate_passes(remaining: int | None) -> bool:
    """None means unlimited (no quota configured for this kind/period).
    Anything <= 0 means exhausted."""
    return remaining is None or remaining > 0
