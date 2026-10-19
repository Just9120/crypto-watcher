import time

import pytest

from cryptowatcher import numeric_blocks, numeric_pool
from cryptowatcher.events import identity
from cryptowatcher.history_codec import Codec
from cryptowatcher.market_storage import enable
from cryptowatcher.record_codec import pack_binary
from cryptowatcher.storage import encode
from cryptowatcher.storage_repack import normalized_record, record_rows
from tests.test_metric_price_context import frozen
from tests.test_numeric_blocks import finish, setup
from tests.test_numeric_price_blocks import activate


@pytest.mark.parametrize("method", ["1", "2", 1])
def test_outcome_producer_uses_only_exact_supported_frozen_method(store, method):
    setup(store)
    activate(store)
    codec = Codec(store.db)
    original = {**frozen(), "methodology_version": method}
    with store.db:
        packed = codec.pack(original)
    assert encode(codec.unpack(packed)) == encode(original)
    data = numeric_blocks.read(store.db, 0)
    assert (data[3][:4] == b"NB2\x00") == (method == "1")
    assert numeric_blocks.summary(store.db)["issued"] == 1


def test_comparator_outcomes_inherit_parent_method_and_future_before_field_iteration(store):
    setup(store)
    activate(store)
    codec = Codec(store.db)
    future = frozen()["source_inputs"]
    outcomes = {}
    for horizon in (1, 2, 3):
        value = frozen(horizon)
        value.pop("source_inputs")
        value.pop("methodology_version")
        outcomes[str(horizon)] = value
    # future_inputs precedes outcomes and exercises the old window partition
    # local variable independently of the inherited price context.
    original = {"methodology_version": "1", "samples": [{"future_inputs": future, "outcomes": outcomes}]}
    with store.db:
        packed = codec.pack(original)
    assert encode(codec.unpack(packed)) == encode(original)
    assert numeric_blocks.read(store.db, 0)[3][:4] == b"NB2\x00"


def test_existing_retained_record_recode_keeps_reference_and_counts_numeric_saving(store):
    setup(store)
    codec = Codec(store.db)
    original = frozen()
    with store.db:
        packed = codec.pack(original)
        key = f"radar:v1:outcome:{identity('retained-price')}:0000000001"
        raw_owner = pack_binary(encode(packed))
        store.db.execute("INSERT INTO runtime VALUES(?,?)", (key, raw_owner))
    reference = packed["_history_storage"]["fields"]["metrics"]
    before = numeric_pool.raw_many(store.db, {numeric_pool.reference_id(reference)})
    activate(store)
    state = {"cursor": "", "records": 0, "saved_payload_bytes": 0}
    with store.db:
        store.db.execute("BEGIN")
        record_rows(store, state, 256, time.monotonic() + 10)
    assert state["records_complete"]
    assert state["saved_categories"]["numeric_details"] > 0
    assert store.db.execute("SELECT payload_json FROM runtime WHERE key=?", (key,)).fetchone()[0] == raw_owner
    assert encode(codec.unpack(packed)) == encode(original)
    assert before == numeric_pool.raw_many(store.db, {numeric_pool.reference_id(reference)})
    with store.db:
        assert normalized_record(store, key, packed) == packed


def test_price_offer_waits_for_legacy_body_transfer_without_changing_ids(store):
    enable(store.db)
    store.bounded = True
    codec = Codec(store.db)
    original = frozen()
    with store.db:
        first = codec.pack(original)
    reference = first["_history_storage"]["fields"]["metrics"]
    numeric_blocks.install(store.db, store.cached_bytes)
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('numeric_block_runtime','1')")
    activate(store)
    with store.db:
        second = codec.pack(original)
    assert second["_history_storage"]["fields"]["metrics"] == reference
    assert not store.db.execute("SELECT 1 FROM market_numeric_blocks").fetchone()
    assert codec.unpack(second) == original
    finish(store)
    with store.db:
        third = codec.pack(original)
    assert third["_history_storage"]["fields"]["metrics"] == reference
    assert numeric_blocks.read(store.db, 0)[3][:4] == b"NB2\x00"
    assert encode(codec.unpack(third)) == encode(original)
