"""``relocate_session_file``: moving a durable conversation to a new working directory without
losing or overwriting any of it. The real-omp resume of a migrated file is in
``test_omp_xd_guard.py``."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from annealage_ember.session import omp_session_file as module
from annealage_ember.session.omp_session_file import (
    SessionFileError,
    relocate_session_file,
    rollback,
)

TITLE = {
    "type": "title",
    "v": 1,
    "title": "",
    "updatedAt": "2026-01-01T00:00:00.000Z",
    "pad": " " * 40,
}
HEADER = {
    "type": "session",
    "version": 3,
    "id": "01a1bcff-23d0-721e-a922-e8707eae2c5e",
    "timestamp": "2026-01-01T00:00:00.000Z",
    "cwd": "/old/project",
    "providerPromptCacheKey": "cache-key-é",
}
ENTRIES = [
    {
        "type": "message",
        "id": "a",
        "parentId": None,
        "message": {"role": "user", "content": "héllo"},
    },
    {
        "type": "message",
        "id": "b",
        "parentId": "a",
        "message": {"role": "assistant", "content": "hi"},
    },
]


def _write(path, *, title=True, header=None, entries=ENTRIES):
    lines = ([TITLE] if title else []) + [header or HEADER] + list(entries)
    path.write_bytes(b"".join(json.dumps(x, ensure_ascii=False).encode() + b"\n" for x in lines))
    return path.read_bytes()


def _header(path):
    for line in Path(path).read_bytes().splitlines():
        value = json.loads(line)
        if value["type"] == "session":
            return value
    raise AssertionError("no header")


@pytest.mark.parametrize("title", (True, False))
def test_only_the_cwd_changes_and_the_original_is_kept(tmp_path, title):
    path = tmp_path / "s.jsonl"
    original = _write(path, title=title)
    target = tmp_path / "private"
    target.mkdir()
    migration = relocate_session_file(path, target)
    assert migration is not None and migration.previous_cwd == "/old/project"
    header = _header(path)
    assert header["cwd"] == str(target.resolve())
    assert {k: v for k, v in header.items() if k != "cwd"} == {
        k: v for k, v in HEADER.items() if k != "cwd"
    }
    # Every other line, byte for byte, in the same order.
    old_lines, new_lines = original.splitlines(True), path.read_bytes().splitlines(True)
    index = 1 if title else 0
    assert new_lines[:index] == old_lines[:index]
    assert new_lines[index + 1 :] == old_lines[index + 1 :]
    assert migration.backup.read_bytes() == original
    assert migration.backup.name.startswith("s.jsonl.cwd-backup-")
    assert (tmp_path / "s.jsonl.cwd-original").read_bytes() == original


def test_a_conversation_already_in_the_directory_is_left_alone(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path)
    target = tmp_path / "private"
    target.mkdir()
    assert relocate_session_file(path, target) is not None
    before = path.read_bytes()
    assert relocate_session_file(path, target) is None
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "case",
    (
        "version",
        "no_id",
        "no_cwd",
        "extra_dirs",
        "not_json",
        "no_header",
        "oversized",
        "symlink",
        "hardlink",
    ),
)
def test_a_file_it_does_not_know_is_refused_and_untouched(tmp_path, case):
    path = tmp_path / "s.jsonl"
    header = dict(HEADER)
    if case == "version":
        header["version"] = 4
    elif case == "no_id":
        del header["id"]
    elif case == "no_cwd":
        del header["cwd"]
    elif case == "extra_dirs":
        header["additionalDirectories"] = ["/elsewhere"]
    original = _write(path, header=header)
    if case == "not_json":
        original = b"not json\n" + original
        path.write_bytes(original)
    elif case == "no_header":
        original = json.dumps(ENTRIES[0]).encode() + b"\n"
        path.write_bytes(original)
    elif case == "oversized":
        original = b'{"type":"session","pad":"' + b"x" * (module._MAX_LINE + 1) + b'"}\n'
        path.write_bytes(original)
    elif case == "symlink":
        real = tmp_path / "real.jsonl"
        path.rename(real)
        path.symlink_to(real)
        original = real.read_bytes()
    elif case == "hardlink":
        os.link(path, tmp_path / "other.jsonl")
    with pytest.raises(SessionFileError):
        relocate_session_file(path, tmp_path)
    assert (
        path.read_bytes() if not path.is_symlink() else (tmp_path / "real.jsonl").read_bytes()
    ) == original
    assert not list(tmp_path.glob("*.cwd-*"))
    assert not list(tmp_path.glob(".migrating-*"))


def test_a_backup_under_the_same_name_that_differs_is_never_overwritten(tmp_path):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    migration = relocate_session_file(path, tmp_path / "x")
    # Put the conversation back and tamper with the backup that would be reused.
    path.write_bytes(original)
    migration.backup.chmod(0o600)
    migration.backup.write_bytes(b"somebody else's file")
    with pytest.raises(SessionFileError, match="refusing to overwrite"):
        relocate_session_file(path, tmp_path / "x")
    assert migration.backup.read_bytes() == b"somebody else's file"
    assert path.read_bytes() == original


def test_a_stray_original_file_is_left_alone_and_does_not_stop_a_migration(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path)
    (tmp_path / "s.jsonl.cwd-original").write_bytes(b"not ours")
    assert relocate_session_file(path, tmp_path / "x") is not None
    assert (tmp_path / "s.jsonl.cwd-original").read_bytes() == b"not ours"


def test_an_identical_backup_from_an_interrupted_run_is_reused(tmp_path):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    first = relocate_session_file(path, tmp_path / "x")
    path.write_bytes(original)
    assert relocate_session_file(path, tmp_path / "x") is not None
    assert first.backup.read_bytes() == original


def test_a_conversation_can_be_moved_again_and_every_earlier_state_is_kept(tmp_path):
    """The base directory can change, so the same conversation is migrated more than once, with
    new turns in between."""
    path = tmp_path / "s.jsonl"
    original = _write(path)
    first = relocate_session_file(path, tmp_path / "one")
    with open(path, "ab") as handle:
        handle.write(b'{"type":"message","id":"later"}\n')
    second_before = path.read_bytes()
    second = relocate_session_file(path, tmp_path / "two")
    assert second is not None and first.backup != second.backup
    assert first.backup.read_bytes() == original and second.backup.read_bytes() == second_before
    assert (tmp_path / "s.jsonl.cwd-original").read_bytes() == original
    assert _header(path)["cwd"] == str((tmp_path / "two").resolve())
    assert rollback(second) is True and path.read_bytes() == second_before


def test_an_interruption_before_the_rename_leaves_the_conversation_as_it_was(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    real_rename = os.rename

    def fail(src, dst):
        if os.fspath(dst) == str(path):
            raise KeyboardInterrupt
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", fail)
    with pytest.raises(KeyboardInterrupt):
        relocate_session_file(path, tmp_path / "x")
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".migrating-*"))
    # And the next attempt completes.
    assert relocate_session_file(path, tmp_path / "x") is not None
    assert _header(path)["cwd"] == str((tmp_path / "x").resolve())


def test_a_conversation_written_to_during_the_copy_is_not_migrated(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    real_fsync = os.fsync
    state = {"done": False}

    def fsync(fd):
        if not state["done"]:
            state["done"] = True
            with open(path, "ab") as other:
                other.write(b'{"type":"message","id":"late"}\n')
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(SessionFileError, match="changed while"):
        relocate_session_file(path, tmp_path / "x")
    monkeypatch.undo()
    assert path.read_bytes() == original + b'{"type":"message","id":"late"}\n'
    assert _header(path)["cwd"] == "/old/project" and not list(tmp_path.glob(".migrating-*"))


def test_two_resumes_at_once_migrate_it_once(tmp_path):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    results, errors = [], []

    def attempt():
        try:
            results.append(relocate_session_file(path, tmp_path / "x"))
        except Exception as exc:  # pragma: no cover - the failure being tested for
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len([r for r in results if r is not None]) == 1
    assert _header(path)["cwd"] == str((tmp_path / "x").resolve())
    assert (tmp_path / "s.jsonl.cwd-original").read_bytes() == original
    assert len(list(tmp_path.glob("*.cwd-backup-*"))) == 1


def test_rollback_restores_the_original_only_while_nothing_has_been_added(tmp_path):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    migration = relocate_session_file(path, tmp_path / "x")
    assert rollback(migration) is True
    assert path.read_bytes() == original

    migration = relocate_session_file(path, tmp_path / "x")
    with open(path, "ab") as handle:
        handle.write(b'{"type":"message","id":"new"}\n')
    grown = path.read_bytes()
    assert rollback(migration) is False
    assert path.read_bytes() == grown


def _gate_path(path):
    return str(path.parent / (".%s.lock.os" % path.name))


def test_omps_own_publish_gate_held_by_another_writer_stops_the_migration(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    monkeypatch.setattr(module, "LOCK_WAIT_SECONDS", 0.2)
    gate = module._Gate.try_acquire(_gate_path(path))
    try:
        with pytest.raises(SessionFileError, match="publish lock"):
            relocate_session_file(path, tmp_path / "x")
    finally:
        gate.release()
    assert path.read_bytes() == original and not list(tmp_path.glob("*.cwd-*"))
    # And once it is released the migration goes through.
    assert relocate_session_file(path, tmp_path / "x") is not None


def _dead_pid():
    import subprocess
    import sys

    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    return gone.pid


def _lockfile(path):
    return path.parent / (".%s.lock" % path.name)


def test_a_lockfile_held_by_a_live_process_is_never_taken_over(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    original = _write(path)
    lock = _lockfile(path)
    lock.write_text("%d:1\n" % os.getpid())
    monkeypatch.setattr(module, "LOCK_WAIT_SECONDS", 0.2)
    with pytest.raises(SessionFileError, match="live writer"):
        relocate_session_file(path, tmp_path / "x")
    assert lock.read_text() == "%d:1\n" % os.getpid() and path.read_bytes() == original
    # The gate it took on the way is free again.
    gate = module._Gate.try_acquire(_gate_path(path))
    assert gate is not None
    gate.release()


def test_a_lockfile_left_by_a_dead_process_is_cleared_as_omp_does_and_the_migration_goes_on(
    tmp_path,
):
    path = tmp_path / "s.jsonl"
    _write(path)
    lock = _lockfile(path)
    lock.write_text("%d:1\n" % _dead_pid())
    assert relocate_session_file(path, tmp_path / "x") is not None
    assert not lock.exists()


@pytest.mark.parametrize("age", (0, 5))
def test_a_lockfile_with_no_record_is_an_orphan_only_once_it_is_old(tmp_path, monkeypatch, age):
    path = tmp_path / "s.jsonl"
    _write(path)
    lock = _lockfile(path)
    lock.write_text("")
    os.utime(lock, (time.time() - age,) * 2)
    monkeypatch.setattr(module, "LOCK_WAIT_SECONDS", 0.1)
    if age:
        assert relocate_session_file(path, tmp_path / "x") is not None and not lock.exists()
    else:
        with pytest.raises(SessionFileError, match="live writer"):
            relocate_session_file(path, tmp_path / "x")
        assert lock.exists()


def test_a_migration_killed_while_it_held_the_locks_does_not_strand_the_conversation(tmp_path):
    """SIGKILL after the lockfile is made: the gate goes with the process and the lockfile stays."""
    import signal
    import subprocess
    import sys

    path = tmp_path / "s.jsonl"
    original = _write(path)
    code = (
        "import os, signal, sys\n"
        "from annealage_ember.session import omp_session_file as m\n"
        "with m._locked(sys.argv[1]):\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    killed = subprocess.run([sys.executable, "-c", code, str(path)], env=dict(os.environ))
    assert killed.returncode == -signal.SIGKILL
    assert _lockfile(path).exists()
    migration = relocate_session_file(path, tmp_path / "x")
    assert migration is not None and not _lockfile(path).exists()
    assert migration.backup.read_bytes() == original
    assert rollback(migration) is True and path.read_bytes() == original


def test_a_conversation_already_in_place_is_not_held_up_by_any_lock(tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    _write(path)
    relocate_session_file(path, tmp_path / "x")
    _lockfile(path).write_text("%d:1\n" % os.getpid())
    monkeypatch.setattr(module, "LOCK_WAIT_SECONDS", 5)
    started = time.monotonic()
    assert relocate_session_file(path, tmp_path / "x") is None
    assert time.monotonic() - started < 1
    gate = module._Gate.try_acquire(_gate_path(path))
    try:
        assert relocate_session_file(path, tmp_path / "x") is None
    finally:
        gate.release()


def test_the_locks_are_released_after_a_migration_and_after_a_refusal(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path)
    relocate_session_file(path, tmp_path / "x")
    assert not (tmp_path / (".%s.lock" % path.name)).exists()
    gate = module._Gate.try_acquire(_gate_path(path))
    assert gate is not None
    gate.release()


def test_the_xxhash_the_gate_is_named_with_matches_the_reference():
    assert module.xxh64(b"", 0) == 0xEF46DB3751D8E999
    assert module.xxh64(b"abc", 0) == 0x44BC2CF5AD770999
    # A length over 32 takes the four-lane path.
    assert module.xxh64(b"Nobody inspects the spammish repetition", 0) == 0xFBCEA83C8A378BF1


@pytest.mark.skipif(
    __import__("shutil").which("omp") is None or __import__("shutil").which("bun") is None,
    reason="needs omp and bun",
)
def test_the_gate_excludes_and_is_excluded_by_the_native_lock_of_the_omp_on_the_path(tmp_path):
    """The lock is only worth taking if omp's own native lock and ours are the same lock."""
    import shutil
    import subprocess

    package = Path(os.path.realpath(shutil.which("omp")))
    while package.name != "pi-coding-agent":
        assert package != package.parent, "omp is not an installed pi-coding-agent"
        package = package.parent
    gate_path = _gate_path(tmp_path / "s.jsonl")
    script = (
        'import {FileLock} from "@oh-my-pi/pi-natives";'
        "const g = FileLock.tryAcquire(process.argv[1]);"
        'console.log(g.acquired ? "acquired" : "busy");'
        'if (process.argv[2] === "hold") { console.log("held"); '
        "await new Promise(r => setTimeout(r, 4000)); } g.release();"
    )
    ours = module._Gate.try_acquire(gate_path)
    seen = subprocess.run(
        ["bun", "-e", script, gate_path, "try"],
        cwd=package,
        capture_output=True,
        text=True,
        timeout=30,
    )
    ours.release()
    assert seen.stdout.split() == ["busy"], seen.stderr
    holder = subprocess.Popen(
        ["bun", "-e", script, gate_path, "hold"], cwd=package, stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout.readline().strip() == "acquired"
        assert holder.stdout.readline().strip() == "held"
        assert module._Gate.try_acquire(gate_path) is None
    finally:
        holder.kill()
        holder.wait()
