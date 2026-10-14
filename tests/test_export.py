import csv
import hashlib
import io
import json
import sys
import time
from contextlib import closing

import pytest

from cryptowatcher.analytics import as_of, cohorts
from cryptowatcher.config import OwnerSettings
from cryptowatcher.events import History, identity
from cryptowatcher.export import Selection, csv_summary, ndjson
from cryptowatcher.history_rebuild import rebuild
from cryptowatcher.outcomes import MINUTE
from cryptowatcher.storage import Store

NOW = 1_800_000_000_000


def populate(store, count=125):
    history = History(store)
    with store.db:
        for i in range(count):
            event_id = identity(f"export-fixture/{i}")
            episode_id = identity(f"export-episode/{i}")
            event = {
                "event_id": event_id,
                "revision": 1,
                "episode_id": episode_id,
                "market_observation_ms": NOW + i * MINUTE,
                "available_at_ms": NOW + i * MINUTE,
                "reference_price": 100,
                "reference_time_ms": NOW + i * MINUTE,
                "detector_id": "price",
                "detector_version": "1",
                "settings_revision": 1,
                "settings_snapshot": store.snapshot().settings.payload(),
                "market_type": "spot",
                "timeframe": "1m",
                "symbol": "BTCUSDT",
                "direction": "UP",
                "strength": 2,
            }
            history.version("event", event)
            history.version("event", {**event, "revision": 2, "available_at_ms": NOW + DAY, "strength": 9})
            history.version("episode", {"episode_id": episode_id, "revision": 1, "available_at_ms": NOW})
            history.version(
                "outcome",
                {
                    "outcome_id": identity(f"outcome/{event_id}/5/1"),
                    "event_id": event_id,
                    "event_revision": 1,
                    "revision": 1,
                    "available_at_ms": NOW + DAY,
                    "state": "COMPLETE",
                    "horizon_min": 5,
                    "methodology_version": "1",
                    "classification": "CONTINUATION",
                    "metrics": {"price_return_pct": i + 1, "directed_return_pct": i + 1},
                },
            )
            history.version(
                "comparison",
                {
                    "comparison_id": identity(f"comparison/{event_id}/1"),
                    "event_id": event_id,
                    "revision": 1,
                    "available_at_ms": NOW + DAY,
                    "samples": [
                        {"outcomes": {"5": {"metrics": {"price_return_pct": -1, "directed_return_pct": -1}}}}
                    ],
                },
            )
        history.put("outbox:telegram:private", {"text": "SENTINEL_SECRET_DO_NOT_EXPORT"})
        store.db.execute(
            "INSERT INTO runtime VALUES (?,?)", ("TELEGRAM_BOT_TOKEN", '"SENTINEL_SECRET_DO_NOT_EXPORT"')
        )
    return history


DAY = 86_400_000


def test_paged_consistent_export_all_revisions_filters_and_no_transport(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, 233382575)
    store.initialize(OwnerSettings())
    history = populate(store)
    reader = Store(path, store.owner_id, readonly=True)
    export = ndjson(reader, Selection(NOW, NOW + 125 * MINUTE, "price", "1", "spot"))
    manifest = json.loads(next(export))
    assert manifest["selected_events"] == 125
    value = history.get("event-head:" + identity("export-fixture/0"))
    with store.db:
        history.version("event", {**value, "revision": 3, "available_at_ms": NOW + 2 * DAY})
    lines = list(export)
    records = [json.loads(line) for line in lines]
    events = [r["record"] for r in records if r["type"] == "event"]
    assert len(events) == 250 and max(e["revision"] for e in events) == 2
    assert records[-1]["counts"]["outcome"] == 125 and records[-1]["counts"]["episode"] == 125
    assert records[-1]["counts"]["comparison"] == 125
    assert "SENTINEL_SECRET_DO_NOT_EXPORT" not in "".join(lines)
    assert reader.db.in_transaction is False
    empty = [json.loads(line) for line in ndjson(reader, Selection(detector="breakout"))]
    assert empty[0]["selected_events"] == 0 and empty[-1]["counts"] == {}
    filtered = [json.loads(line) for line in ndjson(reader, Selection(NOW, NOW + MINUTE, market="spot"))]
    assert filtered[0]["selected_events"] == 1 and filtered[-1]["counts"]["event"] == 3
    with pytest.raises(ValueError):
        list(ndjson(store, Selection()))
    reader.close()


