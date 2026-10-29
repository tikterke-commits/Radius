"""Mikrotik-Rate-Limit encoding.

RouterOS format (positional, rx/tx from the router's side of the PPP
interface — rx = subscriber upload, tx = subscriber download):

    rx/tx [burst-rx/burst-tx [threshold-rx/threshold-tx [time-rx/time-tx [priority [min-rx/min-tx]]]]]

A later field can't be given without the earlier ones, so missing
intermediate fields are written as 0/0. Values are plain bps integers.
"""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RateSpec:
    rx: int
    tx: int
    burst_rx: int | None = None
    burst_tx: int | None = None
    threshold_rx: int | None = None
    threshold_tx: int | None = None
    burst_time_s: int | None = None
    priority: int | None = None
    min_rx: int | None = None
    min_tx: int | None = None


def scale_to(spec: RateSpec, rx: int, tx: int) -> RateSpec:
    """Re-target a profile's rate shape to a new effective rate (FUP tier,
    peak window, override). Burst and threshold scale by the same ratio per
    direction so a 25% peak still bursts like the plan does, just at 25%;
    limit-at is clamped so it never exceeds the new max.
    """
    def ratio(new: int, old: int) -> float:
        return new / old if old else 1.0

    r_rx, r_tx = ratio(rx, spec.rx), ratio(tx, spec.tx)

    def sc(v: int | None, r: float) -> int | None:
        return None if v is None else int(v * r)

    return replace(
        spec,
        rx=rx,
        tx=tx,
        burst_rx=sc(spec.burst_rx, r_rx),
        burst_tx=sc(spec.burst_tx, r_tx),
        threshold_rx=sc(spec.threshold_rx, r_rx),
        threshold_tx=sc(spec.threshold_tx, r_tx),
        min_rx=None if spec.min_rx is None else min(spec.min_rx, rx),
        min_tx=None if spec.min_tx is None else min(spec.min_tx, tx),
    )


def format_rate_limit(spec: RateSpec) -> str:
    parts = [f"{spec.rx}/{spec.tx}"]

    has_burst = bool(spec.burst_rx or spec.burst_tx)
    if has_burst:
        # A burst at or below max-limit is meaningless to RouterOS; drop the
        # direction rather than emit a value the queue will ignore or reject.
        brx = spec.burst_rx if spec.burst_rx and spec.burst_rx > spec.rx else 0
        btx = spec.burst_tx if spec.burst_tx and spec.burst_tx > spec.tx else 0
        has_burst = bool(brx or btx)
    else:
        brx = btx = 0

    if has_burst:
        # Burst needs a threshold and a time to do anything; default to the
        # common 75%-of-max / 8s shape when the profile didn't specify.
        trx = spec.threshold_rx if spec.threshold_rx is not None else (spec.rx * 3 // 4 if brx else 0)
        ttx = spec.threshold_tx if spec.threshold_tx is not None else (spec.tx * 3 // 4 if btx else 0)
        bt = spec.burst_time_s if spec.burst_time_s else 8
        burst_fields = [f"{brx}/{btx}", f"{trx}/{ttx}", f"{bt if brx else 0}/{bt if btx else 0}"]
    else:
        burst_fields = ["0/0", "0/0", "0/0"]

    tail: list[str] = []
    has_min = bool(spec.min_rx or spec.min_tx)
    if spec.priority is not None or has_min:
        tail.append(str(spec.priority if spec.priority is not None else 8))
    if has_min:
        tail.append(f"{spec.min_rx or 0}/{spec.min_tx or 0}")

    if has_burst or tail:
        parts.extend(burst_fields)
    parts.extend(tail)
    return " ".join(parts)


def parse_rate_limit(value: str) -> RateSpec:
    """Inverse of format_rate_limit for plain-bps values (used by the profile
    preview and tests). Accepts k/M/G suffixes as RouterOS does."""
    fields = value.split()

    def num(s: str) -> int:
        s = s.strip()
        mult = {"k": 1_000, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}.get(s[-1:], 1)
        return int(float(s[:-1]) * mult) if mult != 1 else int(s)

    def pair(i: int) -> tuple[int, int] | None:
        if i >= len(fields):
            return None
        a, b = fields[i].split("/")
        return num(a), num(b)

    rx, tx = pair(0)
    burst, thr, times = pair(1), pair(2), pair(3)
    prio = int(fields[4]) if len(fields) > 4 else None
    mins = pair(5)
    return RateSpec(
        rx=rx, tx=tx,
        burst_rx=(burst[0] or None) if burst else None,
        burst_tx=(burst[1] or None) if burst else None,
        threshold_rx=(thr[0] or None) if thr else None,
        threshold_tx=(thr[1] or None) if thr else None,
        burst_time_s=(max(times) or None) if times else None,
        priority=prio,
        min_rx=(mins[0] or None) if mins else None,
        min_tx=(mins[1] or None) if mins else None,
    )
