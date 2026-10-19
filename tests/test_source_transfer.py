import sqlite3
import time
from contextlib import closing

import pytest

from cryptowatcher import candle_blocks as blocks
from cryptowatcher import market_storage as market
from cryptowatcher.source_cells import convert
from cryptowatcher.source_transfer import advance
from cryptowatcher.storage import Store, encode
from tests.test_bounded_storage import bounded
from tests.test_candle_block_codec import fixture


def prepare(store, tiles=4):
    bounded(store)
    tile, slots = fixture()
    from cryptowatcher.candle_block_codec import public

    base = public(tile, 60000, 0, slots[0][0][3])
    values = [
        {**base, "start": tile + i * 32 * 60000, "server_ms": tile + 1000 * 60000} for i in range(tiles)
    ]
    with store.db:
        points = market.admit_many(store.db, "spot/BTCUSDT/1m", values)
    blocks.install(store.db, store.cached_text)
    return tile, values, points


def tick(store, batch=1):
    state = store.get_runtime("source_test_checkpoint", {})
    with store.db:
        store.db.execute("BEGIN")
        advance(store.db, state, batch, time.monotonic() + 5)
        store.put_runtime("source_test_checkpoint", state)
    return state


def test_finite_source_logical_ceiling_survives_restart_ddl_vacuum_and_ongoing_admission(store):
    tile, values, points = prepare(store)
    expected = encode(market.read(store.db, "spot/BTCUSDT/1m"))
    state = tick(store)
    high = state["source_upper"]
    cursor = state["source_cursor"]
    assert state["source_rows_moved"] == 1
    with store.db:
        store.db.execute("INSERT INTO market_meta VALUES('candle_block_runtime','1')")
        market.admit(store.db, "spot/BTCUSDT/1m", {**values[0], "start": tile + 10 * 32 * 60000})
    store.db.execute("CREATE TABLE unrelated_fixture(value INTEGER)")
    store.db.execute("VACUUM")
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert reopened.get_runtime("source_test_checkpoint")["source_cursor"] == cursor
        for _ in range(5):
            state = tick(reopened)
            assert state["source_upper"] == high
        assert state["source_transfer_complete"]
        assert state["source_rows_moved"] == 4
        assert state["source_tiles_examined"] == 4
        assert (
            encode(market.read(reopened.db, "spot/BTCUSDT/1m", end=values[-1]["start"] + 60000)) == expected
        )
        assert blocks.summary(reopened.db)["versions"] == 5
        assert reopened.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 0
        assert high == [points[-1][0], points[-1][1]]


def test_source_transfer_and_checkpoint_rollback_together_without_extending_end(store, monkeypatch):
    prepare(store)
    state = tick(store)
    before = list(store.db.iterdump())
    original = store.put_runtime

    def interrupted(key, value):
        if key == "source_test_checkpoint" and value.get("source_rows_moved", 0) > 1:
            raise RuntimeError("checkpoint interrupted")
        return original(key, value)

    monkeypatch.setattr(store, "put_runtime", interrupted)
    with pytest.raises(RuntimeError, match="checkpoint interrupted"):
        tick(store)
    assert store.get_runtime("source_test_checkpoint") == state
    assert list(store.db.iterdump()) == before
    monkeypatch.setattr(store, "put_runtime", original)
    assert tick(store)["source_rows_moved"] == 2
    with pytest.raises(ValueError, match="outer checkpoint"):
        advance(store.db, {}, 1, time.monotonic() + 5)


def test_finite_source_pass_records_large_correction_fallback_and_moves_later_tiles(store):
    tile, values, points = prepare(store, tiles=2)
    with store.db:
        market.admit_many(
            store.db, "spot/BTCUSDT/1m", [{**values[0], "close": 100.5 + i / 1000} for i in range(257)]
        )
    for _ in range(3):
        state = tick(store)
    assert state["source_transfer_complete"]
    assert state["source_rows_moved"] == 1
    assert state["source_fallbacks_observed"] == 1
    assert blocks.summary(store.db)["fallback_tiles"] == 1
    assert store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 258
    assert encode(market.read(store.db, "spot/BTCUSDT/1m")) == encode(
        [{**values[0], "close": 100.756}, values[1]]
    )
    assert points[0][0] == state["source_upper"][0]


def test_cells_require_full_old_hash_then_preserve_order_duplicates_old_revisions_and_types(store):
    tile, values, points = prepare(store, tiles=2)
    frozen = [values[1], values[0], values[1], {**values[0], "close": 102.0}]
    with store.db:
        ref = market.window(store.db, "spot/BTCUSDT/1m", frozen, tile)
    assert convert(store.db, ref, cache=store.cached_bytes) is None
    for _ in range(3):
        tick(store)
    cells = convert(store.db, ref, cache=store.cached_bytes)
    assert cells["mode"] == "source-cells" and "sha256" not in cells and "id" not in cells
    assert market.restore_window(store.db, cells, cache=store.cached_bytes) == encode(frozen).encode()
    from cryptowatcher.history_codec import Codec
    from cryptowatcher.source_windows import load

    codec = Codec(store.db, binary_cache=store.cached_bytes)
    assert codec.field_raw(cells) == encode(frozen).encode()
    original = load(store.db, ref["id"])
    with pytest.raises(ValueError, match="hash mismatch"):
        convert(store.db, {**original, "sha256": "0" * 64}, cache=store.cached_bytes)
    with pytest.raises(ValueError, match="Missing retained candle"):
        convert(store.db, {**original, "runs": [[tile, 0, 1, 1000]]})
    # A descriptor may expire after its last owner; the authenticated cells do
    # not depend on the descriptor's ID or retain an unbounded metadata row.
    with store.db:
        store.db.execute("DELETE FROM market_source_windows WHERE id=?", (ref["id"],))
    assert market.restore_window(store.db, cells, cache=store.cached_bytes) == encode(frozen).encode()
    assert cells["stream"] == points[0][0]


