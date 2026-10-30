"""Validated infrastructure and owner settings; importing never starts the bot."""

from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .detector_config import FAMILIES, effective_parameters, preset, validate_parameters

TIMEFRAMES = ("1m", "3m", "5m", "15m")
METADATA_REFRESH_SECONDS = 600
TICKER_REFRESH_SECONDS = 300
SOURCE_REFRESH_GRACE_SECONDS = 60
CLOSED_CANDLE_GRACE_MS = 30_000


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
    if not re.fullmatch(r"[A-Z0-9]{1,60}USDT", pair):
        raise ConfigError("Invalid instrument symbol")
    return pair


def instrument_key(raw: str, default_market: str = "spot") -> str:
    parts = raw.strip().split("/", 1)
    if len(parts) == 2 and parts[1].upper() == "USDT":
        market, symbol = default_market, raw
    else:
        market, symbol = parts if len(parts) == 2 else (default_market, parts[0])
    market = market.lower()
    if market not in ("spot", "linear"):
        raise ConfigError("Invalid instrument market")
    return f"{market}/{normalize_pair(symbol)}"


@dataclass(frozen=True)
class OwnerSettings:
    bybit_category: str = "spot"
    alert_universe_mode: str = "top"
    custom_pairs: tuple[str, ...] = ()
    watchlist: tuple[str, ...] = ()
    watchlist_multiplier: float = 0.75
    signal_timeframe: str = "5m"
    price_move_min: float = 2.0
    turnover_spike_min: float = 3.0
    liquidity_floor_24h: float = 1_000_000.0
    radar_poll_sec: int = 90
    cooldown_min: int = 10
    sma_periods: int = 12
    profile: str = "custom"
    detector_parameters: tuple[tuple[str, float], ...] = ()
    active_detectors: tuple[str, ...] = FAMILIES
    alert_detectors: tuple[str, ...] = ("composite",)
    outcome_horizons: tuple[int, ...] = (5, 15, 30, 60)
    digest_interval_min: int = 60
    enrichment_enabled: bool = False
    detail_retention_days: int = 30
    candle_retention_days: int = 3

    def __post_init__(self) -> None:
        if (
            not isinstance(self.outcome_horizons, tuple)
            or not self.outcome_horizons
            or any(type(h) is not int or h <= 0 for h in self.outcome_horizons)
        ):
            raise ConfigError("Invalid outcome_horizons")
        for key in ("detail_retention_days", "candle_retention_days"):
            if (
                type(getattr(self, key)) is not int
                or not 1 <= getattr(self, key) <= (2**63 - 1 - 253402300799999) // 86400000
            ):
                raise ConfigError("Invalid " + key)
        # Historical snapshots remain typed/readable. Current admission policy
        # is validated separately; referenced outcome inputs can outlive the
        # general candle window, so horizons do not require a global archive.
        if max(self.outcome_horizons) >= self.detail_retention_days * 1440:
            raise ConfigError("Detail retention incompatible with outcome horizons")
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
        if not isinstance(self.watchlist, tuple) or any(
            not isinstance(x, str) or instrument_key(x) != x for x in self.watchlist
        ):
            raise ConfigError("Invalid watchlist")
        if len(set(self.watchlist)) != len(self.watchlist):
            raise ConfigError("Duplicate watchlist")
        number(self.watchlist_multiplier, "watchlist_multiplier")
        if self.profile not in ("custom", "normal", "conservative", "sensitive"):
            raise ConfigError("Invalid sensitivity profile")
        if not isinstance(self.detector_parameters, tuple) or any(
            not isinstance(p, tuple) for p in self.detector_parameters
        ):
            raise ConfigError("Invalid immutable detector parameters")
        try:
            validate_parameters(self.detector_parameters)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        if self.profile != "custom" and dict(self.detector_parameters) != dict(preset(self.profile)):
            raise ConfigError("Profile parameters do not match preset")
        for key in ("active_detectors", "alert_detectors"):
            selection = getattr(self, key)
            if (
                not isinstance(selection, tuple)
                or any(not isinstance(x, str) or x not in FAMILIES for x in selection)
                or len(set(selection)) != len(selection)
            ):
                raise ConfigError("Invalid " + key)
        if (
            not isinstance(self.outcome_horizons, tuple)
            or not self.outcome_horizons
            or any(type(x) is not int or x <= 0 for x in self.outcome_horizons)
            or len(set(self.outcome_horizons)) != len(self.outcome_horizons)
        ):
            raise ConfigError("Invalid outcome_horizons")
        number(self.digest_interval_min, "digest_interval_min", integer=True)
        if type(self.enrichment_enabled) is not bool:
            raise ConfigError("Invalid enrichment_enabled")

    def instruments(self) -> tuple[str, ...]:
        return self.watchlist or tuple(instrument_key(x, self.bybit_category) for x in self.custom_pairs)

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
        for key in ("custom_pairs", "watchlist", "active_detectors", "alert_detectors", "outcome_horizons"):
            if key in parsed:
                if not isinstance(parsed[key], (list, tuple)):
                    raise ConfigError(f"Invalid {key}")
                parsed[key] = tuple(parsed[key])
        if "detector_parameters" in parsed:
            try:
                parsed["detector_parameters"] = validate_parameters(parsed["detector_parameters"])
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        for key in (
            "radar_poll_sec",
            "cooldown_min",
            "sma_periods",
            "digest_interval_min",
            "detail_retention_days",
            "candle_retention_days",
        ):
            if key in parsed:
                parsed[key] = int(number(parsed[key], key, integer=True))
        for key in ("price_move_min", "turnover_spike_min", "liquidity_floor_24h", "watchlist_multiplier"):
            if key in parsed:
                parsed[key] = number(parsed[key], key)
        return cls(**parsed)

    def changed(self, **changes: Any) -> OwnerSettings:
        if self.detector_parameters and "detector_parameters" not in changes:
            parameters = dict(self.detector_parameters)
            if "price_move_min" in changes:
                parameters.update({f"price_{tf}": changes["price_move_min"] for tf in TIMEFRAMES})
            if "turnover_spike_min" in changes:
                parameters["turnover_ratio"] = changes["turnover_spike_min"]
            if "sma_periods" in changes:
                parameters["turnover_lookback"] = changes["sma_periods"]
            if any(k in changes for k in ("price_move_min", "turnover_spike_min", "sma_periods")):
                changes.update(profile="custom", detector_parameters=tuple(parameters.items()))
        return OwnerSettings.from_payload({**self.payload(), **changes})

    def with_profile(self, name: str) -> OwnerSettings:
        try:
            values = preset(name)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        return self.changed(profile=name, detector_parameters=values)

    def with_parameter(self, key: str, value: Any) -> OwnerSettings:
        parameters = effective_parameters(self)
        parameters[key] = value
        return self.changed(profile="custom", detector_parameters=tuple(parameters.items()))

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