def test_known_cohort_distribution_comparison_cutoff_and_streaming_csv(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, 233382575)
    store.initialize(OwnerSettings())
    populate(store)
    result = cohorts(store)
    assert len(result["cohorts"]) == 1
    cohort = result["cohorts"][0]
    assert cohort["sample_size"] == 125 and cohort["mean"] == 63 and cohort["median"] == 63
    assert cohort["p10"] == 13 and cohort["p90"] == 113
    assert cohort["comparator_sample_size"] == 125 and cohort["comparator_mean"] == -1
    assert cohort["classifications"] == {"CONTINUATION": 125}
    assert cohorts(store, cutoff_ms=NOW)["cohorts"] == []
    assert cohorts(store, cutoff_ms=NOW + DAY)["cohorts"][0]["sample_size"] == 125
    reader = Store(path, store.owner_id, readonly=True)
    content = "".join(csv_summary(reader, Selection(NOW, NOW + 5 * MINUTE)))
    rows = list(csv.DictReader(io.StringIO(content)))
    assert len(rows) == 1 and rows[0]["sample_size"] == "5" and rows[0]["mean"] == "3.0"
    assert list(csv.DictReader(io.StringIO("".join(csv_summary(reader, Selection(market="linear")))))) == []
    assert reader.db.in_transaction is False
    reader.close()


@pytest.mark.parametrize("backend", ["python", "native"])
def test_converted_history_keeps_export_cohorts_causal_cutoff_and_private_bytes(
    tmp_path, backend, monkeypatch
):
    if backend == "native" and sys.platform != "linux":
        pytest.skip("Native converter is built and required in Linux CI")
    from cryptowatcher import export

    monkeypatch.setattr(export.time, "time", lambda: (NOW + 2 * DAY) / 1000)
    source = tmp_path / "source.sqlite3"
    original = Store(source, 233382575)
    original.initialize(OwnerSettings())
    history = populate(original)
    secret_key = "radar:v1:agent:outbox:fixture"
    private_bytes = '{ "immutable_transport":true, "value":1.00, "model_invoked":false }'
    with original.db:
        original.db.execute("INSERT INTO runtime VALUES (?,?)", (secret_key, private_bytes))
    selection = Selection(NOW, NOW + 125 * MINUTE, "price", "1", "spot")
    with closing(Store(source, original.owner_id, readonly=True)) as reader:
        before_export = list(ndjson(reader, selection))
        before_csv = list(csv_summary(reader, selection))
    before_cohorts = cohorts(original)
    event_id = identity("export-fixture/0")
    before_cutoff = as_of(history, "event", event_id, NOW + DAY - 1)
    assert before_cutoff["revision"] == 1 and before_cutoff["strength"] == 2
    original.db.execute("PRAGMA journal_mode=DELETE")
    original.close()
    source_bytes = source.read_bytes()
    destination = tmp_path / "converted.sqlite3"
    result = rebuild(
        source,
        destination,
        233382575,
        hashlib.sha256(source_bytes).hexdigest(),
        time.monotonic() + 20,
        backend=backend,
        compressed_candidate_validation=True,
    )
    assert result["candidate_validation"] == "COMPRESSED_RAM"
    with closing(Store(destination, 233382575, readonly=True)) as reader:
        assert list(ndjson(reader, selection)) == before_export
        assert list(csv_summary(reader, selection)) == before_csv
        assert cohorts(reader) == before_cohorts
        assert as_of(History(reader), "event", event_id, NOW + DAY - 1) == before_cutoff
        assert (
            reader.db.execute("SELECT payload_json FROM runtime WHERE key=?", (secret_key,)).fetchone()[0]
            == private_bytes
        )
        assert "immutable_transport" not in "".join(ndjson(reader, selection))
    assert source.read_bytes() == source_bytes
