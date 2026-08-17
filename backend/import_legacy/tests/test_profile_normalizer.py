import pytest

from import_legacy.profile_normalizer import (
    DEFAULT_MAP_PATH,
    ProfileNormalizer,
    UnmappedProfileError,
)


@pytest.fixture
def normalizer():
    return ProfileNormalizer(DEFAULT_MAP_PATH)


def test_loads_all_placeholder_tiers(normalizer):
    names = {t.normalized_name for t in normalizer.all_tiers()}
    assert names == {"basic-10m", "standard-20m", "premium-50m"}


def test_resolves_exact_and_pattern_variants(normalizer):
    assert normalizer.resolve("10M Basic").normalized_name == "basic-10m"
    assert normalizer.resolve("Basic-10").normalized_name == "basic-10m"
    assert normalizer.resolve("50M Premium Plan").normalized_name == "premium-50m"


def test_resolution_is_case_insensitive(normalizer):
    assert normalizer.resolve("standard 20mbps").normalized_name == "standard-20m"


def test_unmapped_legacy_name_raises_not_silently_defaults(normalizer):
    with pytest.raises(UnmappedProfileError) as exc_info:
        normalizer.resolve("Some Totally Unknown Legacy Profile Name")
    assert exc_info.value.legacy_name == "Some Totally Unknown Legacy Profile Name"


def test_custom_map_file(tmp_path):
    map_file = tmp_path / "custom.yaml"
    map_file.write_text(
        """
tiers:
  - normalized_name: "only-tier"
    rx_limit_bps: 5000000
    tx_limit_bps: 5000000
    legacy_name_patterns:
      - "^Only.*$"
"""
    )
    normalizer = ProfileNormalizer(map_file)
    assert normalizer.resolve("OnlyThisOne").normalized_name == "only-tier"
    with pytest.raises(UnmappedProfileError):
        normalizer.resolve("Something Else")
