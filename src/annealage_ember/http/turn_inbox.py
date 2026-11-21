"""Messages the agent cannot take yet, kept and sent in order.

A message refused with ``refusal.Busy`` (a passing state) waits here instead of
being reported as not sent. One task per bus drains the inbox first in, first
out: it tries the head, waits while that is still ``HOLD``, and moves on once
the head is accepted or refused for a fault. An interrupt, a checkpoint or the
app stopping empties it, and each message so dropped is refused to its page.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
from collections import deque

#: What an attempt returns for a message that still cannot go.
HOLD = object()
POLL_SECONDS = 0.5


class _Entry:
    __slots__ = ("attempt", "client_id", "reject", "context")

    def __init__(self, attempt, client_id, reject):
        self.attempt = attempt
        self.client_id = client_id
        self.reject = reject
        # The hosted fence a message was received under is its own.
        self.context = contextvars.copy_context()


class TurnInbox:
    def __init__(self):
        self._entries = deque()
        self._task = None

    def __len__(self):
        return len(self._entries)

    def has(self, client_id):
        return client_id is not None and any(e.client_id == client_id for e in self._entries)

    def add(self, attempt, client_id, reject):
        """Keep a message. ``attempt()`` is awaited again until it is not
        ``HOLD``; ``reject(reason)`` tells the page when it is dropped."""
        self._entries.append(_Entry(attempt, client_id, reject))
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())
            self._task.add_done_callback(self._done)

    async def drop(self, reason):
        """Refuse every waiting message, for a reason that ends the wait."""
        entries, self._entries = list(self._entries), deque()
        for entry in entries:
            await _quietly(entry.reject, reason)

    async def stop(self, reason):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        await self.drop(reason)

    def _done(self, task):
        if self._task is task:
            self._task = None
        if not task.cancelled() and task.exception() is not None:
            sys.stderr.write("warning: the held turn queue failed: %r\n" % (task.exception(),))

    async def _run(self):
        while self._entries:
            entry = self._entries[0]
            try:
                attempt = entry.context.run(asyncio.ensure_future, entry.attempt())
                result = await attempt
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sys.stderr.write("warning: a held turn failed: %r\n" % (exc,))
                result = None
            if result is HOLD:
                await asyncio.sleep(POLL_SECONDS)
                continue
            if self._entries and self._entries[0] is entry:
                self._entries.popleft()


async def _quietly(call, *args):
    try:
        await call(*args)
    except Exception as exc:
        sys.stderr.write("warning: could not tell a page its message was dropped: %r\n" % (exc,))


def for_bus(bus):
    inbox = getattr(bus, "turn_inbox", None)
    if inbox is None:
        inbox = bus.turn_inbox = TurnInbox()
    return inbox


def pending(bus):
    inbox = getattr(bus, "turn_inbox", None)
    return len(inbox) if inbox is not None else 0


async def stop_for(bus, reason):
    """Drop every waiting message of ``bus`` (``None``: nothing to drop)."""
    inbox = getattr(bus, "turn_inbox", None)
    if inbox is not None:
        await inbox.stop(reason)
