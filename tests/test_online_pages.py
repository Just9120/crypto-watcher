import hashlib
import io
import json
import sqlite3
import time
from contextlib import closing

import pytest
import zstandard

from cryptowatcher import change_journal, online_pages
from cryptowatcher.page_delta import PACKET
from cryptowatcher.storage import StateError

EPOCH = "a" * 32


def prepared(store, tmp_path):
    base = (tmp_path / "base.sqlite3").resolve()
    store.backup(base)
    directory = (tmp_path / "base-index").resolve()
    online_pages.make_base(base, directory, expected_sha256=hashlib.sha256(base.read_bytes()).hexdigest())
    change_journal.install(
        store.db,
        epoch=EPOCH,
        owner=store.owner_id,
        max_keys=10000,
        max_bytes=1024**2,
        expires=int(time.time()) + 3600,
    )
    store.put_runtime("included", {"useful": "history" * 10000})
    descriptor = directory / "base.json"
    output = (tmp_path / "out").resolve()
    output.mkdir(mode=0o700)
    return (
        base,
        output,
        dict(
            source=store.path.resolve(),
            baseline=descriptor,
            directory=output,
            expected_baseline_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
            epoch=EPOCH,
            owner=store.owner_id,
            maximum=8 * 1024**2,
        ),
    )


def reconstruct(base, encoded, result):
    value = bytearray(base.read_bytes())
    if len(value) < result["raw_bytes"]:
        value.extend(bytes(result["raw_bytes"] - len(value)))
    stream = io.BytesIO(encoded)
    packets = 0
    while raw := stream.read(PACKET.size):
        page, n, length = PACKET.unpack(raw)
        assert n == 4096
        block = zstandard.ZstdDecompressor().decompress(
            stream.read(length), max_output_size=4096, allow_extra_data=False
        )
        assert len(block) == 4096
        value[page * 4096 : (page + 1) * 4096] = block
        packets += 1
    del value[result["raw_bytes"] :]
    root = hashlib.sha256(
        b"".join(hashlib.sha256(value[i : i + 4096]).digest() for i in range(0, len(value), 4096))
    ).hexdigest()
    assert root == result["page_root_sha256"]
    assert packets == result["packets"]
    assert hashlib.sha256(encoded).hexdigest() == result["data_sha256"]
    return value


