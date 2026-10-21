"""Native64-bit frame offsets on an owned sparse file, not a size/performance claim."""

import mmap
import sqlite3
import time
from array import array
from contextlib import closing

import apsw
import pytest
import zstandard

from cryptowatcher.compressed_validation import Image, QualificationError
from cryptowatcher.native_validation import NativeVFS, available


@pytest.mark.skipif(not available(), reason="Required Linux/GCC VFS; no native Windows VFS")
def test_native_offset_beyond_four_gib_and_exact_borrow_release(tmp_path):
    raw = tmp_path / "source.db"
    with closing(sqlite3.connect(raw)) as database:
        database.execute("CREATE TABLE proof(value INTEGER CHECK(value>0))")
        database.execute("INSERT INTO proof VALUES(7)")
        database.commit()
    payload = raw.read_bytes()
    assert len(payload) <= 65536
    encoded = zstandard.ZstdCompressor(write_checksum=True).compress(payload)
    offset = 4 * 1024**3 + 4096
    sparse = tmp_path / "owned-sparse.bin"
    with sparse.open("xb") as output:
        output.seek(offset)
        output.write(encoded)
    image = Image(None, limit=1024**2, deadline=time.monotonic() + 20)
    image.blocks.data.close()
    with sparse.open("r+b") as backing:
        data = mmap.mmap(backing.fileno(), 0, access=mmap.ACCESS_COPY)
        try:

            class Frames:
                def __init__(self):
                    self.data = data
                    self.end = sparse.stat().st_size
                    self.offsets = array("Q", [offset])
                    self.lengths = array("Q", [len(encoded)])

                def __len__(self):
                    return 1

            image.blocks = Frames()
            image.mutable, image.size = False, len(payload)
            sentinel = tmp_path / "sentinel.db"
            sentinel.touch()
            vfs = NativeVFS(image, sentinel)
            try:
                with closing(
                    apsw.Connection(str(sentinel), flags=apsw.SQLITE_OPEN_READWRITE, vfs=vfs.name)
                ) as database:
                    database.execute("PRAGMA query_only=ON")
                    assert list(database.execute("PRAGMA integrity_check")) == [("ok",)]
                    assert list(database.execute("SELECT value FROM proof")) == [(7,)]
            finally:
                vfs.unregister()
            image.blocks.offsets[0] = sparse.stat().st_size + 1
            with pytest.raises(QualificationError, match="NATIVE_VFS_REGISTRATION_REFUSED"):
                NativeVFS(image, sentinel)
        finally:
            # A surviving exported borrow would raise BufferError here.
            data.close()
