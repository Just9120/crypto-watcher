"""Source advice must never weaken SQLite snapshots or descriptor lifetime."""

import os
import time
from contextlib import closing

import apsw
import pytest

from cryptowatcher import compressed_validation as cv
from cryptowatcher import framed_backup
from cryptowatcher import validation_io as vio


def fake_advice(monkeypatch):
    calls = []
    monkeypatch.setattr(vio, "_supported", lambda: True)
    monkeypatch.setattr(vio, "_readonly_handle", lambda path: (51, (9, 7)))
    monkeypatch.setattr(vio.os, "fstat", lambda fd: type("Stat", (), {"st_dev": 9, "st_ino": 7})())
    for name, value in (("NORMAL", 0), ("SEQUENTIAL", 2), ("NOREUSE", 5)):
        monkeypatch.setattr(vio.os, "POSIX_FADV_" + name, value, raising=False)
    monkeypatch.setattr(vio.os, "posix_fadvise", lambda *args: calls.append(args), raising=False)
    return calls


def pinned(store):
    reader = apsw.Connection(
        store.path.resolve().as_uri() + "?mode=ro", flags=apsw.SQLITE_OPEN_URI | apsw.SQLITE_OPEN_READONLY
    )
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM owner_settings").fetchone()
    return reader


@pytest.mark.parametrize("failed", [False, True])
def test_restores_before_source_close_and_preserves_first_backup_error(store, monkeypatch, failed):
    calls = fake_advice(monkeypatch)
    with closing(pinned(store)) as reader:
        original = list(reader.execute("SELECT * FROM owner_settings"))
        try:
            with vio.source_scan(reader, store.path.resolve()) as status:
                assert status["policy"] == "SEQUENTIAL_NOREUSE"
                assert reader.txn_state("main") == apsw.SQLITE_TXN_READ
                if failed:
                    raise RuntimeError("original-backup-failure")
        except RuntimeError as error:
            assert failed and str(error) == "original-backup-failure"
        assert calls == [(51, 0, 0, 2), (51, 0, 0, 5), (51, 0, 0, 0)]
        assert status["restored"] is True
        assert list(reader.execute("SELECT * FROM owner_settings")) == original


@pytest.mark.parametrize("mode", ["unsupported", "ambiguous", "advice_error", "restore_error", "reused"])
def test_unavailable_and_cleanup_paths_do_not_mask_failure_or_touch_another_handle(store, monkeypatch, mode):
    calls = fake_advice(monkeypatch)
    if mode == "unsupported":
        monkeypatch.setattr(vio, "_supported", lambda: False)
    elif mode == "ambiguous":
        monkeypatch.setattr(vio, "_readonly_handle", lambda path: None)
    elif mode in ("advice_error", "restore_error"):

        def advise(*args):
            calls.append(args)
            if (mode == "advice_error" and args[-1] == 5) or (mode == "restore_error" and args[-1] == 0):
                raise OSError("unavailable")

        monkeypatch.setattr(vio.os, "posix_fadvise", advise)
    with closing(pinned(store)) as reader:
        with pytest.raises(RuntimeError, match="first-cause"):
            with vio.source_scan(reader, store.path.resolve()) as status:
                if mode == "reused":
                    monkeypatch.setattr(
                        vio.os, "fstat", lambda fd: type("Stat", (), {"st_dev": 8, "st_ino": 7})()
                    )
                raise RuntimeError("first-cause")
        if mode in ("unsupported", "ambiguous"):
            assert calls == [] and status["policy"] == "DEFAULT"
        elif mode == "advice_error":
            assert status == {"policy": "DEFAULT", "reason": "ADVICE_UNAVAILABLE", "restored": True}
        elif mode == "reused":
            assert calls[-1][-1] == 5 and status["restored"] is False
        else:
            assert status["restored"] is False


def test_writable_or_unpinned_connection_never_receives_advice(store, monkeypatch):
    calls = fake_advice(monkeypatch)
    with closing(apsw.Connection(str(store.path))) as writer:
        with vio.source_scan(writer, store.path.resolve()) as status:
            assert status["policy"] == "DEFAULT"
    with closing(pinned(store)) as reader:
        reader.execute("ROLLBACK")
        with vio.source_scan(reader, store.path.resolve()) as status:
            assert status["policy"] == "DEFAULT"
    assert calls == []


def test_expensive_qualification_selected_only_for_its_inputs():
    from scripts.impact import backup_io_required

    assert backup_io_required([])
    for path in (
        "cryptowatcher/validation_io.py",
        "native/compressed_vfs.c",
        "requirements.txt",
        "scripts/impact.py",
        ".github/workflows/ci.yml",
    ):
        assert backup_io_required([path])
    assert not backup_io_required(["docs/open_issues.md", "cryptowatcher/bot.py"])


@pytest.mark.skipif(os.name != "posix", reason="Linux kernel descriptor/advice integration")
def test_real_linux_readonly_handle_and_pinned_wal_with_concurrent_writer(store):
    if not vio._supported():
        pytest.skip("Linux 6.3+ advice required")
    # Close the fixture's RW connection before opening the advice scope: a
    # second connection to the inode intentionally makes the hint ambiguous.
    path = store.path.resolve()
    before = store.snapshot()
    store.close()
    with closing(
        apsw.Connection(path.as_uri() + "?mode=ro", flags=apsw.SQLITE_OPEN_URI | apsw.SQLITE_OPEN_READONLY)
    ) as reader:
        reader.execute("BEGIN")
        original = list(reader.execute("SELECT * FROM owner_settings"))
        with vio.source_scan(reader, path) as status:
            assert status["policy"] == "SEQUENTIAL_NOREUSE"
            with closing(apsw.Connection(str(path))) as writer:
                writer.execute("UPDATE owner_settings SET revision=revision+1")
                writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            assert list(reader.execute("SELECT * FROM owner_settings")) == original
        assert status["restored"] is True
        assert original[0][1] == before.revision


def test_framed_backup_restores_hint_before_seal_and_keeps_full_exact_restore(store, tmp_path, monkeypatch):
    from tests.test_framed_backup import environment

    real_fstat = os.fstat
    calls = fake_advice(monkeypatch)
    original = cv.Image.freeze

    def freeze(image):
        assert calls[-1] == (51, 0, 0, 0)
        return original(image)

    monkeypatch.setattr(cv.Image, "freeze", freeze)
    # Fake fstat is limited to advice; ArchiveArena itself uses real fstat.
    monkeypatch.setattr(
        vio.os,
        "fstat",
        lambda fd: type("Stat", (), {"st_dev": 9, "st_ino": 7})() if fd == 51 else real_fstat(fd),
    )
    scratch, destination = environment(tmp_path, monkeypatch)
    result = framed_backup.backup(
        store.path.resolve(),
        destination.resolve(),
        scratch,
        owner=store.owner_id,
        deadline=time.monotonic() + 20,
    )
    assert result["source_io"] == {"policy": "SEQUENTIAL_NOREUSE", "reason": "APPLIED", "restored": True}
    assert result["phase"] == "COMPLETE"
