"""Moving a durable omp conversation to the working directory omp now runs in.

omp resumes a conversation only from the directory recorded in its session file's header
(``switch_session`` comes back cancelled otherwise: omp 18.11.3 raises
``SESSION_CWD_CHANGE_REJECTED`` in rpc mode, and no rpc command takes the
``preserveLocalCwd`` option that skips the check). A conversation an earlier Ember started
recorded the caller's own directory, so resuming it from the private directory of
``omp_cwd`` needs that one field changed. There is no omp API for it (``open_session`` and
``fork`` go through the same check or mint a new session id, which would lose the provider's
cache identity), so ``relocate_session_file`` does it to the file, with these guarantees:

* **The original is never overwritten.** Before a file is changed its bytes are copied to
  ``<file>.cwd-backup-<sha256 prefix of those bytes>`` (created with ``link``, which cannot
  replace an existing file), so each migration has its own backup and a conversation can be
  moved again later, when the base directory changes. The first conversation bytes ever
  migrated are also kept as ``<file>.cwd-original``, a hard link to that backup that is never
  replaced. A backup that already exists under the same name must be identical.
* **The change is one field.** The header line is parsed, ``cwd`` is replaced, and it is
  written back compact; every other byte of the file, the title slot line omp 18.11.3 puts
  first and every entry after the header included, is copied unchanged. The session id and the
  provider's cache identity (``providerPromptCacheKey``) are untouched, so the provider
  history survives.
* **It is strict.** A symlink, a file with another link, a file owned by someone else, a
  header that is not the one ``type: session`` object in the first two lines, a version other
  than 3 (the version of both 18.4.5 and 18.11.3), a missing id, or any
  ``additionalDirectories`` (extra workspace roots omp would still look in) raises
  ``SessionFileError`` and the file is left as it was.
* **It is atomic.** The new file is written beside the old one, flushed, checked, and
  moved over it with ``rename``. An interruption leaves either the original or the finished
  copy, plus at most a ``.migrating-*`` temporary, never a half-written conversation. The
  copy is refused if the file changed while it was read (another writer).
* **It takes omp's own publish lock.** omp 18.4.5 and 18.11.3 write a session file under two
  layers (``session-storage.ts``, ``#withPublishLock``): an OS gate named ``.<file>.lock.os``
  and, inside it, a lockfile ``.<file>.lock`` created with ``O_EXCL`` that older writers still
  use. The freshness check and the final ``rename`` run under both, so an append that lands
  between our last look and the rename is impossible while we hold them, and omp's own append
  waits. The gate on Linux is a bound abstract Unix socket whose name is two xxh64 hashes of the
  gate path (``crates/pi-natives/src/file_lock``); elsewhere it is an ``flock`` on the
  persistent sidecar. A lockfile left under the gate is taken over only as omp does it
  (``#stealStalePublishLock``): its pid is dead (``kill(pid, 0)`` gives ESRCH), or its record
  does not parse and is older than omp's lock wait, and it is read again before removal; one
  held by a live pid is waited for and then the migration is refused. Our own migrations and
  rollbacks take the same locks, so two Ember processes also serialise on them.
* **It can be undone.** ``rollback`` puts the pre-migration bytes back from that migration's
  own backup, under the same locks, but only while the migrated file is still exactly the one
  written, so a conversation omp has since appended to is never rolled back over.

What it cannot cover: the gate is keyed by the path string, so a writer that reaches the file
by another spelling of its path (a symlinked directory) does not meet it; and an omp that
already has the conversation open keeps appending to its own view after the file is replaced,
which its freshness check then reports as a write conflict rather than losing the migrated
file. Resuming one conversation from two processes at once is not supported by omp either.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

#: The header ``version`` of omp 18.4.5 and 18.11.3 session files.
SUPPORTED_VERSIONS = frozenset({3})
ORIGINAL_SUFFIX = ".cwd-original"
BACKUP_PREFIX = ".cwd-backup-"
#: How long a lock held by another writer is waited for, as omp does (its bounded wait).
LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.02
#: A lockfile with no readable ``pid:time`` record is a crash orphan once older than this (omp's
#: ``SESSION_PUBLISH_LOCK_WAIT_MS``, the longest a live acquirer can take to record itself).
ORPHAN_AFTER_SECONDS = 0.5
_LOCKFILE_RECORD = re.compile(rb"^(\d+):(\d+)\s*$")
#: A header or title line longer than this is not one.
_MAX_LINE = 1 << 20


class SessionFileError(ValueError):
    """The conversation file cannot be migrated safely; it has not been changed."""


@dataclass(frozen=True)
class Migration:
    """What ``relocate_session_file`` did, for ``rollback``."""

    path: Path
    backup: Path
    previous_cwd: str
    new_cwd: str
    migrated_size: int
    migrated_mtime_ns: int


def _fsync_dir(directory: str) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_backup(path: str) -> None:
    """A backup is ours and a plain file; it is linked on purpose (``.cwd-original``)."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SessionFileError("%s is not a plain file of this user" % path)


