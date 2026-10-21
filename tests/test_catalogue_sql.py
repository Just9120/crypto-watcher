from contextlib import closing

import pytest

from cryptowatcher import catalogue_sql
from cryptowatcher.events import PREFIX, History
from cryptowatcher.storage import Store, encode, json_payload
from tests.test_catalogue_store import mixed


def test_domain_sql_sources_match_checked_mixed_scan_without_duplicate_rows_and_share_a_snapshot(store):
    expected, _ = mixed(store)
    with closing(Store(store.path, store.owner_id, readonly=True)) as reader:
        reader.db.execute("BEGIN")
        for kind in ("event", "outcome", "episode", "comparison"):
            for head in (False, True):
                prefix = "radar:v1:" + kind + ("-head:" if head else ":")
                rows = list(reader.scan_runtime(prefix))
                sql = catalogue_sql.source(reader, kind, head=head)
                actual = [
                    (key, json_payload(raw))
                    for key, raw in reader.db.execute("SELECT key,payload_json FROM " + sql + " ORDER BY key")
                ]
                assert encode(actual) == encode(rows)
                assert len({key for key, _ in actual}) == len(actual)
                assert {key for key, _ in actual} == {key for key in expected if key.startswith(prefix)}
                plan = [row[3] for row in reader.db.execute("EXPLAIN QUERY PLAN SELECT key FROM " + sql)]
                assert any("SEARCH r USING PRIMARY KEY (kind=?)" in text for text in plan)
        assert reader.db.execute("SELECT cw_runtime_payload('missing')").fetchone()[0] is None
        key = next(key for key in expected if key.startswith(PREFIX + "event-head:"))
        old = reader.get_runtime(key)
        identity = key.removeprefix(PREFIX + "event-head:")
        newer = {**store.get_runtime(PREFIX + "event:" + identity + ":0000000002"), "revision": 3}
        with store.db:
            history = History(store)
            history.put(f"event:{identity}:0000000003", newer)
            history.put(
                "event-head:" + identity,
                History._head("event", newer, f"event:{identity}:0000000003", bounded=True),
            )
        assert reader.get_runtime(key) == old
        reader.db.execute("ROLLBACK")
        assert reader.get_runtime(key)["revision"] == 3
    for kind in ("decision", "future", "event' UNION SELECT secret"):
        with pytest.raises(ValueError, match="namespace"):
            catalogue_sql.source(store, kind)


def test_sql_source_is_plain_runtime_before_explicit_activation_and_open_creates_no_tables(store):
    before = list(store.db.execute("SELECT name FROM sqlite_master ORDER BY name"))
    assert catalogue_sql.source(store, "event", head=True) == "runtime"
    assert list(store.db.execute("SELECT name FROM sqlite_master ORDER BY name")) == before
