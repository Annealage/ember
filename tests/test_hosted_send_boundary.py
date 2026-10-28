"""The provider-send boundary: a hosted prompt is checked at the send, after the awaits that can outlast its grant."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from test_omp_session import _started_session

from annealage_ember import hosted_authority
from annealage_ember.session import omp as omp_module
from annealage_ember.session.base import InputState

pytestmark = pytest.mark.asyncio


def _bus(exp):
    return SimpleNamespace(
        hosted_turn_live=True,
        hosted_turn_ops=("project.exec",),
        hosted_turn_exp=exp,
        hosted_turn_secret="s",
        hosted_turn_principal="alice",
        hosted_turn_owner="conn",
        hosted_expiry_handle=None,
    )


async def _submit_during_slow_expansion(monkeypatch, bus, *, while_expanding):
    """Submit one turn whose image expansion is held; run ``while_expanding``
    (the grant ending), then let it finish. Returns the fake client."""
    release = threading.Event()
    started = threading.Event()

    def slow_expand(blocks, cwd):
        started.set()
        release.wait(5)
        return blocks

    monkeypatch.setattr(omp_module.turn_images, "expand_turn_blocks", slow_expand)
    session, fake, recorder, _broker = await _started_session()
    session.send_guard = lambda: hosted_authority.guard(bus)
    try:
        task = asyncio.ensure_future(session.submit_turn([{"type": "text", "text": "go"}]))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        while_expanding(session)
        release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0.05)
        return fake, recorder
    finally:
        release.set()
        await session.close()


async def test_a_prompt_is_sent_while_the_grant_still_holds(monkeypatch):
    fake, _ = await _submit_during_slow_expansion(
        monkeypatch, _bus(time.time() + 60), while_expanding=lambda s: None
    )
    assert [c.message for c in fake.prompt_calls] == ["go"]


async def test_a_grant_released_during_image_expansion_is_not_sent(monkeypatch):
    bus = _bus(time.time() + 60)
    fake, recorder = await _submit_during_slow_expansion(
        monkeypatch,
        bus,
        while_expanding=lambda s: hosted_authority.release(bus, "conn", None),
    )
    assert fake.prompt_calls == []
    rejected = [e for e in recorder.all if isinstance(e, InputState) and e.state == "rejected"]
    assert rejected and "access" in rejected[0].detail


async def test_a_grant_that_expires_during_image_expansion_is_not_sent(monkeypatch):
    bus = _bus(time.time() + 0.3)
    fake, _ = await _submit_during_slow_expansion(
        monkeypatch, bus, while_expanding=lambda s: time.sleep(0.4)
    )
    assert fake.prompt_calls == []
