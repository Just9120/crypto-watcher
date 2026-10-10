from dataclasses import replace

import pytest

from cryptowatcher.config import ConfigError, Infrastructure, OwnerSettings, authorized

from .conftest import update


@pytest.mark.parametrize("value", ["", "x", "nan", "inf", "-1", "0", "1.1"])
def test_invalid_identity_fails_closed(value):
    with pytest.raises(ConfigError):
        Infrastructure.from_env({"OWNER_USER_ID": value})


@pytest.mark.parametrize(
    "changes",
    [
        {"price_move_min": float("nan")},
        {"liquidity_floor_24h": float("inf")},
        {"turnover_spike_min": 1},
        {"radar_poll_sec": -1},
        {"sma_periods": 1},
        {"signal_timeframe": "1h"},
        {"bybit_category": "inverse"},
        {"custom_pairs": ["BTCUSDT", "BTCUSDT"]},
    ],
)
def test_owner_values_validated(changes):
    with pytest.raises(ConfigError):
        OwnerSettings().changed(**changes)


def test_auth_checks_user_and_private_chat(infrastructure):
    assert authorized(update(), infrastructure)
    assert not authorized(update(owner=False), infrastructure)
    assert not authorized(update(group=True), infrastructure)
    assert not authorized(update(), replace(infrastructure, delivery_chat_id=1))


def test_token_error_does_not_echo_secret():
    secret = "this-is-an-invalid-secret-token"
    with pytest.raises(ConfigError) as caught:
        Infrastructure.from_env({"OWNER_USER_ID": "1", "EXPECTED_BOT_ID": "2", "TELEGRAM_BOT_TOKEN": secret})
    assert secret not in str(caught.value)


def test_defaults_preserve_zero_length_watchlist():
    assert OwnerSettings().custom_pairs == ()


def test_single_character_crypto_symbol_is_valid():
    from cryptowatcher.config import normalize_pair

    assert normalize_pair("S") == "SUSDT"
    assert normalize_pair("WUSDT") == "WUSDT"
