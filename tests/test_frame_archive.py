import hashlib
import json
import time

import pytest

from cryptowatcher import compressed_validation as cv
from cryptowatcher.frame_archive import decoded_blocks, save_index, verify
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
    image.blocks.close()
    return descriptor, raw


def test_independent_recovery_decodes_every_source_byte(tmp_path):
    descriptor, original = archive(tmp_path)
    deadline = time.monotonic() + 15
    proof = verify(descriptor, deadline=deadline)
    assert proof["state"] == "RECOVERY_BYTES_VERIFIED"
    assert proof["raw_sha256"] == hashlib.sha256(original).hexdigest()
    assert b"".join(decoded_blocks(descriptor, deadline=deadline)) == original


@pytest.mark.parametrize("fault", ["encoded", "index", "raw_hash", "size", "path", "extra", "future"])
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
    else:
        record["schema"] = "future"
    descriptor.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(StateError):
        verify(descriptor, deadline=time.monotonic() + 15)
