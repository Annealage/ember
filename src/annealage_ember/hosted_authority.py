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
import secrets
import time
from typing import Optional


def _in_flight(bus, holder) -> bool:
    if bus.hosted_turn_live:
        return True
    return bool(holder is not None and holder.turn_in_flight())


def admit(bus, holder, claims, *, now: Optional[float] = None) -> Optional[str]:
    """Give ``claims``' principal the bus's turn authority for a turn about to
    be handed to the session, or return why it cannot have it (nothing changed)."""
    now = time.time() if now is None else now
    if claims is None or now >= claims.exp:
        return "your access to this project has expired, so this message was not sent"
    if _in_flight(bus, holder):
        if bus.hosted_turn_principal == claims.sub:
            return "your previous message is still being worked on, so this one was not sent; wait for it to finish"
        return "another person's turn is in progress, so this message was not sent; wait for it to finish"
    bus.hosted_turn_principal = claims.sub
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
        bus.hosted_turn_live = False
        bus.hosted_turn_ops = ()
        bus.hosted_turn_secret = None
        task = loop.create_task(session.interrupt())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    bus.hosted_expiry_handle = loop.call_later(max(0.0, bus.hosted_turn_exp - time.time()), fire)
