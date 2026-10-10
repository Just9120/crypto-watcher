"""Validated infrastructure and owner settings; importing never starts the bot."""

from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

TIMEFRAMES = ("1m", "3m", "5m", "15m")


class ConfigError(ValueError):
    """Messages name the invalid field and never echo its value."""


def number(value: Any, field: str, *, minimum: float = 0, integer: bool = False) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or result <= minimum:
            raise ValueError
        if integer and not result.is_integer():
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError) as exc:
        raise ConfigError(f"Invalid {field}") from exc


def normalize_pair(raw: str) -> str:
    pair = raw.strip().upper().replace("/", "")
    if not pair.endswith("USDT"):
        pair += "USDT"
    if not re.fullmatch(r"[A-Z0-9]{2,60}USDT", pair):
        raise ConfigError("Invalid instrument symbol")
    return pair


@dataclass(frozen=True)
class OwnerSettings:
    bybit_category: str = "spot"
    alert_universe_mode: str = "top"
    custom_pairs: tuple[str, ...] = ()
    signal_timeframe: str = "5m"
    price_move_min: float = 2.0
    turnover_spike_min: float = 3.0
    liquidity_floor_24h: float = 1_000_000.0
    radar_poll_sec: int = 90
    cooldown_min: int = 10
    sma_periods: int = 12

    def __post_init__(self) -> None:
        if self.bybit_category not in ("spot", "linear"):
            raise ConfigError("Invalid bybit_category")
        if self.alert_universe_mode not in ("top", "custom"):
            raise ConfigError("Invalid alert_universe_mode")
        if self.signal_timeframe not in TIMEFRAMES:
            raise ConfigError("Invalid signal_timeframe")
        for key in ("price_move_min", "liquidity_floor_24h"):
            number(getattr(self, key), key)
        number(self.turnover_spike_min, "turnover_spike_min", minimum=1)
        for key in ("radar_poll_sec", "cooldown_min", "sma_periods"):
            number(getattr(self, key), key, minimum=1 if key == "sma_periods" else 0, integer=True)
        if not isinstance(self.custom_pairs, tuple) or any(
            not isinstance(x, str) or normalize_pair(x) != x for x in self.custom_pairs
        ):
            raise ConfigError("Invalid custom_pairs")
        if len(set(self.custom_pairs)) != len(self.custom_pairs):
            raise ConfigError("Duplicate custom_pairs")

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, values: Mapping[str, Any]) -> OwnerSettings:
        if not isinstance(values, dict):
            raise ConfigError("Invalid owner settings")
        fields = cls.__dataclass_fields__
        if set(values) - set(fields):
            raise ConfigError("Unknown owner settings field")
        parsed = dict(values)
        if "custom_pairs" in parsed:
            if not isinstance(parsed["custom_pairs"], (list, tuple)):
                raise ConfigError("Invalid custom_pairs")
            parsed["custom_pairs"] = tuple(parsed["custom_pairs"])
        for key in ("radar_poll_sec", "cooldown_min", "sma_periods"):
            if key in parsed:
                parsed[key] = int(number(parsed[key], key, integer=True))
        for key in ("price_move_min", "turnover_spike_min", "liquidity_floor_24h"):
            if key in parsed:
                parsed[key] = number(parsed[key], key)
        return cls(**parsed)

    def changed(self, **changes: Any) -> OwnerSettings:
        return OwnerSettings.from_payload({**self.payload(), **changes})

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OwnerSettings:
        defaults = cls().payload()
        for field, key in (
            ("bybit_category", "BYBIT_CATEGORY"),
            ("signal_timeframe", "SIGNAL_TIMEFRAME"),
            ("price_move_min", "PRICE_MOVE_MIN"),
            ("turnover_spike_min", "TURNOVER_SPIKE_MIN"),
            ("liquidity_floor_24h", "LIQUIDITY_FLOOR_24H"),
            ("radar_poll_sec", "RADAR_POLL_SEC"),
            ("cooldown_min", "COOLDOWN_MIN"),
            ("sma_periods", "SMA_PERIODS"),
        ):
            if key in env:
                defaults[field] = env[key]
        return cls.from_payload(defaults)


@dataclass(frozen=True)
class Infrastructure:
    owner_id: int
    delivery_chat_id: int
    expected_bot_id: int
    bot_token: str
    database: Path
    legacy_state: Path
    engine_interval: int
    defaults: OwnerSettings
    source_revision: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Infrastructure:
        env = os.environ if env is None else env
        owner = int(number(env.get("OWNER_USER_ID"), "OWNER_USER_ID", integer=True))
        chat = int(number(env.get("DELIVERY_CHAT_ID", str(owner)), "DELIVERY_CHAT_ID", integer=True))
        if chat != owner:
            raise ConfigError("DELIVERY_CHAT_ID must be the owner's private chat")
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[1-9][0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigError("Invalid TELEGRAM_BOT_TOKEN")
        expected = int(number(env.get("EXPECTED_BOT_ID"), "EXPECTED_BOT_ID", integer=True))
        if int(token.split(":", 1)[0]) != expected:
            raise ConfigError("TELEGRAM_BOT_TOKEN bot identity mismatch")
        legacy = Path(env.get("STATE_FILE", "/data/state_crypto_watcher.json"))
        revision = env.get("SOURCE_REVISION", "UNSET")
        if revision != "UNSET" and not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ConfigError("Invalid SOURCE_REVISION")
        return cls(
            owner_id=owner,
            delivery_chat_id=chat,
            expected_bot_id=expected,
            bot_token=token,
            database=Path(env.get("DATABASE_FILE", str(legacy.with_name("cryptowatcher.sqlite3")))),
            legacy_state=legacy,
            engine_interval=int(
                number(env.get("ENGINE_INTERVAL_SEC", "60"), "ENGINE_INTERVAL_SEC", integer=True)
            ),
            defaults=OwnerSettings.from_env(env),
            source_revision=revision,
        )


def authorized(update: Any, infrastructure: Infrastructure) -> bool:
    """No state read or write precedes this shared command/callback guard."""
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    return bool(
        user
        and chat
        and user.id == infrastructure.owner_id
        and chat.id == infrastructure.delivery_chat_id
        and chat.type == "private"
    )
