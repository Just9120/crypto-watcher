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


def test_additional_owner_identity_keeps_primary_delivery_and_private_guard(infrastructure):
    extra = infrastructure.owner_id + 1
    configured = replace(infrastructure, additional_owner_id=extra)
    assert infrastructure.owner_chat_ids == (infrastructure.owner_id,)
    assert configured.owner_chat_ids == (infrastructure.owner_id, extra)
    assert authorized(update(), configured)
    request = update()
    request.effective_user.id = request.effective_chat.id = extra
    assert authorized(request, configured)
    assert not authorized(request, infrastructure)
    request.effective_chat.id = configured.owner_id
    assert not authorized(request, configured)
    request.effective_chat.id = extra
    request.effective_chat.type = "group"
    assert not authorized(request, configured)
    assert not authorized(update(owner=False), configured)
    assert not authorized(update(), replace(configured, delivery_chat_id=extra))
    assert configured.owner_id == infrastructure.owner_id
    assert configured.delivery_chat_id == infrastructure.delivery_chat_id


@pytest.mark.parametrize("raw", ["", " ", "1234567890", " 1234567890 "])
def test_additional_owner_env_is_optional_and_never_replaces_primary(infrastructure, raw):
    configured = Infrastructure.from_env(
        {
            "OWNER_USER_ID": str(infrastructure.owner_id),
            "EXPECTED_BOT_ID": str(infrastructure.expected_bot_id),
            "TELEGRAM_BOT_TOKEN": infrastructure.bot_token,
            "ADDITIONAL_OWNER_USER_ID": raw,
        }
    )
    assert configured.additional_owner_id == (1234567890 if raw.strip() else None)
    assert configured.owner_id == configured.delivery_chat_id == infrastructure.owner_id


@pytest.mark.parametrize("raw", ["0", "-1", "1.0", "1e3", "nan", "inf", "1,2", "00123", "True", "1" * 17])
def test_invalid_additional_owner_fails_closed(infrastructure, raw):
    with pytest.raises(ConfigError, match="Invalid ADDITIONAL_OWNER_USER_ID"):
        Infrastructure.from_env(
            {
                "OWNER_USER_ID": str(infrastructure.owner_id),
                "EXPECTED_BOT_ID": str(infrastructure.expected_bot_id),
                "TELEGRAM_BOT_TOKEN": infrastructure.bot_token,
                "ADDITIONAL_OWNER_USER_ID": raw,
            }
        )
