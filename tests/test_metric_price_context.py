import copy

import pytest

from cryptowatcher import metric_codec, metric_price_codec
from cryptowatcher import metric_price_context as context
from cryptowatcher.history_codec import Codec
from cryptowatcher.market_storage import enable, window
from cryptowatcher.outcomes import calculate
from cryptowatcher.sources import Candle
from cryptowatcher.storage import encode

NOW = 1800000000000


def frozen(horizon=3):
    candles = [
        Candle.parse(
            [NOW + i * 60000, 100.0, 102.0, 99.0, 101.0, 1, 200], "1m", "fixture", NOW + 180000, NOW + 180000
        )
        for i in range(horizon)
    ]
    value = calculate(
        {"reference_time_ms": NOW, "reference_price": 100.0, "direction": "UP"}, horizon, candles
    )
    value["methodology_version"] = "1"
    return value


def proposal(store, value, parent=None, read=None):
    codec = Codec(store.db)
    read = read or codec.field_raw
    current = context.context(store.db, value, parent or context.Context(), read)
    body, view = metric_codec.canonical(value["metrics"])
    raw = context.candidate(store.db, value, metric_codec.pack(body), current, read)
    return raw, body, view


def with_reference(value, reference):
    result = {k: v for k, v in value.items() if k != "source_inputs"}
    result["_history_storage"] = {
        "schema": "cryptowatcher.shared-fields.v1",
        "fields": {"source_inputs": reference},
    }
    return result


def test_complete_inline_outcome_and_inherited_comparator_window_preserve_exact_metrics(store):
    enable(store.db)
    value = frozen()
    raw, body, view = proposal(store, value)
    assert raw is not None and metric_price_codec.frame(raw) == metric_codec.pack(body)
    assert encode(metric_codec.restore_view(metric_price_codec.unpack(raw), view)) == encode(value["metrics"])
    parent = context.context(
        store.db,
        {"methodology_version": "1", "future_inputs": value["source_inputs"]},
        context.Context(),
        Codec(store.db).field_raw,
    )
    for horizon in (1, 2, 3):
        outcome = frozen(horizon)
        outcome.pop("source_inputs")
        outcome.pop("methodology_version")
        raw, body, _ = proposal(store, outcome, parent)
        assert raw is not None and metric_price_codec.frame(raw) == metric_codec.pack(body)
    assert parent.future.loaded


@pytest.mark.parametrize(
    "change",
    [
        {"methodology_version": "2"},
        {"methodology_version": 1},
        {"state": "INCOMPLETE"},
        {"source_inputs": []},
        {"window_end_ms": NOW + 180001},
        {"reference_price": 0},
        {"reference_time_ms": NOW - 1},
        {"window_end_ms": NOW + (context.MAX_CANDLES + 1) * 60000},
    ],
)
def test_unknown_method_incomplete_and_unsupported_reference_keep_original_metrics(store, change):
    enable(store.db)
    value = {**frozen(), **change}
    original = encode(value)
    raw, _body, _view = proposal(store, value)
    assert raw is None
    assert encode(value) == original


@pytest.mark.parametrize("damage", ["gap", "duplicate", "reverse", "step", "price"])
def test_frozen_window_geometry_must_be_complete_before_offering_a_recipe(store, damage):
    enable(store.db)
    value = frozen()
    rows = value["source_inputs"]
    if damage == "gap":
        rows.pop(1)
    elif damage == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif damage == "reverse":
        rows.reverse()
    elif damage == "step":
        rows[0]["step"] = True
    else:
        rows[0]["high"] = float("nan")
    assert proposal(store, value)[0] is None


@pytest.mark.parametrize("tiles", [False, True])
def test_normalized_windows_check_actual_sources_and_avoid_materializing_full_json(store, tiles):
    from cryptowatcher import candle_blocks

    enable(store.db)
    if tiles:
        candle_blocks.install(store.db, store.cached_text)
        with store.db:
            store.db.execute("INSERT INTO market_meta VALUES('candle_block_runtime','1')")
    value = frozen()
    with store.db:
        reference = window(store.db, "spot/FIXUSDT/1m", value.pop("source_inputs"), NOW)
    value["_history_storage"] = {
        "schema": "cryptowatcher.shared-fields.v1",
        "fields": {"source_inputs": reference},
    }

    def no_assembly(_):
        pytest.fail("normalized price context must stream/check actual source points")

    raw, body, _ = proposal(store, value, read=no_assembly)
    assert raw is not None and metric_price_codec.frame(raw) == metric_codec.pack(body)
    with store.db:
        if tiles:
            store.db.execute("DROP TRIGGER market_candle_block_update_guard")
            store.db.execute("UPDATE market_candle_blocks SET payload=zeroblob(length(payload))")
        else:
            store.db.execute("DELETE FROM market_candles")
    with pytest.raises((ValueError, KeyError)):
        proposal(store, value, read=no_assembly)


def test_shared_blob_future_is_loaded_once_and_bad_original_checksum_is_not_silenced(store):
    enable(store.db)
    codec = Codec(store.db)
    value = frozen()
    with store.db:
        key = codec.save_bytes(encode(value["source_inputs"]).encode())
    reference = {"mode": "value2", "id": key}
    calls = []

    def read(ref):
        calls.append(ref)
        return codec.field_raw(ref)

    parent = context.context(
        store.db,
        {
            "methodology_version": "1",
            "_history_storage": {
                "schema": "cryptowatcher.shared-fields.v1",
                "fields": {"future_inputs": reference},
            },
        },
        context.Context(),
        read,
    )
    for horizon in (1, 2, 3):
        value = frozen(horizon)
        value.pop("source_inputs")
        assert proposal(store, value, parent, read)[0] is not None
    assert calls == [reference]
    with store.db:
        store.db.execute("DELETE FROM runtime WHERE key=?", ("historyblob:v1:" + key,))
    with pytest.raises(ValueError, match="Missing retained"):
        proposal(store, with_reference(frozen(), reference))


def test_bounds_reject_before_blob_inflation_or_unbounded_coordinate_expansion(store):
    enable(store.db)
    key = "f" * 64
    with store.db:
        store.db.execute(
            "INSERT INTO runtime VALUES(?,?)",
            ("historyblob:v1:" + key, encode({"bytes": context.MAX_BYTES + 1})),
        )

    def no_read(_):
        pytest.fail("oversized source must be rejected before reading")

    value = with_reference(frozen(), {"mode": "value2", "id": key})
    assert proposal(store, value, read=no_read)[0] is None
    value = with_reference(
        frozen(), {"mode": "source-cells", "stream": 1, "runs": [[NOW, 60000, 2**63 - 1, 0]]}
    )
    assert proposal(store, value, read=no_read)[0] is None


def test_original_normalized_window_sha_is_checked_before_a_price_candidate(store):
    from cryptowatcher.source_windows import load

    enable(store.db)
    value = frozen()
    with store.db:
        reference = window(store.db, "spot/FIXUSDT/1m", value.pop("source_inputs"), NOW)
    descriptor = load(store.db, reference["id"])
    descriptor["sha256"] = "0" * 64
    value = with_reference(value, descriptor)
    with pytest.raises(ValueError, match="checksum mismatch"):
        proposal(store, value)


def test_public_dictionary_is_not_interpreted_as_a_private_storage_reference(store):
    enable(store.db)
    value = {**frozen(), "source_inputs": {"mode": "source-ref", "id": 123456}}
    assert proposal(store, value)[0] is None
