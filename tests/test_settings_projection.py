"""Frozen settings projections retain exact public bytes and isolation."""

from dataclasses import asdict, dataclass, field

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.detector_config import preset
from cryptowatcher.storage import encode


@pytest.mark.parametrize(
    "settings",
    (
        OwnerSettings(),
        OwnerSettings(
            watchlist=("spot/BTCUSDT", "linear/ETHUSDT"),
            detector_parameters=preset("normal"),
            price_move_min=2.0000000000000004,
        ),
        OwnerSettings(outcome_horizons=(1, 10, 100), active_detectors=()),
    ),
)
def test_projection_keeps_exact_json_and_tuple_types(settings):
    expected = asdict(settings)
    actual = settings.payload()
    assert actual == expected and encode(actual) == encode(expected)
    assert {key: type(value) for key, value in actual.items()} == {
        key: type(value) for key, value in expected.items()
    }
    actual["price_move_min"] = 123
    actual["detector_parameters"] = ()
    assert settings.payload() == expected


def test_projection_preserves_mutable_subclass_deep_copy():
    @dataclass(frozen=True)
    class Extended(OwnerSettings):
        extra: list[str] = field(default_factory=lambda: ["original"])

    settings = Extended()
    returned = settings.payload()
    returned["extra"].append("changed")
    assert settings.extra == ["original"]
    assert settings.payload() == asdict(settings)


def test_nonstandard_mutable_value_uses_original_deep_copy():
    settings = OwnerSettings()
    # Even an internally corrupted object must not expose a mutable alias.
    object.__setattr__(settings, "detector_parameters", (("fixture", {"a": [1]}),))
    returned = settings.payload()
    returned["detector_parameters"][0][1]["a"].append(2)
    assert settings.detector_parameters[0][1] == {"a": [1]}
