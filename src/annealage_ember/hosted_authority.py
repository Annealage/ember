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
* ``arm_expiry`` interrupts the turn when its delegation expires, so a turn
  that is still working at that moment makes no further provider requests.
* ``holds`` is what a turn waiting to be delivered must still satisfy then.

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

#: ``(connection, bus, bus.hosted_cancel_generation)`` taken by the read loop the
#: moment a turn frame arrives, in the context of the task that will submit it.
#: An interrupt on ANY connection bumps the bus's generation (the submission
#: lock is project-wide, so one principal's interrupt reaches every waiting
#: turn), and a connection going away closes its own, so a turn still waiting
#: for the lock when either happens takes no grant.
FENCE = contextvars.ContextVar("hosted_turn_fence", default=None)


def fenced_out() -> bool:
    """Whether the turn being submitted was cancelled (interrupt) or its
    connection closed after its frame arrived and before it took the lock."""
    fence = FENCE.get()
    if fence is None:
        return False
    conn, bus, generation = fence
    return bool(conn.closed or bus.hosted_cancel_generation != generation)


def _in_flight(bus, holder) -> bool:
    if bus.hosted_stopping:
        # A revoked turn is still being stopped: no successor until it is
        # confirmed ended (or torn down), or the late stop could hit it.
        return True
    if bus.hosted_turn_live:
        return True
    return bool(holder is not None and holder.turn_in_flight())


def admit(bus, holder, claims, *, owner=None, now: Optional[float] = None) -> Optional[str]:
    """Give ``claims``' principal the bus's turn authority for a turn about to
    be handed to the session, or return why it cannot have it (nothing changed).
    ``owner`` is the connection the delegation arrived on: the grant ends with
    it (``release``)."""
    now = time.time() if now is None else now
    if claims is None or now >= claims.exp:
        return "your access to this project has expired, so this message was not sent"
    if _in_flight(bus, holder):
        if bus.hosted_turn_principal == claims.sub:
            return "your previous message is still being worked on, so this one was not sent; wait for it to finish"
        return "another person's turn is in progress, so this message was not sent; wait for it to finish"
    with bus.hosted_lock:
        bus.hosted_turn_principal = claims.sub
        bus.hosted_turn_owner = owner
        bus.hosted_turn_generation += 1
        GENERATION.set(bus.hosted_turn_generation)
        bus.hosted_turn_ops = tuple(claims.ops)
        bus.hosted_turn_exp = claims.exp
        bus.hosted_turn_secret = secrets.token_urlsafe(32)
        bus.hosted_turn_live = True
    return None


def holds(bus, principal: Optional[str], *, now: Optional[float] = None) -> bool:
    """Whether ``principal``'s authority is still the live, unexpired one."""
    now = time.time() if now is None else now
    return bool(
        principal is not None
        and bus.hosted_turn_live
        and bus.hosted_turn_principal == principal
        and now < bus.hosted_turn_exp
    )


def _begin_stop(bus, session) -> None:
    """Start stopping ``session``'s turn and count it until it has settled."""
    bus.hosted_stopping += 1
    task = asyncio.get_running_loop().create_task(_stop(session))

    def settled(done):
        bus.hosted_stopping -= 1
        done.cancelled() or done.exception()

    task.add_done_callback(settled)


def _stop(session):
    """Stop the turn the grant belonged to: bounded and fail-closed where the
    backend can (``stop_for_revocation``), a plain interrupt otherwise."""
    stop = getattr(session, "stop_for_revocation", None)
    return stop() if stop is not None else session.interrupt()


def checker(bus):
    """A thread-safe ``() -> reason | None`` for the send of ONE turn: bound to
    the grant generation of the task asking (``GENERATION``), and so false for
    this turn's prompt once that grant is released, expired or replaced, even
    if another principal holds a newer one. The backend asks it at the send
    itself, in the thread that makes the provider call, after every await that
    could have let the grant end (recording, image expansion, a busy executor).
    Not set (no grant for this task) refuses."""
    generation = GENERATION.get()

    def check() -> Optional[str]:
        # Under the lock admit/release/expiry take, so the check never sees a
        # half-written grant. It orders the CHECK only, not the provider
        # call that follows it: that is made after the lock is dropped (never
        # held across a blocking call), so a release can still land between the
        # check and the prompt reaching omp, and its interrupt then queues
        # behind the prompt on the backend's single worker.
        with bus.hosted_lock:
            if (
                generation is not None
                and bus.hosted_turn_generation == generation
                and bus.hosted_turn_live
                and time.time() < bus.hosted_turn_exp
            ):
                return None
        return "your access to this project ended before the message was sent to the agent"

    return check


def arm_expiry(bus, session) -> None:
    """Interrupt ``session`` when the authority just admitted expires, unless
    that grant has ended by then (a new turn has a new secret)."""
    handle = getattr(bus, "hosted_expiry_handle", None)
    if handle is not None:
        handle.cancel()
    secret = bus.hosted_turn_secret
    loop = asyncio.get_running_loop()

    def fire():
        bus.hosted_expiry_handle = None
        if bus.hosted_turn_secret != secret or not bus.hosted_turn_live:
            return
        with bus.hosted_lock:
            bus.hosted_turn_live = False
            bus.hosted_turn_ops = ()
            bus.hosted_turn_secret = None
        _begin_stop(bus, session)

    bus.hosted_expiry_handle = loop.call_later(max(0.0, bus.hosted_turn_exp - time.time()), fire)


def release(bus, owner, session) -> None:
    """End the grant ``owner``'s connection holds, because that connection has
    gone: the hosted front closes it when the principal's access is revoked or
    expires, and nothing else tells this process. The turn it was running is
    interrupted and a turn waiting to start is rejected (``holds``), so nothing
    the revoked principal sent is dispatched to the model after this. A
    connection that holds no live grant (the usual idle close) changes nothing."""
    if owner is None or bus.hosted_turn_owner is not owner or not bus.hosted_turn_live:
        return
    handle = getattr(bus, "hosted_expiry_handle", None)
    if handle is not None:
        handle.cancel()
        bus.hosted_expiry_handle = None
    with bus.hosted_lock:
        bus.hosted_turn_live = False
        bus.hosted_turn_ops = ()
        bus.hosted_turn_secret = None
        bus.hosted_turn_owner = None
        bus.hosted_turn_generation += 1
    if session is not None:
        _begin_stop(bus, session)
