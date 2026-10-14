import hashlib
import json
import os
import time

import pytest

from cryptowatcher import compressed_validation as cv
from cryptowatcher.frame_archive import decoded_blocks, restore, save_index, verify
from cryptowatcher.storage import StateError


def archive(tmp_path):
    backing = (tmp_path / "source.frames").resolve()
    if not cv.disk_backing(backing):
        pytest.skip("Recovery archive requires a disk filesystem")
    deadline = time.monotonic() + 15
    image = cv.Image(None, limit=2 * 1024**2, deadline=deadline, backing=backing)
    raw = b"original-history" * 6000
    image.write(raw, 0)
    image.freeze()
    descriptor = save_index(image, tmp_path.resolve(), deadline=deadline)
    assert backing.stat().st_size == (image.blocks.limit if os.name == "nt" else image.blocks.end)
    assert image.reconstructed_hash() == hashlib.sha256(raw).hexdigest()
    image.blocks.close()
    return descriptor, raw


def test_independent_recovery_decodes_every_source_byte(tmp_path):
    descriptor, original = archive(tmp_path)
    deadline = time.monotonic() + 15
    proof = verify(descriptor, deadline=deadline)
    assert proof["state"] == "RECOVERY_BYTES_VERIFIED"
    assert proof["raw_sha256"] == hashlib.sha256(original).hexdigest()
    assert b"".join(decoded_blocks(descriptor, deadline=deadline)) == original


@pytest.mark.parametrize(
    "fault",
    [
        "encoded",
        "index",
        "raw_hash",
        "size",
        "path",
        "extra",
        "future",
        "invalid_size",
        "invalid_hash",
        "invalid_block",
        "unproved_integrity",
    ],
)
def test_recovery_corruption_or_unknown_layout_refuses(tmp_path, fault):
    descriptor, _ = archive(tmp_path)
    record = json.loads(descriptor.read_text())
    if fault == "encoded":
        data = tmp_path / record["data"]
        with data.open("r+b") as stream:
            stream.seek(0)
            stream.write(b"broken")
    elif fault == "index":
        index = tmp_path / record["index"]
        with index.open("r+b") as stream:
            stream.write(b"broken")
    elif fault == "raw_hash":
        record["raw_sha256"] = "0" * 64
    elif fault == "size":
        record["data_bytes"] += 1
    elif fault == "path":
        record["data"] = "../outside"
    elif fault == "extra":
        record["extra"] = True
    elif fault == "future":
        record["schema"] = "future"
    elif fault == "invalid_size":
        record["data_bytes"] = "100"
    elif fault == "invalid_hash":
        record["data_sha256"] = 42
    elif fault == "invalid_block":
        record["block_bytes"] = True
    else:
        record["source_full_integrity"] = "PASS"
    descriptor.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(StateError):
        verify(descriptor, deadline=time.monotonic() + 15)


def test_restore_preserves_bytes_and_refuses_overwrite_capacity_or_expired_budget(tmp_path):
    descriptor, raw = archive(tmp_path)
    restored = (tmp_path / "restored.sqlite3").resolve()
    proof = restore(descriptor, restored, maximum=len(raw), deadline=time.monotonic() + 15)
    assert proof["state"] == "RECOVERY_FILE_VERIFIED"
    assert restored.read_bytes() == raw
    with pytest.raises(StateError, match="Exclusive"):
        restore(descriptor, restored, maximum=len(raw), deadline=time.monotonic() + 15)
    empty = (tmp_path / "too-small").resolve()
    with pytest.raises(StateError, match="capacity"):
        restore(descriptor, empty, maximum=len(raw) - 1, deadline=time.monotonic() + 15)
    assert not empty.exists()
    with pytest.raises(StateError):
        restore(descriptor, empty, maximum=len(raw), deadline=time.monotonic() - 1)
    assert not empty.exists()
