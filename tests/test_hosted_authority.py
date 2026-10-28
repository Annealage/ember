"""One agent serves a project's principals; its turn authority has one owner at a time."""

import threading
from types import SimpleNamespace

import pytest

from annealage_ember import hosted_authority


def _bus():
    return SimpleNamespace(
        hosted_turn_live=False,
        hosted_turn_ops=(),
        hosted_turn_exp=0,
        hosted_turn_secret=None,
        hosted_turn_principal=None,
        hosted_turn_generation=0,
        hosted_stopping=0,
        hosted_lock=threading.Lock(),
    )


def _holder(in_flight):
    return SimpleNamespace(turn_in_flight=lambda: in_flight[0])


def _claims(sub, ops=("project.exec",), exp=4_000_000_000):
    return SimpleNamespace(sub=sub, ops=ops, exp=exp)


def test_another_principal_cannot_take_the_authority_while_a_turn_is_in_flight():
    bus, running = _bus(), [False]
    assert (
        hosted_authority.admit(
            bus, _holder(running), _claims("alice", ("project.exec", "project.write"))
        )
        is None
    )
    running[0] = True
    before = (bus.hosted_turn_ops, bus.hosted_turn_exp, bus.hosted_turn_secret)
    reason = hosted_authority.admit(
        bus, _holder(running), _claims("bob", ("project.read",), exp=5_000_000_000)
    )
    assert "another person's turn" in reason
    assert bus.hosted_turn_principal == "alice"
    assert (bus.hosted_turn_ops, bus.hosted_turn_exp, bus.hosted_turn_secret) == before


def test_a_queued_or_deferred_turn_still_blocks_another_principal_after_the_grant_ended():
    bus, queued = _bus(), [False]
    assert hosted_authority.admit(bus, _holder(queued), _claims("alice")) is None
    queued[0] = True  # the first turn ended, one is still queued
    bus.hosted_turn_live, bus.hosted_turn_ops = False, ()
    assert hosted_authority.admit(bus, _holder(queued), _claims("bob")) is not None
    assert "previous message" in hosted_authority.admit(bus, _holder(queued), _claims("alice"))


def test_a_turn_nobody_owns_waits_rather_than_being_adopted():
    bus = _bus()  # in flight, but no principal recorded
    assert hosted_authority.admit(bus, _holder([True]), _claims("bob")) is not None


def test_the_owner_is_refused_behind_their_own_turn_and_the_grant_is_untouched():
    bus, running = _bus(), [False]
    hosted_authority.admit(bus, _holder(running), _claims("alice", ("project.exec",)))
    before = (bus.hosted_turn_ops, bus.hosted_turn_secret)
    running[0] = True
    reason = hosted_authority.admit(
        bus, _holder(running), _claims("alice", ("project.exec", "project.admin"))
    )
    assert "previous message" in reason
    assert (bus.hosted_turn_ops, bus.hosted_turn_secret) == before


def test_anyone_is_admitted_when_nothing_is_in_flight_and_gets_a_fresh_grant():
    bus, running = _bus(), [False]
    hosted_authority.admit(bus, _holder(running), _claims("alice"))
    first = bus.hosted_turn_secret
    bus.hosted_turn_live = False
    assert hosted_authority.admit(bus, _holder(running), _claims("bob", ("project.read",))) is None
    assert bus.hosted_turn_principal == "bob" and bus.hosted_turn_ops == ("project.read",)
    assert bus.hosted_turn_secret != first


def test_an_expired_or_missing_delegation_is_refused_and_changes_nothing():
    bus = _bus()
    assert "expired" in hosted_authority.admit(bus, None, _claims("alice", exp=1))
    assert hosted_authority.admit(bus, None, None) is not None
    assert bus.hosted_turn_live is False and bus.hosted_turn_principal is None


def test_a_waiting_turn_is_delivered_only_under_its_own_live_unexpired_authority():
    bus = _bus()
    hosted_authority.admit(bus, None, _claims("alice", exp=4_000_000_000))
    assert hosted_authority.holds(bus, "alice", now=1_000)
    assert not hosted_authority.holds(bus, "alice", now=4_000_000_000)  # expired
    assert not hosted_authority.holds(bus, "bob", now=1_000)
    assert not hosted_authority.holds(bus, None, now=1_000)
    bus.hosted_turn_live = False
    assert not hosted_authority.holds(bus, "alice", now=1_000)


@pytest.mark.asyncio
async def test_release_stops_a_backend_with_the_bounded_fail_closed_stop():
    import asyncio

    calls = []

    class Session:
        async def interrupt(self):
            calls.append("interrupt")

        async def stop_for_revocation(self):
            calls.append("stop_for_revocation")

    bus = _bus()
    hosted_authority.admit(bus, None, _claims("alice"), owner="conn")
    hosted_authority.release(bus, "conn", Session())
    await asyncio.sleep(0.05)
    assert calls == ["stop_for_revocation"]