def test_missing_cold_cell_does_not_use_unhashed_legacy_fallback_and_gc_keeps_converted_lease(store):
    tile, values, _ = prepare(store, tiles=1)
    with store.db:
        ref = market.window(store.db, "spot/BTCUSDT/1m", values, tile + 40 * 86400000)
    tick(store)
    cells = convert(store.db, ref)
    assert cells is not None
    with store.db:
        assert blocks.gc(store.db, tile + 60 * 86400000, seconds=5) == 0
    assert market.restore_window(store.db, cells) == encode(values).encode()
    with store.db:
        assert blocks.gc(store.db, tile + 70 * 86400000, seconds=5) == 1
        # Reintroducing a value through the previous legacy writer cannot
        # substitute for the authenticated dependency of a source-cells ref.
        market.admit(store.db, "spot/BTCUSDT/1m", values[0])
    with pytest.raises(ValueError, match="Missing authenticated"):
        market.restore_window(store.db, cells)


def test_cell_owners_keep_a_full_record_checksum_and_release_only_descriptor_ownership(store):
    from cryptowatcher.events import History
    from cryptowatcher.history_codec import MARKER, SCHEMA
    from cryptowatcher.storage import StateError

    _, values, _ = prepare(store, tiles=1)
    with store.db:
        ref = market.window(store.db, "spot/BTCUSDT/1m", values, values[0]["start"])
        old = {MARKER: {"schema": SCHEMA, "fields": {"source_inputs": ref}}}
        History(store).put("event:checked:0000000001", old)
    tick(store)
    cells = convert(store.db, ref)
    with store.db:
        History(store).put(
            "event:checked:0000000001", {MARKER: {"schema": SCHEMA, "fields": {"source_inputs": cells}}}
        )
    raw = store.db.execute(
        "SELECT payload_json FROM runtime WHERE key='radar:v1:event:checked:0000000001'"
    ).fetchone()[0]
    assert isinstance(raw, bytes)
    assert encode(History(store).get("event:checked:0000000001")) == encode({"source_inputs": values})
    assert (
        store.db.execute("SELECT refs FROM market_source_windows WHERE id=?", (ref["id"],)).fetchone()[0] == 0
    )
    corrupted = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(sqlite3.OperationalError), store.db:
        store.db.execute(
            "UPDATE runtime SET payload_json=? WHERE key='radar:v1:event:checked:0000000001'",
            (corrupted,),
        )
    with pytest.raises(StateError):
        History(store).codec.read(corrupted)
    assert encode(History(store).get("event:checked:0000000001")) == encode({"source_inputs": values})


def test_repack10_selects_source_adapters_only_in_explicit_work_and_is_restartable_and_idempotent(
    store, tmp_path
):
    from unittest.mock import patch

    from cryptowatcher.events import History
    from cryptowatcher.export import Selection, csv_summary, ndjson
    from cryptowatcher.history_codec import MARKER
    from cryptowatcher.outcomes import OutcomeWorker
    from cryptowatcher.sources import Sources
    from cryptowatcher.storage_repack import work
    from tests.test_outcomes import MINUTE, NOW, future, observe

    bounded(store)
    worker = OutcomeWorker(store)
    observe(store, worker)
    Sources(store).put("spot/BTCUSDT/1m", future())
    worker.work(NOW + 6 * MINUTE)
    original = encode(History(store).items("event:") + History(store).items("outcome:"))
    source_count = store.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0]

    def exported(path):
        with (
            closing(Store(path, store.owner_id, readonly=True)) as reader,
            patch("cryptowatcher.export.time.time", return_value=NOW / 1000),
        ):
            return list(ndjson(reader, Selection())), list(csv_summary(reader))

    original_export = exported(store.path)
    store.put_runtime("storage_repack", {"version": 9, "state": "COMPLETE"})
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert (
            reopened.db.execute("SELECT value FROM market_meta WHERE key='candle_blocks'").fetchone() is None
        )
        for tick in range(200):
            state = work(reopened, NOW + tick, batch=1, seconds=1)
            if state["state"] == "COMPLETE":
                break
        assert state["state"] == "COMPLETE" and state["version"] == 10
        assert state["previous_version"] == 9 and state["source_transfer_complete"]
        assert state["source_rows_moved"] == source_count
        assert reopened.db.execute("SELECT COUNT(*) FROM market_candles").fetchone()[0] == 0
        assert encode(History(reopened).items("event:") + History(reopened).items("outcome:")) == original
        event_key = History(reopened).items("event:")[0][0]
        stored = reopened.get_runtime("radar:v1:" + event_key)
        assert stored[MARKER]["fields"]["source_inputs"]["mode"] == "source-cells"
        assert work(reopened, NOW + 1000) == state
        assert exported(reopened.path) == original_export
        backup = tmp_path / "source-tile-backup.sqlite3"
        assert reopened.backup(backup)["phase"] == "COMPLETE"
        assert exported(backup) == original_export
    with closing(Store(store.path, store.owner_id)) as reopened:
        assert work(reopened, NOW + 2000) == state
        assert encode(History(reopened).items("event:") + History(reopened).items("outcome:")) == original
