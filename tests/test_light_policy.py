"""Current admission rules and explicit private-preserving light-model transition."""

import json
from contextlib import closing

import pytest

from cryptowatcher.config import ConfigError, OwnerSettings
from cryptowatcher.detector_config import FAMILIES
from cryptowatcher.market_storage import enable
from cryptowatcher.reset_history import LIGHT_POLICY, prepare, private_state, signature, verify_private
from cryptowatcher.storage import StateError, Store, encode

from .test_fresh_measurement import GENERATION, OWNER


@pytest.mark.parametrize("horizon", [4319, 4320])
def test_horizon_at_or_below_72h_is_admitted_without_growing_global_window(store, horizon):
    before = store.snapshot()
    store.update(before.revision, before.settings.changed(outcome_horizons=(horizon,)))
    assert store.snapshot().settings.candle_retention_days == 3
    assert store.snapshot().settings.outcome_horizons == (horizon,)


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome_horizons": (4321,)},
        {"candle_retention_days": 35},
        {"candle_retention_days": 2},
        {"detail_retention_days": 31},
        {"detector_parameters": (("breakout_lookback", 289),)},
    ],
)
def test_incompatible_current_admission_never_changes_revision_or_state(store, changes):
    before = store.snapshot()
    rows = list(store.db.iterdump())
    with pytest.raises(ConfigError):
        store.update(before.revision, before.settings.changed(**changes))
    assert store.snapshot() == before
    assert list(store.db.iterdump()) == rows


def test_existing_256_bar_source_bound_remains_stricter_than_72h_on_15m(store):
    before = store.snapshot()
    store.update(before.revision, before.settings.with_parameter("volatility_lookback", 254))
    saved = store.snapshot()
    with pytest.raises(ConfigError, match="256"):
        store.update(saved.revision, saved.settings.with_parameter("volatility_lookback", 255))
    assert store.snapshot() == saved


def test_invalid_new_startup_is_rejected_before_any_private_rows(tmp_path):
    with closing(Store(tmp_path / "new.sqlite3", OWNER)) as store:
        with pytest.raises(ConfigError, match="72"):
            store.initialize(OwnerSettings(outcome_horizons=(4321,)))
        assert store.db.execute("SELECT count(*) FROM owner_settings").fetchone()[0] == 0


def old_source(tmp_path, *, active_missing=True, horizon=60):
    source = (tmp_path / "old.sqlite3").resolve()
    with closing(Store(source, OWNER)) as store:
        store.initialize(OwnerSettings())
        enable(store.db)
        store.bounded = True
        store.put_runtime("history_generation", {"id": GENERATION})
        store.put_runtime("discardable-test-history", {"old": True})
        store.put_runtime("radar:v1:agent:preference", {"enabled": False, "schema": 1})
        store.mute("linear/ETHUSDT", 9999999999)
        store.set_enabled(False)
        payload = store.snapshot().settings.payload()
        payload["candle_retention_days"] = 35
        payload["outcome_horizons"] = [horizon]
        if active_missing:
            payload.pop("active_detectors")
        else:
            payload["active_detectors"] = ["price"]
        with store.db:
            store.db.execute("UPDATE owner_settings SET settings_json=?", (encode(payload),))
        preserved = private_state(store)
    return source, source.read_bytes(), preserved


