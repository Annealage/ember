"""Job-owned hosted authority and per-submission fencing."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from annealage_ember import hosted_authority as authority


def _bus():
    return SimpleNamespace(
        hosted_turn_live=False,
        hosted_turn_ops=(),
        hosted_turn_secret=None,
        hosted_turn_principal=None,
        hosted_turn_generation=0,
        hosted_cancel_generation=0,
        hosted_quiescing=False,
        hosted_stopping=0,
        hosted_fenced=False,
        hosted_worker=None,
        hosted_lock=threading.Lock(),
    )


def _claims(**changes):
    values = {
        "sub": "alice",
        "wsp": "workspace",
        "prj": "project",
        "job": "job",
        "ops": ("project.exec", "project.read"),
        "exp": 4_000_000_000,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_successive_internal_turns_keep_launch_authority_after_expiry(monkeypatch):
    bus = _bus()
    authority.install(bus, _claims(exp=100), now=1)
    assert authority.admit(bus, None, _claims(exp=100), now=1) is None
    secret = bus.hosted_turn_secret
    old = authority.checker(bus)
    bus.hosted_turn_live = False  # terminal event
    bus.hosted_turn_ops = ()
    monkeypatch.setattr(authority.time, "time", lambda: 200)
    assert authority.admit(bus, None, _claims(exp=100)) is not None
    assert authority.admit_internal(bus, None) is None
    assert authority.checker(bus)() is None
    assert old() is not None
    assert bus.hosted_turn_secret != secret
    assert bus.hosted_turn_ops == ("project.exec", "project.read")


@pytest.mark.parametrize(
    "changes",
    [
        {"wsp": "other"},
        {"prj": "other"},
        {"job": "other"},
        {"ops": ("project.read",)},
        {"exp": 1},
    ],
)
def test_caller_cannot_expand_or_switch_worker_authority(changes):
    bus = _bus()
    authority.install(bus, _claims(exp=None))
    assert authority.admit(bus, None, _claims(**changes)) is not None
    assert not bus.hosted_turn_live


def test_caller_turn_operations_are_intersection_with_launch_grant():
    bus = _bus()
    authority.install(bus, _claims(exp=None))
    assert authority.admit(bus, None, _claims(ops=("project.exec", "project.admin"))) is None
    assert bus.hosted_turn_ops == ("project.exec",)


@pytest.mark.parametrize(
    "changes",
    [
        {"wsp": "other"},
        {"prj": "other"},
        {"job": "other"},
        {"ops": ("project.read",)},
        {"exp": 1},
    ],
)
def test_collaborator_still_needs_same_job_live_exec_claims(changes):
    bus = _bus()
    authority.install(bus, _claims(exp=None))
    assert authority.admit(bus, None, _claims(sub="bob", **changes)) is not None
    assert not bus.hosted_turn_live


def test_quiesce_drains_admitted_frames_and_reopens_only_live_worker():
    bus = _bus()
    bus.hosted_frames = 0
    authority.install(bus, _claims(exp=None))
    assert authority.enter_frame(bus)
    authority.quiesce(bus)
    assert not authority.enter_frame(bus)
    assert bus.hosted_frames == 1
    assert authority.admit_internal(bus, None) is not None
    authority.leave_frame(bus)
    assert bus.hosted_frames == 0 and authority.resume(bus)
    assert authority.admit_internal(bus, None) is None
    authority.revoke(bus, None)
    assert not authority.resume(bus)


def test_missing_launch_grant_and_duplicate_install_fail_closed():
    bus = _bus()
    assert authority.admit_internal(bus, None) is not None
    with pytest.raises(ValueError):
        authority.install(bus, _claims(exp=1))
    authority.install(bus, _claims(exp=None))
    with pytest.raises(ValueError):
        authority.install(bus, _claims(exp=None))


def test_other_host_without_worker_grant_retains_delegation_admission():
    bus = _bus()
    bus.hosted_secret_history = []
    assert authority.admit_internal(bus, None) is not None
    assert authority.admit(bus, None, _claims()) is None
    assert bus.hosted_turn_principal == "alice"
    assert bus.hosted_turn_ops == ("project.exec", "project.read")


def test_idle_maintenance_is_fenced_without_granting_a_turn():
    bus = _bus()
    assert authority.maintenance_checker(bus)() is not None
    authority.install(bus, _claims(exp=None))
    check = authority.maintenance_checker(bus)
    assert check() is None
    assert not bus.hosted_turn_live and not bus.hosted_turn_ops
    assert authority.admit_internal(bus, None) is None
    assert check() is not None
    authority.interrupt(bus, None)
    assert check() is not None


@pytest.mark.asyncio
async def test_interrupt_waits_for_stop_and_revocation_is_permanent():
    bus = _bus()
    authority.install(bus, _claims(exp=None))
    assert authority.admit_internal(bus, None) is None
    old = authority.checker(bus)
    done = asyncio.Event()

    class Session:
        async def stop_for_revocation(self):
            await done.wait()
            return True

    authority.interrupt(bus, Session())
    assert old() is not None and bus.hosted_turn_secret is None
    assert authority.admit_internal(bus, None) is not None
    done.set()
    await asyncio.sleep(0.01)
    assert authority.admit_internal(bus, None) is None
    authority.revoke(bus, None)
    assert authority.admit_internal(bus, None) is not None
    assert bus.hosted_fenced and bus.hosted_worker is None


@pytest.mark.asyncio
async def test_uncertain_stop_permanently_fences():
    bus = _bus()
    authority.install(bus, _claims(exp=None))
    assert authority.admit_internal(bus, None) is None

    class Session:
        async def stop_for_revocation(self):
            return False

    authority.interrupt(bus, Session())
    await asyncio.sleep(0.01)
    assert bus.hosted_fenced
    assert authority.admit_internal(bus, None) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fallback", "unknown"])
async def test_unconfirmed_stop_fences_instead_of_admitting_successor(kind):
    bus = _bus()
    authority.install(bus, _claims(exp=None))
    assert authority.admit_internal(bus, None) is None

    class Session:
        async def interrupt(self):
            return None

    session = Session()
    if kind == "unknown":
        session.stop_for_revocation = session.interrupt
    await authority.interrupt(bus, session)
    await asyncio.sleep(0)
    assert bus.hosted_fenced
    assert authority.admit_internal(bus, None) is not None


def test_a_member_other_than_the_owner_starts_a_turn_under_their_own_ops():
    bus = _bus()
    authority.install(bus, _claims())
    bob = _claims(sub="bob", ops=("project.exec",))
    assert authority.admit(bus, None, bob) is None
    assert bus.hosted_turn_principal == "bob"
    assert bus.hosted_turn_ops == ("project.exec",)


def test_a_steer_is_stamped_with_the_running_turns_generation():
    bus = _bus()
    authority.install(bus, _claims())
    assert authority.admit(bus, None, _claims()) is None
    running = bus.hosted_turn_generation
    authority.GENERATION.set(None)
    assert authority.admit(bus, None, _claims(sub="bob")) is None
    assert authority.GENERATION.get() == running
    assert bus.hosted_turn_generation == running
