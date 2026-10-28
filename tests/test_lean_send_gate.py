"""``LeanRpcClient.send_gate``: the revocation check and the wire write are one step under the write lock."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("omp_rpc")

from omp_rpc.client import RpcTimeoutError  # noqa: E402

from annealage_ember import hosted_authority  # noqa: E402
from annealage_ember.session.rpc_client import LeanRpcClient, SendRefused  # noqa: E402


class _Stdin:
    def __init__(self):
        self.lines, self.pause, self.broken = [], None, False
        self._buf = ""

    def write(self, text):
        if self.pause is not None:
            self.pause.wait(5)  # a full pipe: the write blocks
        if self.broken:
            raise BrokenPipeError("omp was killed")
        self._buf += text

    def flush(self):
        for line in self._buf.splitlines():
            self.lines.append(json.loads(line))
        self._buf = ""


def _client():
    client = LeanRpcClient(request_timeout=0.3)
    stdin = _Stdin()
    client._process = SimpleNamespace(
        stdin=stdin,
        kill=lambda: (setattr(stdin, "broken", True), stdin.pause and stdin.pause.set()),
    )
    return client, stdin


def _types(stdin):
    return [line["type"] for line in stdin.lines]


def test_a_refused_check_writes_nothing_and_leaves_no_pending_request():
    client, stdin = _client()
    with client.send_gate(lambda: "revoked"):
        with pytest.raises(SendRefused, match="revoked"):
            client.prompt("hi")
    assert stdin.lines == [] and client._pending == {}


def test_a_passing_check_writes_the_prompt_and_only_commands_that_start_work_are_gated():
    client, stdin = _client()
    checks = []
    with client.send_gate(lambda: checks.append(1) or None):
        with pytest.raises(RpcTimeoutError):  # no omp to answer; the write happened
            client.prompt("hi")
        with pytest.raises(RpcTimeoutError):
            client.abort()
    assert _types(stdin) == ["prompt", "abort"]
    assert checks == [1]  # abort is never gated: a revoked grant must still be able to stop work


def test_the_check_runs_under_the_write_lock_so_an_abort_cannot_overtake_a_checked_prompt():
    client, stdin = _client()
    order = []

    def check():
        # Inside the lock: nothing else can write now.
        order.append("locked" if not client._write_lock.acquire(blocking=False) else "free")
        return

    with client.send_gate(check):
        with pytest.raises(RpcTimeoutError):
            client.prompt("hi")
    assert order == ["locked"]


def test_release_does_not_wait_for_a_blocked_prompt_write_and_the_abort_follows_it_on_the_wire():
    client, stdin = _client()
    stdin.pause = threading.Event()  # the pipe is full: the prompt's write blocks
    bus = SimpleNamespace(
        hosted_turn_live=True,
        hosted_turn_ops=(),
        hosted_turn_exp=time.time() + 60,
        hosted_turn_secret="s",
        hosted_turn_principal="alice",
        hosted_turn_owner="conn",
        hosted_turn_generation=1,
        hosted_stopping=0,
        hosted_fenced=False,
        hosted_lock=threading.Lock(),
        hosted_expiry_handle=None,
    )
    hosted_authority.GENERATION.set(1)
    check = hosted_authority.checker(bus)
    errors = []

    def prompt():
        try:
            with client.send_gate(check):
                client.prompt("hi")
        except Exception as exc:  # the timeout waiting for a reply that never comes
            errors.append(exc)

    sender = threading.Thread(target=prompt)
    sender.start()
    time.sleep(0.2)  # checked, and now stuck writing
    started = time.monotonic()
    hosted_authority.release(bus, "conn", None)  # what the event loop does on revocation
    assert time.monotonic() - started < 0.1  # never waits for the write
    aborter = threading.Thread(target=lambda: _swallow(client.abort))
    aborter.start()
    time.sleep(0.1)
    assert stdin.lines == []  # the abort waits behind the prompt for the write lock
    stdin.pause.set()
    sender.join(3)
    aborter.join(3)
    assert _types(stdin) == ["prompt", "abort"]


def _swallow(fn):
    try:
        fn()
    except Exception:
        pass


def test_a_prompt_paused_before_the_wire_is_refused_if_the_grant_ends_meanwhile():
    """The sender is held off the wire (another writer has the lock, or the thread
    is descheduled before it); the grant ends; when it finally gets to write, the
    check runs then and refuses it."""
    client, stdin = _client()
    live = [True]
    errors = []

    def prompt():
        try:
            with client.send_gate(lambda: None if live[0] else "revoked"):
                client.prompt("hi")
        except SendRefused as exc:
            errors.append(exc)

    with client._write_lock:  # some other write in progress
        sender = threading.Thread(target=prompt)
        sender.start()
        time.sleep(0.2)  # waiting for the lock, nothing checked or written yet
        live[0] = False
    sender.join(3)
    assert errors and stdin.lines == []


def test_revoked_between_the_check_and_the_write_the_stalled_write_is_killed_not_delivered():
    """The window the gate cannot close: the check passed, the write is stuck on
    a full pipe, the grant ends. Teardown kills omp, the write fails, and the
    prompt never reaches it."""
    client, stdin = _client()
    stdin.pause = threading.Event()
    errors = []

    def prompt():
        try:
            with client.send_gate(lambda: None):  # the grant still held at the check
                client.prompt("hi")
        except Exception as exc:
            errors.append(exc)

    sender = threading.Thread(target=prompt)
    sender.start()
    time.sleep(0.2)  # past the check, stuck in write
    assert sender.is_alive()
    client.kill()  # what OmpSession.stop_for_revocation does when abort cannot go through
    sender.join(3)
    assert not sender.is_alive()
    assert errors and type(errors[0]).__name__ == "RpcProcessExitError"
    assert stdin.lines == []


def test_kill_takes_down_the_whole_process_group_including_a_child_holding_the_pipes():
    import os
    import subprocess

    client = LeanRpcClient()
    # A leader that spawns a long-running child in its own session/group, as omp does for tools.
    leader = subprocess.Popen(
        ["sh", "-c", "sleep 60 & echo $!; wait"],
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child = int(leader.stdout.readline())
    client._process, client._pgid = leader, leader.pid
    assert client.kill() is True
    time.sleep(0.2)
    assert leader.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the child went with the group (it is reaped by init)


def test_a_group_signal_that_fails_makes_the_kill_unverified(monkeypatch):
    import os
    import subprocess

    client = LeanRpcClient()
    leader = subprocess.Popen(["sleep", "60"], start_new_session=True)
    client._process, client._pgid = leader, leader.pid

    def denied(pgid, sig):
        raise PermissionError("EPERM")

    monkeypatch.setattr(os, "killpg", denied)
    try:
        assert client.kill() is False  # the leader died, but its group could not be signalled
    finally:
        leader.kill()
        leader.wait()