def _check_file(path: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise SessionFileError("cannot inspect %s: %s" % (path, exc)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise SessionFileError("%s is not a regular file (or is a symlink)" % path)
    if info.st_nlink != 1:
        raise SessionFileError("%s has more than one hard link" % path)
    if info.st_uid != os.getuid():
        raise SessionFileError("%s is owned by another user" % path)
    return info


def _header(handle) -> "tuple[int, bytes, dict]":
    """``(offset, raw line, parsed)`` of the session header: the first or second line."""
    offset = 0
    for index in range(2):
        raw = handle.readline(_MAX_LINE + 1)
        if not raw or len(raw) > _MAX_LINE or not raw.endswith(b"\n"):
            break
        try:
            value = json.loads(raw)
        except ValueError:
            break
        if isinstance(value, dict) and value.get("type") == "session":
            return offset, raw, value
        if index == 0 and not (isinstance(value, dict) and value.get("type") == "title"):
            break
        offset += len(raw)
    raise SessionFileError("no session header in the first two lines")


def _validate(header: dict) -> str:
    version = header.get("version")
    if type(version) is not int or version not in SUPPORTED_VERSIONS:
        raise SessionFileError(
            "session file version %r is not one this migration knows" % (version,)
        )
    if not isinstance(header.get("id"), str) or not header["id"]:
        raise SessionFileError("session header has no id")
    cwd = header.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise SessionFileError("session header has no cwd")
    if header.get("additionalDirectories"):
        raise SessionFileError(
            "the conversation has additional workspace directories, which omp would still read"
        )
    return cwd


def _digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


_M64 = (1 << 64) - 1
_P1, _P2, _P3, _P4, _P5 = (
    11400714785074694791,
    14029467366897019727,
    1609587929392839161,
    9650029242287828579,
    2870177450012600261,
)


def _rotl(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & _M64


def _round(acc: int, lane: int) -> int:
    return (_rotl((acc + lane * _P2) & _M64, 31) * _P1) & _M64


def _merge(acc: int, value: int) -> int:
    return (((acc ^ _round(0, value)) * _P1) + _P4) & _M64


def xxh64(data: bytes, seed: int) -> int:
    """xxHash64, which omp's native file lock names its Linux gate with."""
    length, pos = len(data), 0
    if length >= 32:
        v = [(seed + _P1 + _P2) & _M64, (seed + _P2) & _M64, seed & _M64, (seed - _P1) & _M64]
        while pos <= length - 32:
            for k in range(4):
                v[k] = _round(v[k], int.from_bytes(data[pos : pos + 8], "little"))
                pos += 8
        h = (_rotl(v[0], 1) + _rotl(v[1], 7) + _rotl(v[2], 12) + _rotl(v[3], 18)) & _M64
        for k in range(4):
            h = _merge(h, v[k])
    else:
        h = (seed + _P5) & _M64
    h = (h + length) & _M64
    while pos + 8 <= length:
        h ^= _round(0, int.from_bytes(data[pos : pos + 8], "little"))
        h = (_rotl(h, 27) * _P1 + _P4) & _M64
        pos += 8
    if pos + 4 <= length:
        h ^= (int.from_bytes(data[pos : pos + 4], "little") * _P1) & _M64
        h = (_rotl(h, 23) * _P2 + _P3) & _M64
        pos += 4
    while pos < length:
        h ^= (data[pos] * _P5) & _M64
        h = (_rotl(h, 11) * _P1) & _M64
        pos += 1
    h ^= h >> 33
    h = (h * _P2) & _M64
    h ^= h >> 29
    h = (h * _P3) & _M64
    h ^= h >> 32
    return h


def _pid_alive(pid: int) -> bool:
    """omp's ``isPidAlive``: only ESRCH means dead; EPERM and anything else count as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _steal_stale_lockfile(lock_path: str) -> bool:
    """omp's ``#stealStalePublishLock``, for a lockfile found while holding the OS gate: remove it
    only if its holder is verifiably dead, and say whether to try to take it again.

    With the gate held, no live omp is inside its own publish, so a lockfile that remains was left
    by a process that died, or by an older omp that holds the lockfile alone (its pid is then
    alive and the file is kept). A record that does not parse is an orphan only once older than
    ``ORPHAN_AFTER_SECONDS``. The file is read again just before it is removed, so a lock
    replaced by a live holder in between is not removed."""
    try:
        with open(lock_path, "rb") as handle:
            content = handle.read()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    match = _LOCKFILE_RECORD.match(content)
    pid = int(match.group(1)) if match else 0
    if pid > 0:
        if _pid_alive(pid):
            return False
    else:
        try:
            age = time.time() - os.stat(lock_path).st_mtime
        except FileNotFoundError:
            return True
        if age <= ORPHAN_AFTER_SECONDS:
            return False
    try:
        with open(lock_path, "rb") as handle:
            if handle.read() != content:
                return False
        os.unlink(lock_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _gate_name(gate_path: str) -> str:
    """``memory_lock_name`` of ``crates/pi-natives/src/file_lock/mod.rs``."""
    data = gate_path.encode()
    high = xxh64(data, 0x4F4D502D4C4F434B)
    low = xxh64(data, 0x5049_2D46_494C_454C)
    return "omp-file-lock-%016x%016x" % (high, low)


class _Gate:
    """omp's ``FileLock.tryAcquire``: an abstract Unix socket on Linux, an ``flock`` elsewhere."""

    def __init__(self, handle) -> None:
        self._handle = handle

    @classmethod
    def try_acquire(cls, gate_path: str) -> "Optional[_Gate]":
        if sys.platform.startswith("linux"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sock.bind("\0" + _gate_name(gate_path))
            except OSError as exc:
                sock.close()
                if exc.errno == 98:  # EADDRINUSE
                    return None
                raise
            return cls(sock)
        fd = os.open(gate_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return cls(fd)

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if isinstance(handle, int):
            os.close(handle)
        elif handle is not None:
            handle.close()


@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    """omp's publish lock for ``path``: the OS gate, then the ``O_EXCL`` lockfile inside it."""
    directory, base = os.path.split(path)
    lock_path = os.path.join(directory, ".%s.lock" % base)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    gate = None
    while gate is None:
        gate = _Gate.try_acquire(lock_path + ".os")
        if gate is None:
            if time.monotonic() >= deadline:
                raise SessionFileError("another writer holds the conversation's publish lock")
            time.sleep(_LOCK_POLL_SECONDS)
    held = False
    try:
        while not held:
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # Under the gate, so a lockfile still here is a dead holder's or an older omp's.
                # Taken over only as omp does it: holder verifiably dead (a live one is waited for).
                if _steal_stale_lockfile(lock_path):
                    continue
                if time.monotonic() >= deadline:
                    raise SessionFileError(
                        "the publish lockfile %s belongs to a live writer; not taking it over"
                        % lock_path
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
                continue
            with os.fdopen(fd, "w") as handle:
                handle.write("%d:%d\n" % (os.getpid(), int(time.time() * 1000)))
            held = True
        yield
    finally:
        if held:
            with contextlib.suppress(OSError):
                os.unlink(lock_path)
        gate.release()


def _write_backup(path: str) -> str:
    """Copy the file as it is to a backup named for its bytes; the first one is also the original."""
    digest = _digest(path)
    backup = "%s%s%s" % (path, BACKUP_PREFIX, digest[:32])
    if os.path.lexists(backup):
        _check_backup(backup)
        if _digest(backup) != digest:
            raise SessionFileError(
                "%s already exists and differs from the conversation; refusing to overwrite it"
                % backup
            )
    else:
        fd, temp = tempfile.mkstemp(prefix=".backup-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "wb") as out, open(path, "rb") as source:
                shutil.copyfileobj(source, out, 1 << 20)
                out.flush()
                os.fsync(out.fileno())
            shutil.copystat(path, temp)
            os.link(temp, backup)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temp)
    original = path + ORIGINAL_SUFFIX
    if not os.path.lexists(original):
        with contextlib.suppress(FileExistsError):
            os.link(backup, original)
    _fsync_dir(os.path.dirname(path))
    return backup


def relocate_session_file(path: os.PathLike, cwd: os.PathLike) -> Optional[Migration]:
    """Record ``cwd`` as the conversation's working directory; None when it already is.

    Raises ``SessionFileError`` and leaves the file unchanged when it cannot be done safely."""
    path = os.path.abspath(os.fspath(path))
    target = os.path.realpath(os.fspath(cwd))
    _check_file(path)
    # A conversation already in place needs nothing, and must not wait for, or be refused by, a
    # lock that is none of its business (omp deals with its own).
    with open(path, "rb") as handle:
        if os.path.normpath(_validate(_header(handle)[2])) == target:
            return None
    with _locked(path):
        before = _check_file(path)
        with open(path, "rb") as handle:
            offset, raw, header = _header(handle)
            recorded = _validate(header)
            if os.path.normpath(recorded) == target:
                return None
            header_end = handle.tell()
        migrated_header = dict(header)
        migrated_header["cwd"] = target
        line = (
            json.dumps(migrated_header, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        backup = _write_backup(path)
        directory = os.path.dirname(path)
        fd, temp = tempfile.mkstemp(prefix=".migrating-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as out, open(path, "rb") as source:
                out.write(source.read(offset))
                out.write(line)
                source.seek(header_end)
                shutil.copyfileobj(source, out, 1 << 20)
                out.flush()
                os.fsync(out.fileno())
            shutil.copystat(path, temp)
            after = os.lstat(path)
            if (after.st_size, after.st_mtime_ns, after.st_ino) != (
                before.st_size,
                before.st_mtime_ns,
                before.st_ino,
            ):
                raise SessionFileError(
                    "the conversation changed while it was being copied; not migrated"
                )
            expected = before.st_size - len(raw) + len(line)
            if os.stat(temp).st_size != expected:
                raise SessionFileError("the migrated copy has the wrong size; not used")
            with open(temp, "rb") as check:
                _, _, written = _header(check)
            if written != migrated_header:
                raise SessionFileError("the migrated header does not read back; not used")
            os.rename(temp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        _fsync_dir(directory)
        done = os.lstat(path)
        return Migration(
            path=Path(path),
            backup=Path(backup),
            previous_cwd=recorded,
            new_cwd=target,
            migrated_size=done.st_size,
            migrated_mtime_ns=done.st_mtime_ns,
        )


def rollback(migration: Migration) -> bool:
    """Put the original back. False, and nothing changed, when the conversation has been written to
    since (it is no longer the file this migration made)."""
    path = os.fspath(migration.path)
    with _locked(path):
        info = _check_file(path)
        if (info.st_size, info.st_mtime_ns) != (
            migration.migrated_size,
            migration.migrated_mtime_ns,
        ):
            return False
        _check_backup(os.fspath(migration.backup))
        directory = os.path.dirname(path)
        fd, temp = tempfile.mkstemp(prefix=".rollback-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as out, open(migration.backup, "rb") as source:
                shutil.copyfileobj(source, out, 1 << 20)
                out.flush()
                os.fsync(out.fileno())
            shutil.copystat(migration.backup, temp)
            os.rename(temp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        _fsync_dir(directory)
        return True
