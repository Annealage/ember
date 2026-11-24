"""One live agent per project: ``<state dir>/agent.lock``.

``lock.py`` keeps two *servers* off one project. This is the narrower question
of who may run the *agent*: two opens (a second process, or a service
restarted while its predecessor was still dying) against one conversation
would both write to the same omp session directory.

An exclusive ``flock`` on the file is the lock. It belongs to the open file
description, so it is released the moment its holder dies however it died (a
SIGKILL after a stop timeout included): a leftover lock file never blocks
anything. What a SIGKILLed server can leave behind is its agent child, so the
holder records its pid, kernel start time and command line. A successor stops
the matching process, including its process group when it is a dedicated group,
before opening another agent. Failed termination leaves the record intact and
the claim retryable. A reused pid is left alone.

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

from .lock import _boot_id

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


class AgentRecoveryError(Exception):
    """A recorded agent could not be stopped; retrying the claim is safe."""


def _identity(pid: int) -> Optional[dict]:
    """``{"pid", "start", "cmd"}`` for a live process, or ``None``: its kernel
    start time (clock ticks since boot) is what tells it from a later process
    that got the same pid."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as stat:
            raw = stat.read().decode("utf-8", "replace")
        # The command name is in parentheses and may contain spaces or ')'.
        fields = raw[raw.rindex(")") + 2 :].split()
        if fields[0] == "Z":
            return None
        start = int(fields[19])
        with open("/proc/%d/cmdline" % pid, "rb") as cmd:
            command = cmd.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except (OSError, ValueError, IndexError):
        return None
    return {"pid": pid, "start": start, "cmd": command}


def _group_alive(pgid: int) -> bool:
    """Whether a dedicated process group still contains non-zombie processes."""
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as stat:
                raw = stat.read().decode("utf-8", "replace")
            fields = raw[raw.rindex(")") + 2 :].split()
            if fields[0] != "Z" and int(fields[2]) == pgid:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as exc:
            raise AgentRecoveryError("could not inspect agent process group: %s" % exc) from exc
    return False


def _end_process(agent: dict) -> bool:
    """Stop the identified agent/group; return whether a signal was sent."""
    pid = agent["pid"]
    pgid = agent.get("pgid")
    group = pgid == pid

    def alive():
        if group:
            return _group_alive(pgid)
        live = _identity(pid)
        return live is not None and live["start"] == agent["start"] and live["cmd"] == agent["cmd"]

    signalled = False
    for sig, wait in ((signal.SIGTERM, ORPHAN_GRACE), (signal.SIGKILL, 1.0)):
        if not alive():
            return signalled
        try:
            if group:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
            signalled = True
        except ProcessLookupError:
            if not alive():
                return signalled
        except OSError as exc:
            raise AgentRecoveryError("could not signal agent process %d: %s" % (pid, exc)) from exc
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not alive():
                return signalled
            time.sleep(0.05)
    if alive():
        raise AgentRecoveryError("agent process %d did not stop" % pid)
    return signalled


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
        try:
            agent = self._read(fd).get("agent")
            if isinstance(agent, dict) and isinstance(agent.get("pid"), int):
                current_boot = _boot_id()
                old_boot = agent.get("boot")
                same_boot = not old_boot or not current_boot or old_boot == current_boot
                live = _identity(agent["pid"]) if same_boot else None
                same = (
                    live is not None
                    and live["start"] == agent.get("start")
                    and live["cmd"] == agent.get("cmd")
                )
                # A dedicated group can outlive its leader. A reused live
                # leader, however, is never evidence of the old group.
                orphaned_group = (
                    same_boot
                    and live is None
                    and agent.get("pgid") == agent["pid"]
                    and isinstance(agent.get("start"), int)
                    and isinstance(agent.get("cmd"), str)
                )
                if (same or orphaned_group) and _end_process(agent):
                    self.reaped = agent["pid"]
            self._write({"pid": os.getpid()})
        except BaseException:
            self._fd = None
            os.close(fd)
            raise

    def record_agent(self, pid) -> None:
        """Note the agent child just started, so a successor can end it if this
        process is killed without closing it."""
        if self._fd is None or not isinstance(pid, int):
            return
        identity = _identity(pid)
        if identity is not None:
            identity["boot"] = _boot_id()
            try:
                if os.getpgid(pid) == pid:
                    identity["pgid"] = pid
            except ProcessLookupError:
                pass
        self._write({"pid": os.getpid(), **({"agent": identity} if identity else {})})

    def release(self) -> None:
        if self._fd is None:
            return
        # Keep the record until the next claim confirms the process is gone.
        # Session.close() may fail or leave descendants behind.
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
