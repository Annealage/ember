"""Recovery of recorded local agent processes under the existing flock."""

import json
import os
import signal
import subprocess
import sys

import pytest

from annealage_ember import agent_lock


def _orphan_record(directory, process):
    lock = agent_lock.AgentLock(directory)
    lock.acquire()
    lock.record_agent(process.pid)
    record = json.loads((directory / "agent.lock").read_text())
    lock.release()
    return record


def test_failed_signal_preserves_record_unlocks_and_retry_reaps(tmp_path, monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = _orphan_record(tmp_path, process)
        original_kill = os.kill

        def fail(pid, sig):
            if pid == process.pid:
                raise PermissionError("denied")
            return original_kill(pid, sig)

        with monkeypatch.context() as patch:
            patch.setattr(agent_lock.os, "kill", fail)
            lock = agent_lock.AgentLock(tmp_path)
            with pytest.raises(agent_lock.AgentRecoveryError, match="denied"):
                lock.acquire()
            assert not lock.held
            assert json.loads((tmp_path / "agent.lock").read_text()) == record
            assert process.poll() is None
        lock.acquire()
        assert lock.reaped == process.pid
        assert process.wait(timeout=10) is not None
        lock.release()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_reused_pid_is_never_signalled(tmp_path, monkeypatch):
    stale = {"pid": 1, "agent": {"pid": 1234, "start": 10, "cmd": "old", "pgid": 1234}}
    (tmp_path / "agent.lock").write_text(json.dumps(stale))
    monkeypatch.setattr(
        agent_lock, "_identity", lambda pid: {"pid": pid, "start": 11, "cmd": "new"}
    )
    monkeypatch.setattr(agent_lock.os, "killpg", lambda *args: pytest.fail("signalled reused pid"))
    lock = agent_lock.AgentLock(tmp_path)
    lock.acquire()
    assert lock.reaped is None
    lock.release()


def test_record_from_another_boot_never_signals_a_reused_group(tmp_path, monkeypatch):
    stale = {
        "pid": 1,
        "agent": {"pid": 1234, "start": 10, "cmd": "old", "pgid": 1234, "boot": "old-boot"},
    }
    (tmp_path / "agent.lock").write_text(json.dumps(stale))
    monkeypatch.setattr(agent_lock, "_boot_id", lambda: "new-boot")
    monkeypatch.setattr(agent_lock.os, "killpg", lambda *args: pytest.fail("signalled old boot"))
    lock = agent_lock.AgentLock(tmp_path)
    lock.acquire()
    assert lock.reaped is None
    lock.release()


def test_disappearing_proc_entry_during_group_scan_is_ignored(monkeypatch):
    monkeypatch.setattr(agent_lock.os, "listdir", lambda path: ["1234"])

    def disappeared(*args, **kwargs):
        raise ProcessLookupError()

    monkeypatch.setattr("builtins.open", disappeared)
    assert not agent_lock._group_alive(1234)


def test_dead_group_is_not_reported_as_reaped(tmp_path, monkeypatch):
    record = {"pid": 1, "agent": {"pid": 1234, "start": 10, "cmd": "old", "pgid": 1234}}
    (tmp_path / "agent.lock").write_text(json.dumps(record))
    monkeypatch.setattr(agent_lock, "_identity", lambda pid: None)
    monkeypatch.setattr(agent_lock, "_group_alive", lambda pgid: False)
    lock = agent_lock.AgentLock(tmp_path)
    lock.acquire()
    assert lock.reaped is None
    lock.release()


def test_legacy_record_without_agent_is_reclaimed(tmp_path):
    (tmp_path / "agent.lock").write_text(json.dumps({"pid": 123}))
    lock = agent_lock.AgentLock(tmp_path)
    lock.acquire()
    assert lock.held and lock.reaped is None
    lock.release()


def test_dedicated_group_descendant_is_stopped_after_leader_exits(tmp_path):
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", script], start_new_session=True, stdout=subprocess.PIPE, text=True
    )
    child_pid = int(leader.stdout.readline())
    try:
        record = _orphan_record(tmp_path, leader)
        assert record["agent"]["pgid"] == leader.pid
        leader.kill()
        leader.wait(timeout=10)
        assert agent_lock._identity(child_pid) is not None
        lock = agent_lock.AgentLock(tmp_path)
        lock.acquire()
        assert lock.reaped == leader.pid
        assert agent_lock._identity(child_pid) is None
        lock.release()
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait()
