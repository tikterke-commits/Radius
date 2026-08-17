"""Loads profile_map.yaml and resolves a legacy SAS4 profile name to one of
the normalized tiers. An unmapped legacy name raises rather than silently
falling back to a default — per Part F, the ~47->8 collapse is a reviewable
config decision, not something the import should guess at.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_MAP_PATH = Path(__file__).parent / "profile_map.yaml"


@dataclass(frozen=True)
class NormalizedTier:
    normalized_name: str
    rx_limit_bps: int
    tx_limit_bps: int


class UnmappedProfileError(Exception):
    def __init__(self, legacy_name: str):
        self.legacy_name = legacy_name
        super().__init__(
            f"legacy profile name {legacy_name!r} matches no tier in profile_map.yaml — "
            "add a pattern for it rather than guessing a default tier"
        )


class ProfileNormalizer:
    def __init__(self, map_path: Path = DEFAULT_MAP_PATH):
        raw = yaml.safe_load(map_path.read_text())
        self._tiers: list[tuple[NormalizedTier, list[re.Pattern]]] = []
        for tier in raw["tiers"]:
            normalized = NormalizedTier(
                normalized_name=tier["normalized_name"],
                rx_limit_bps=tier["rx_limit_bps"],
                tx_limit_bps=tier["tx_limit_bps"],
            )
            patterns = [re.compile(p, re.IGNORECASE) for p in tier["legacy_name_patterns"]]
            self._tiers.append((normalized, patterns))

    def resolve(self, legacy_name: str) -> NormalizedTier:
        for tier, patterns in self._tiers:
            if any(p.match(legacy_name) for p in patterns):
                return tier
        raise UnmappedProfileError(legacy_name)

    def all_tiers(self) -> list[NormalizedTier]:
        return [tier for tier, _ in self._tiers]
