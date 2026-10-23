import json
from contextlib import closing

import pytest

from cryptowatcher.config import OwnerSettings
from cryptowatcher.storage import RevisionConflict, StateError, Store, encode


def test_parsing_is_pure_but_revision_and_owner_existence_are_always_current(store, monkeypatch):
    parse = OwnerSettings.from_payload
    calls = []

    def counted(cls, value):
        calls.append(value)
        return parse(value)

    monkeypatch.setattr(OwnerSettings, "from_payload", classmethod(counted))
    first = store.snapshot()
    assert store.snapshot() == first and len(calls) == 1
    with store.db:
        store.db.execute("UPDATE owner_settings SET revision=revision+1")
    assert store.snapshot().revision == first.revision + 1 and len(calls) == 1
    with store.db:
        store.db.execute("DELETE FROM owner_settings")
    with pytest.raises(StateError, match="missing"):
        store.snapshot()


def test_same_revision_new_payload_and_rolled_back_payload_cannot_reuse_stale_settings(store):
    first = store.snapshot()
    new = first.settings.changed(signal_timeframe="3m")
    store.db.execute("BEGIN")
    store.db.execute("UPDATE owner_settings SET settings_json=?", (encode(new.payload()),))
    assert store.snapshot().revision == first.revision
    assert store.snapshot().settings == new
    store.db.rollback()
    assert store.snapshot() == first
    committed = store.update(first.revision, first.settings.changed(signal_timeframe="15m"))
    assert store.snapshot() == committed
    with pytest.raises(RevisionConflict):
        store.update(first.revision, new)
    assert store.snapshot() == committed


@pytest.mark.parametrize("invalid", ["{", "[]", '{"price_move_min":0}', '{"unknown":1}'])
def test_changed_invalid_payload_is_rejected_after_a_valid_cache_hit(store, invalid):
    store.snapshot()
    with store.db:
        store.db.execute("UPDATE owner_settings SET settings_json=?", (invalid,))
    with pytest.raises(StateError, match="persisted owner"):
        store.snapshot()


def test_caller_settings_and_payload_mutations_do_not_touch_the_cached_proof(store):
    first = store.snapshot()
    expected = first.settings.payload()
    first.settings.__dict__["price_move_min"] = 987
    first.settings.__dict__["outcome_horizons"] = (999999,)
    payload = store.snapshot().settings.payload()
    payload["price_move_min"] = 876
    assert store.snapshot().settings.payload() == expected
    assert json.loads(
        store.db.execute("SELECT settings_json FROM owner_settings").fetchone()[0]
    ) == json.loads(encode(expected))


def test_frozen_reader_observes_its_snapshot_then_the_new_committed_revision(store):
    original = store.snapshot()
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        assert reader.snapshot() == original
        updated = store.update(original.revision, original.settings.changed(signal_timeframe="1m"))
        assert reader.snapshot() == original
        reader.db.rollback()
        reader.db.execute("BEGIN")
        assert reader.snapshot() == updated
        assert reader.db.total_changes == 0


def test_settings_share_one_real_byte_budget_and_are_cleared_with_other_pure_values(store):
    expected = store.snapshot()
    for i in range(40):
        raw = "Ж🙂" * 10000 + str(i)
        store.cached_text("fixture", str(i), raw, lambda: raw)
        assert store.reference_cache_bytes <= 1024 * 1024
        assert sum(size for _, size in store.reference_cache.values()) == store.reference_cache_bytes
    assert store.snapshot() == expected
    raw = store.db.execute("SELECT settings_json FROM owner_settings").fetchone()[0]
    with pytest.raises(StateError, match="pure cache"):
        store.cached_text("immutable-owner-settings", "", raw, lambda: "wrong type")
    store.close()
    assert store.reference_cache == {} and store.reference_cache_bytes == 0


def test_oversize_valid_settings_are_parsed_without_retaining_an_extra_pool(store, monkeypatch):
    settings = OwnerSettings(custom_pairs=tuple(f"ASSET{i}USDT" for i in range(16000)))
    raw = encode(settings.payload())
    with store.db:
        store.db.execute("UPDATE owner_settings SET settings_json=?", (raw,))
    parse = OwnerSettings.from_payload
    calls = []

    def counted(cls, value):
        calls.append(1)
        return parse(value)

    monkeypatch.setattr(OwnerSettings, "from_payload", classmethod(counted))
    assert store.snapshot().settings == settings
    assert store.snapshot().settings == settings
    assert len(calls) == 2
    assert ("immutable-owner-settings", "", raw) not in store.reference_cache
    assert store.reference_cache_bytes <= 1024 * 1024