def test_online_capture_pins_wal_snapshot_and_does_not_stop_later_writes(store, tmp_path):
    base, output, args = prepared(store, tmp_path)
    stream = io.BytesIO()
    written = False

    def progress(**fields):
        nonlocal written
        if not written:
            store.put_runtime("after-snapshot", {"still_running": True})
            store.put_runtime("included", {"later": True})
            written = True

    result = online_pages.capture(output=stream, progress=progress, **args)
    assert written and result["application_stopped"] is False
    assert result["full_source_integrity"] == "PENDING"
    assert not (output / "pages.delta").exists()
    assert store.get_runtime("after-snapshot") == {"still_running": True}
    current = (tmp_path / "reconstructed.sqlite3").resolve()
    current.write_bytes(reconstruct(base, stream.getvalue(), result))
    with closing(sqlite3.connect(current)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("SELECT payload_json FROM runtime WHERE key='after-snapshot'").fetchone() is None
        assert json.loads(
            db.execute("SELECT payload_json FROM runtime WHERE key='included'").fetchone()[0]
        ) == {"useful": "history" * 10000}
    with change_journal.changes(
        store.db,
        epoch=EPOCH,
        owner=store.owner_id,
        after=result["journal_watermark"],
        deadline=time.monotonic() + 10,
    ) as (_, rows):
        assert {row["key"] for row in rows} == {"after-snapshot", "included"}


@pytest.mark.parametrize("fault", ["capacity", "broken_pipe", "baseline"])
def test_failed_stream_has_no_success_footer_and_app_continues(store, tmp_path, fault):
    _, output, args = prepared(store, tmp_path)

    class Broken(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError("receiver disconnected")

    stream = Broken() if fault == "broken_pipe" else io.BytesIO()
    if fault == "capacity":
        args["maximum"] = 1
    if fault == "baseline":
        args["expected_baseline_sha256"] = "0" * 64
    with pytest.raises((StateError, BrokenPipeError)):
        online_pages.capture(output=stream, **args)
    assert not (output / "snapshot.json").exists()
    store.put_runtime("after-refusal", 1)
    assert store.get_runtime("after-refusal") == 1
    assert store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0


def test_repeated_page_reverting_to_baseline_is_transmitted(tmp_path):
    base = hashlib.sha256(b"a" * 4096).digest()
    stream = io.BytesIO()
    image = online_pages.StreamImage(
        stream,
        base,
        1,
        maximum=1024**2,
        deadline=time.monotonic() + 10,
        directory=tmp_path.resolve(),
        progress=lambda **kw: None,
    )
    image.write(b"b" * 4096, 0)
    image.write(b"a" * 4096, 0)
    result = image.finish()
    assert result["packets"] == 2
    assert result["page_root_sha256"] == hashlib.sha256(base).hexdigest()


def test_unexpected_read_or_omitted_page_fails_closed(tmp_path):
    image = online_pages.StreamImage(
        io.BytesIO(),
        b"",
        2,
        maximum=1024**2,
        deadline=time.monotonic() + 10,
        directory=tmp_path.resolve(),
        progress=lambda **kw: None,
    )
    with pytest.raises(StateError, match="destination read"):
        image.read(4096, 4096)
    image.write(bytes(4096), 0)
    with pytest.raises(StateError, match="omitted"):
        image.finish()


def stored_stream(store, tmp_path):
    base, output, args = prepared(store, tmp_path)
    stream = io.BytesIO()
    result = online_pages.capture(output=stream, **args)
    (output / "pages.delta").write_bytes(stream.getvalue())
    work = (tmp_path / "working.sqlite3").resolve()
    work.write_bytes(base.read_bytes())
    return work, output / "snapshot.json", result


def test_pc_reconstruction_verifies_every_page_and_real_sqlite_integrity(store, tmp_path):
    work, descriptor, result = stored_stream(store, tmp_path)
    proof = online_pages.apply_to_working_copy(
        work, descriptor, expected_descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest()
    )
    assert (
        proof["page_root_sha256"] == result["page_root_sha256"]
        and proof["full_source_integrity"] == "PENDING"
    )
    with closing(sqlite3.connect(work)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("SELECT key FROM runtime WHERE key='included'").fetchone() == ("included",)


def test_interrupted_pc_replay_keeps_binding_and_resumes_without_source_loss(store, tmp_path, monkeypatch):
    work, descriptor, result = stored_stream(store, tmp_path)
    expected = hashlib.sha256(descriptor.read_bytes()).hexdigest()
    original = online_pages.zstandard.ZstdDecompressor
    calls = 0

    class Interrupted:
        def __init__(self, *args, **kw):
            self.real = original(*args, **kw)

        def decompress(self, *args, **kw):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("worker interrupted")
            return self.real.decompress(*args, **kw)

    monkeypatch.setattr(online_pages.zstandard, "ZstdDecompressor", Interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        online_pages.apply_to_working_copy(work, descriptor, expected_descriptor_sha256=expected)
    assert work.with_name(work.name + ".online-pages.json").is_file()
    monkeypatch.setattr(online_pages.zstandard, "ZstdDecompressor", original)
    proof = online_pages.apply_to_working_copy(
        work, descriptor, expected_descriptor_sha256=expected, resume=True
    )
    assert proof["page_root_sha256"] == result["page_root_sha256"]
    assert store.get_runtime("included") == {"useful": "history" * 10000}


def test_corrupt_packet_stream_refused_before_pc_mutation(store, tmp_path):
    work, descriptor, _ = stored_stream(store, tmp_path)
    before = work.read_bytes()
    data = descriptor.parent / "pages.delta"
    bad = bytearray(data.read_bytes())
    bad[-1] ^= 1
    data.write_bytes(bad)
    with pytest.raises(StateError, match="stream identity"):
        online_pages.apply_to_working_copy(
            work, descriptor, expected_descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest()
        )
    assert work.read_bytes() == before
