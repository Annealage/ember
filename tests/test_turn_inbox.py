"""Held messages: sent in order, dropped with a reason when the wait ends."""

import asyncio

import pytest

from annealage_ember.http import turn_inbox
from annealage_ember.http.turn_inbox import HOLD, TurnInbox


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(turn_inbox, "POLL_SECONDS", 0.01)


def _entry(inbox, name, log, gate, dropped=None):
    async def attempt():
        if not gate[0]:
            return HOLD
        log.append(name)

    async def reject(reason):
        (dropped if dropped is not None else []).append((name, reason))

    inbox.add(attempt, name, reject)


@pytest.mark.asyncio
async def test_held_messages_go_in_the_order_they_arrived_once_the_gate_opens():
    inbox, log, gate = TurnInbox(), [], [False]
    for name in "abc":
        _entry(inbox, name, log, gate)
    await asyncio.sleep(0.05)
    assert log == [] and len(inbox) == 3 and inbox.has("b")
    gate[0] = True
    await asyncio.sleep(0.1)
    assert log == ["a", "b", "c"] and len(inbox) == 0 and not inbox.has("a")


@pytest.mark.asyncio
async def test_stop_refuses_every_waiting_message_and_sends_none():
    inbox, log, gate, dropped = TurnInbox(), [], [False], []
    _entry(inbox, "a", log, gate, dropped)
    _entry(inbox, "b", log, gate, dropped)
    await inbox.stop("stopped")
    gate[0] = True
    await asyncio.sleep(0.05)
    assert log == [] and len(inbox) == 0
    assert dropped == [("a", "stopped"), ("b", "stopped")]


@pytest.mark.asyncio
async def test_a_message_that_fails_is_dropped_and_the_next_still_goes():
    inbox, log = TurnInbox(), []

    async def boom():
        raise RuntimeError("x")

    async def reject(reason):
        pass

    inbox.add(boom, "a", reject)
    _entry(inbox, "b", log, [True])
    await asyncio.sleep(0.1)
    assert log == ["b"] and len(inbox) == 0