@pytest.mark.parametrize("active_missing", [False, True])
def test_light_transition_restores_exact_private_then_only_migrates_approved_fields(tmp_path, active_missing):
    source, raw, preserved = old_source(tmp_path, active_missing=active_missing)
    recovery = (tmp_path / "private.json").resolve()
    candidate = (tmp_path / "candidate.sqlite3").resolve()
    proof = prepare(
        source,
        candidate,
        recovery,
        OWNER,
        expected_generation=GENERATION,
        discard_test_history=True,
        light_radar_transition=True,
    )
    assert source.read_bytes() == raw
    assert json.loads(recovery.read_bytes()) == preserved
    assert proof["light_radar_transition"] is True
    assert set(proof["configuration_changes"]) == (
        {"candle_retention_days", "active_detectors"} if active_missing else {"candle_retention_days"}
    )
    with closing(Store(candidate, OWNER, readonly=True)) as fresh:
        actual = private_state(fresh)
        assert fresh.snapshot().settings.candle_retention_days == 3
        assert fresh.snapshot().settings.active_detectors == (FAMILIES if active_missing else ("price",))
        assert fresh.snapshot().settings.alert_detectors == ("composite",)
        assert fresh.snapshot().revision == 2
        assert fresh.get_runtime("discardable-test-history") is None
        assert fresh.get_runtime("history_generation")["model"] == LIGHT_POLICY
        for name in preserved["tables"]:
            if name != "owner_settings":
                assert actual["tables"][name] == preserved["tables"][name]
        assert actual["preferences"] == preserved["preferences"]
        before_settings = json.loads(preserved["tables"]["owner_settings"][0][2])
        after_settings = json.loads(actual["tables"]["owner_settings"][0][2])
        assert {k: v for k, v in before_settings.items() if k not in proof["configuration_changes"]} == {
            k: v for k, v in after_settings.items() if k not in proof["configuration_changes"]
        }
        generation = fresh.get_runtime("history_generation")["id"]
    assert verify_private(candidate, recovery, OWNER)["state"] == "PASS"
    with pytest.raises(StateError, match="already exists"):
        prepare(
            candidate,
            (tmp_path / "repeat.sqlite3").resolve(),
            (tmp_path / "repeat.json").resolve(),
            OWNER,
            expected_generation=generation,
            discard_test_history=True,
            light_radar_transition=True,
        )
    assert not (tmp_path / "repeat.sqlite3").exists() and not (tmp_path / "repeat.json").exists()
    # Generic legacy discard must not be a way to reset valid light history.
    with pytest.raises(StateError, match="already exists"):
        prepare(
            candidate,
            (tmp_path / "legacy-repeat.sqlite3").resolve(),
            (tmp_path / "legacy-repeat.json").resolve(),
            OWNER,
            expected_generation=generation,
            discard_test_history=True,
        )
    assert not (tmp_path / "legacy-repeat.sqlite3").exists()
    with closing(Store(candidate, OWNER)) as fresh:
        saved = fresh.snapshot()
        fresh.update(saved.revision, saved.settings.changed(liquidity_floor_24h=2e6))
    with pytest.raises(StateError, match="configuration mismatch"):
        verify_private(candidate, recovery, OWNER)


def test_legacy_out_of_policy_horizons_are_readable_but_transition_requires_owner_resolution(tmp_path):
    source, raw, _ = old_source(tmp_path, horizon=10_000)
    with closing(Store(source, OWNER)) as store:
        before = list(store.db.iterdump())
        store.initialize(OwnerSettings())  # Ordinary open never migrates private settings.
        assert store.snapshot().settings.outcome_horizons == (10_000,)
        assert store.snapshot().settings.candle_retention_days == 35
        assert list(store.db.iterdump()) == before
    with pytest.raises(ConfigError, match="72"):
        prepare(
            source,
            (tmp_path / "candidate.sqlite3").resolve(),
            (tmp_path / "private.json").resolve(),
            OWNER,
            expected_generation=GENERATION,
            discard_test_history=True,
            light_radar_transition=True,
        )
    assert source.read_bytes() == raw
    assert not (tmp_path / "candidate.sqlite3").exists() and not (tmp_path / "private.json").exists()


def test_operator_private_preflight_requires_exact_bounded_generation_and_refuses_light_history(tmp_path):
    from scripts.install_history_reset import private_signature

    source, raw, preserved = old_source(tmp_path)
    assert private_signature(source, GENERATION) == signature(preserved)
    with pytest.raises(RuntimeError):
        private_signature(source)
    with pytest.raises(RuntimeError, match="generation changed"):
        private_signature(source, "b" * 32)
    assert source.read_bytes() == raw
    with closing(Store(source, OWNER)) as store:
        store.put_runtime("history_generation", {"id": GENERATION, "model": LIGHT_POLICY})
    with pytest.raises(RuntimeError, match="must not be reset"):
        private_signature(source, GENERATION)
