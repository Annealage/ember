"""One live agent per project: ``<state dir>/agent.lock``.

``lock.py`` keeps two *servers* off one project. This is the narrower question
of who may run the *agent*: two opens (a second process, or a service
restarted while its predecessor was still dying) against one conversation
would both write to the same omp session directory.

An exclusive ``flock`` on the file is the lock. It belongs to the open file
description, so it is released the moment its holder dies however it died (a
SIGKILL after a stop timeout included): a leftover lock file never blocks
anything. What a SIGKILLed server can leave behind is its agent child, which
``flock`` knows nothing about, so the holder also records the child it started
(pid, kernel start time and command line) in the file. The next ``acquire``
finds that record, and if the process is still the same one it ends it before
the new agent starts, rather than failing the open. A recorded pid that is dead,
or has been reused by some other process, is left alone.

Linux ``/proc`` is how a process is recognised; where it is not there the
record cannot be checked, and an orphan is then left alone rather than guessed
at.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

#: Seconds an orphaned agent is given to exit after SIGTERM before SIGKILL.
ORPHAN_GRACE = 3.0


class AgentAlreadyOpen(Exception):
    """Another live holder has this project's agent lock. ``owner`` is its pid
    when the file says."""

    def __init__(self, owner: Optional[int]):
        self.owner = owner
        super().__init__(
            "another process%s already has an agent open for this project"
            % (" (pid %d)" % owner if owner else "")
        )


def _identity(pid: int) -> Optional[dict]:
    """``{"pid", "start", "cmd"}`` for a live process, or ``None``: its kernel
    start time (clock ticks since boot) is what tells it from a later process
    that got the same pid."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as stat:
            raw = stat.read().decode("utf-8", "replace")
        # The command name is in parentheses and may contain spaces or ')'.
        fields = raw[raw.rindex(")") + 2:].split()
        if fields[0] == "Z":
            return None
        start = int(fields[19])
        with open("/proc/%d/cmdline" % pid, "rb") as cmd:
            command = cmd.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except (OSError, ValueError, IndexError):
        return None
    return {"pid": pid, "start": start, "cmd": command}


def _end_process(pid: int) -> None:
    """SIGTERM, then SIGKILL after ``ORPHAN_GRACE``; never raises."""
    for sig, wait in ((signal.SIGTERM, ORPHAN_GRACE), (signal.SIGKILL, 1.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except OSError as exc:
            sys.stderr.write("warning: could not signal agent process %d: %r\n" % (pid, exc))
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if _identity(pid) is None:
                return
            time.sleep(0.05)


class AgentLock:
    def __init__(self, directory):
        self.path = Path(directory) / "agent.lock"
        self._fd: Optional[int] = None
        #: The pid of an orphaned agent ``acquire`` ended, else ``None``.
        self.reaped: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock, ending a recorded orphan agent first. Raises
        ``AgentAlreadyOpen`` when a live holder has it. Idempotent."""
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = self._read(fd).get("pid")
            os.close(fd)
            raise AgentAlreadyOpen(owner if isinstance(owner, int) else None) from None
        self._fd = fd
        self.reaped = None
        agent = self._read(fd).get("agent")
        if isinstance(agent, dict):
            live = _identity(agent.get("pid")) if isinstance(agent.get("pid"), int) else None
            if live is not None and live["start"] == agent.get("start") and live["cmd"] == agent.get("cmd"):
                self.reaped = live["pid"]
                _end_process(live["pid"])
        self._write({"pid": os.getpid()})

    def record_agent(self, pid) -> None:
        """Note the agent child just started, so a successor can end it if this
        process is killed without closing it."""
        if self._fd is None or not isinstance(pid, int):
            return
        identity = _identity(pid)
        self._write({"pid": os.getpid(), **({"agent": identity} if identity else {})})

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            # The agent is closed (or being), so there is nothing left to reap.
            self._write({"pid": os.getpid()})
        finally:
            fd, self._fd = self._fd, None
            os.close(fd)

    @staticmethod
    def _read(fd) -> dict:
        try:
            raw = os.pread(fd, 65536, 0)
            data = json.loads(raw) if raw else {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, record: dict) -> None:
        try:
            os.ftruncate(self._fd, 0)
            os.pwrite(self._fd, json.dumps(record).encode("utf-8"), 0)
        except OSError as exc:
            sys.stderr.write("warning: could not record the agent lock: %r\n" % (exc,))
