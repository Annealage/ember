"""Whose authority a hosted agent's running turn holds.

One agent serves a whole project, so every principal's turns share one bus and
the bus carries the authority that tool calls are checked against
(``hosted_turn_ops``/``_exp``/``_secret``/``_live``). That is only sound while
exactly one turn, for one principal, is in flight:

* ``admit`` is the one place a hosted turn takes the authority, and it refuses
  every turn while another is running, queued or waiting for the agent to
  start, the same principal's included. A backend's own queue (and a steer
  into a running turn) would otherwise run a message after the authority it
  was accepted under has ended, with nothing checking it at the provider.
* The delegation's expiry (``exp``) gates admission only. Once a turn is
  admitted it runs until it ends, an explicit ``interrupt`` stops it, or the
  worker is cancelled from outside (the hosted front cancels the job when
  access is revoked). A browser closing, a connection dropping or the
  delegation expiring mid-turn changes nothing about it: the page is a view
  that comes and goes, not what keeps the work alive.
* ``holds`` is what a turn waiting to be delivered must still satisfy.

All of it fails closed.
"""

from __future__ import annotations

import asyncio
import contextvars
import secrets
import time
from typing import Optional

#: The generation of the grant the running task was admitted under. A grant's
#: number is its own: a send stamped with an older one is never made under a
#: newer grant, whoever holds that.
GENERATION = contextvars.ContextVar("hosted_grant_generation", default=None)

#: ``(bus, bus.hosted_cancel_generation)`` taken by the read loop the moment a
#: turn frame arrives, in the context of the task that will submit it. An
#: interrupt on ANY connection bumps the bus's generation (the submission lock
#: is project-wide, so one principal's interrupt reaches every waiting turn), so
#: a turn still waiting for the lock when that happens takes no grant. The
#: connection going away does not: a turn frame that arrived is still run.
FENCE = contextvars.ContextVar("hosted_turn_fence", default=None)


def fenced_out() -> bool:
    """Whether the turn being submitted was cancelled (interrupt) after its
    frame arrived and before it took the lock."""
    fence = FENCE.get()
    if fence is None:
        return False
    bus, generation = fence
    return bus.hosted_cancel_generation != generation


def _in_flight(bus, holder) -> bool:
    if bus.hosted_stopping:
        # A revoked turn is still being stopped: no successor until it is
        # confirmed ended (or torn down), or the late stop could hit it.
        return True
    if bus.hosted_turn_live:
        return True
    return bool(holder is not None and holder.turn_in_flight())


def admit(bus, holder, claims, *, now: Optional[float] = None) -> Optional[str]:
    """Give ``claims``' principal the bus's turn authority for a turn about to
    be handed to the session, or return why it cannot have it (nothing changed).
    ``claims.exp`` is checked here and nowhere after: the turn that is admitted
    lives until it ends."""
    now = time.time() if now is None else now
    if claims is None or now >= claims.exp:
        return "your access to this project has expired, so this message was not sent"
    if bus.hosted_fenced:
        return "this agent could not be shut down safely after access ended, so it takes no more messages until the worker is restarted"
    if _in_flight(bus, holder):
        if bus.hosted_turn_principal == claims.sub:
            return "your previous message is still being worked on, so this one was not sent; wait for it to finish"
        return "another person's turn is in progress, so this message was not sent; wait for it to finish"
    with bus.hosted_lock:
        bus.hosted_turn_principal = claims.sub
        bus.hosted_turn_generation += 1
        GENERATION.set(bus.hosted_turn_generation)
        bus.hosted_turn_ops = tuple(claims.ops)
        bus.hosted_turn_secret = secrets.token_urlsafe(32)
        bus.hosted_turn_live = True
    return None


def holds(bus, principal: Optional[str]) -> bool:
    """Whether ``principal``'s authority is still the live one."""
    return bool(
        principal is not None and bus.hosted_turn_live and bus.hosted_turn_principal == principal
    )


def _begin_stop(bus, session) -> None:
    """Start stopping ``session``'s turn and count it until it has settled."""
    bus.hosted_stopping += 1
    task = asyncio.get_running_loop().create_task(_stop(session))

    def settled(done):
        bus.hosted_stopping -= 1
        # Only an explicit "known stopped" lets the worker go on. A stop that
        # returned False, raised or was cancelled leaves the old process possibly
        # alive: fence the worker for good (a restart clears it).
        if done.cancelled() or done.exception() is not None or done.result() is False:
            bus.hosted_fenced = True

    task.add_done_callback(settled)


def _stop(session):
    """Stop the turn the grant belonged to: bounded and fail-closed where the
    backend can (``stop_for_revocation``), a plain interrupt otherwise."""
    stop = getattr(session, "stop_for_revocation", None)
    return stop() if stop is not None else session.interrupt()


def interrupt(bus, session) -> None:
    """A manual interrupt on a hosted turn: the grant ends and the turn is
    stopped through the same bounded lifecycle as a revocation (abort, terminal
    proof, verified group kill or a permanent fence), not a bare abort whose
    failure nothing would follow up. Every turn still waiting for the
    submission lock is cancelled with it."""
    bus.hosted_cancel_generation += 1
    with bus.hosted_lock:
        bus.hosted_turn_live = False
        bus.hosted_turn_ops = ()
        bus.hosted_turn_secret = None
        bus.hosted_turn_generation += 1
    if session is not None:
        _begin_stop(bus, session)


def checker(bus):
    """A thread-safe ``() -> reason | None`` for the send of ONE turn: bound to
    the grant generation of the task asking (``GENERATION``), and so false for
    this turn's prompt once that grant is interrupted or replaced, even
    if another principal holds a newer one. The backend asks it at the send
    itself, in the thread that makes the provider call, after every await that
    could have let the grant end (recording, image expansion, a busy executor).
    Not set (no grant for this task) refuses."""
    generation = GENERATION.get()

    def check() -> Optional[str]:
        # Under the lock admit/interrupt take, so the check never sees a
        # half-written grant. It orders the CHECK only, not the provider
        # call that follows it: that is made after the lock is dropped (never
        # held across a blocking call), so an interrupt can still land between the
        # check and the prompt reaching omp, and its interrupt then queues
        # behind the prompt on the backend's single worker.
        with bus.hosted_lock:
            if (
                generation is not None
                and bus.hosted_turn_generation == generation
                and bus.hosted_turn_live
            ):
                return None
        return "your access to this project ended before the message was sent to the agent"

    return check
