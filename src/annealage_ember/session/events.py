"""EventLog: monotonic seq, a bounded in-memory ring, and an optional
append-only ``events.jsonl``.

Every server-to-browser event carries a seq number that never repeats and
never goes backwards for a given log (plan section 3.4). A reconnecting
browser sends the last seq it saw; this module replays what happened since
then from the in-memory ring, or from the file where the ring no longer
reaches (a long gap, or a restart), and never silently drops a difference
it cannot fill.

``EventLog`` takes an optional ``path``: with one it persists to
``events.jsonl`` beneath a session's own ``<state dir>/sessions/<sid>/``
directory, and without one it is a pure in-memory ring, which is what lets
it be exercised directly against a ``tmp_path`` with no session, served
directory, or agent involved at all.

This is not the exchange-file threat model ``files.py``'s
``safe_fixed_file`` and ``read_fixed_file`` defend: those
files sit in a served project directory an outside party can also write
into, so every open there re-validates identity against a race. An
``events.jsonl`` lives inside the product's own ``<state dir>/`` control
directory, which nothing but this process ever writes to, so there is no
name to race and no symlink substitution to guard against; the file is
opened once, by name, and held for the log's lifetime.

``read_records``, ``render_transcript`` and ``export_transcript``, below,
turn one session's ``events.jsonl`` back into a document a person can read
or archive. They live here rather than in a separate module because a
transcript is a projection of exactly the file ``EventLog`` writes, and
reading it back is the direct counterpart to appending to it.
``export_transcript`` writes through ``files.create_review_file``, which
gives a generated destination in a served project directory the same
containment ``files.create_image_file`` gives a model-supplied one: the
directory is created by this process on first use, but the served project
directory around it is not otherwise trusted, so a ``review/`` replaced by
a symlink is refused rather than followed.

A rendered transcript reads as the conversation, the human's side included.
``http/ws.py`` logs each turn the human sends as a ``user_turn`` event
(``session/base.py``'s ``UserTurn``) before the session sees it, carrying
the blocks exactly as the page sent them: what the human typed, and an
attached image as its path. The product's own notes, which
``ViewerBus.begin_turn`` puts in front of a turn on its way to the model, are
not part of it, so a transcript shows what the human said rather than
everything the model was sent.
"""

from __future__ import annotations

import asyncio
import bisect
import collections
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .. import files, product, sessions

# Bounded history kept in memory for a reconnect to replay without a disk
# read. 500 events comfortably outlasts a normal reconnect gap (dropped
# WiFi, laptop lid, a phone locking) without growing without bound across
# a long session; a gap wider than that is read back from the file, or,
# for a log with no file, reported rather than guessed at, via
# Replay.truncated below.
RING_SIZE = 500


@dataclasses.dataclass(frozen=True)
class Replay:
    """The result of asking an ``EventLog`` to replay from a client's ``last_seq``.

    ``events`` is every ``(seq, wire_dict)`` pair newer than ``last_seq`` that
    this log can still produce, in seq order, ready to send straight back
    over the socket: the ring's own, preceded, when the client is further
    back than the ring reaches, by the file's records for the gap (see
    ``EventLog.replay``). ``truncated`` is True only when some of that
    history is unavailable: the log has no file (viewer-only mode's, or a
    test's) and the ring has already dropped events the client is missing,
    or the file could not be read back as far as the ring reaches. The
    caller must then say so rather than replay a partial history that looks
    complete. ``events`` is still populated in that case with whatever is
    available, since a gap in the oldest history is no reason to also
    withhold the newer events.

    ``through`` is the log's ``current_seq`` when the replay was taken:
    everything up to it is in ``events`` or reported missing, so it is where
    a following replay (``http/ws.py``'s catch-up) starts, whatever was
    appended while this one was being read or sent.
    """

    events: List[Tuple[int, dict]]
    truncated: bool
    through: int = 0


#: The event kinds whose ``turn`` numbers a conversation turn (``EventLog``
#: reads a resumed session's turns from these alone). ``user_turn`` is one,
#: so a turn the human sent that got no reply at all (the process was
#: killed first) still counts and is closed as interrupted on the next start.
_TURN_KINDS = frozenset(("user_turn", "text_delta", "thinking_delta", "tool_use", "turn_end"))

#: The streamed-text kinds ``_coalesce`` joins, each only with its own kind.
_DELTA_KINDS = ("text_delta", "thinking_delta")


@dataclasses.dataclass(frozen=True)
class HistoryPage:
    """One immutable, chronological page, independent of the live cursor."""

    events: List[Tuple[int, dict]]
    before: Optional[int]
    has_more: bool
    through: int
    pending_request_ids: tuple = ()



class EventLog:
    """Append-only event history for one session's lifetime.

    ``path``, when given, is opened once for appending (``O_APPEND`` makes
    each write atomic with respect to any other append to the same file
    descriptor, which matters only for the "one process, one writer"
    invariant this class itself maintains: nothing here defends against a
    second process writing the same path, that is ``<state dir>/lock``'s job,
    owned elsewhere). Re-opening the same path in a fresh ``EventLog``
    picks up numbering where the file left off, so a restarted process
    (or, in tests, a second ``EventLog`` standing in for one) never
    reissues a seq a client may already have.
    """

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path is not None else None
        self._ring: collections.deque = collections.deque(maxlen=RING_SIZE)
        self._seq = 0
        self._fd: Optional[int] = None
        # One entry per non-overlapping group of turns: (first seq, byte
        # offset, number of turns preceding it). Queued/overlapping turns
        # share a group so a page never cuts a reply in half.
        self._history_groups: list = []
        # Keep old cursors valid if a later result merges turn groups. They
        # still bound a complete transcript at the snapshot that issued them.
        self._history_boundaries: dict = {}
        self._history_turns: dict = {}
        self._history_tools: dict = {}
        self._history_open: set = set()
        self._history_floor = 1
        self._history_offset = 0
        self._file_size = 0
        self._pending_history: dict = {}
        #: The highest turn number in the file, and every turn that has no
        #: ``turn_end`` there (a process killed mid-turn): where a resumed
        #: session's numbering continues and which turns ``app.create_app``
        #: closes, so a new turn never reuses the number of one the page
        #: already shows from history, nor does history look like it runs.
        self.last_turn = 0
        self.unfinished_turns: tuple = ()
        #: ``[(turn, its user_turn wire event)]`` of unfinished turns whose input
        #: never reached the backend; see ``_recover_seq``.
        self.undelivered_turns: tuple = ()
        #: Every ``permission_request`` in the file with no
        #: ``permission_resolved`` after it: asked by a process that died
        #: before anyone answered, which ``app.create_app`` closes, so a page
        #: replaying the history is not shown a card nobody can answer.
        self.unresolved_requests: tuple = ()
        #: The figures of the file's last ``usage`` event (``Usage.snapshot``'s
        #: shape), or ``None``: what the conversation had used when the last
        #: process stopped, which the ``hello`` reports until the backend
        #: reports afresh. Every ``usage`` event is the whole conversation's,
        #: so the last one is the answer, with nothing summed.
        self.last_usage: Optional[dict] = None
        #: Called with each appended event's wire dict, after it is recorded:
        #: every event a page sees passes through here, whichever of the
        #: app's publishers sent it, so this is where the app's status
        #: summary (``AgentHolder``) watches them. One that raises is
        #: reported and never stops the append.
        self.observers: list = []
        self._request_usage: dict = {}
        if self._path is not None:
            self._seq = self._recover_seq()
            self._fd = os.open(str(self._path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def _recover_seq(self) -> int:
        """The highest seq already written to ``self._path``, or 0, noting
        the turns on the way (``last_turn``, ``unfinished_turns``).

        A new ``EventLog`` for a path that already has content must not
        restart numbering at 0: a client that saw seq 50 before a restart
        and reconnects with ``last_seq=50`` would otherwise be replayed
        seq 1 through 50 again, duplicating events it has already
        rendered. A malformed trailing line, from a process killed mid
        write, is skipped rather than raising: the recovered seq only
        needs to be at least as high as anything a client could have
        already been sent, and a torn last line was never sent to one
        either, so skipping it costs nothing a client could have seen.
        """
        if not self._path.exists():
            return 0
        highest = 0
        started, ended = set(), set()
        human_turns: dict = {}  # turn -> its user_turn event
        input_states: dict = {}  # turn -> every input_state seen for it
        requests: dict = {}  # request_id -> None, in the order they were asked
        with open(self._path, "rb") as f:
            offset = 0
            for line in f:
                line_offset = offset
                offset += len(line)
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                seq = record.get("seq")
                if isinstance(seq, int) and seq > highest:
                    highest = seq
                event = record.get("event")
                if not isinstance(event, dict):
                    continue
                if isinstance(seq, int) and not isinstance(seq, bool):
                    self._index_history(seq, event, line_offset)
                kind = event.get("kind")
                if kind == "request_usage":
                    event_id = event.get("event_id")
                    if isinstance(event_id, str):
                        self._request_usage[event_id] = (
                            _usage_identity(event),
                            record.get("seq", highest),
                        )
                if kind == "usage":
                    self.last_usage = {
                        key: event.get(key)
                        for key in (
                            "cost_usd",
                            "tokens",
                            "context",
                            "usage_epoch",
                            "usage_source",
                        )
                    }
                    continue
                if kind == "session_reset":
                    # A new conversation began after it: that usage is not its.
                    self.last_usage = None
                    started.clear()
                    ended.clear()
                    requests.clear()
                    human_turns.clear()
                    input_states.clear()
                request_id = event.get("request_id")
                if kind == "permission_request" and isinstance(request_id, str):
                    requests[request_id] = None
                elif kind == "permission_resolved":
                    requests.pop(request_id, None)
                if kind == "user_turn" and isinstance(event.get("turn"), int):
                    human_turns[event["turn"]] = event
                elif kind == "input_state" and isinstance(event.get("turn"), int):
                    input_states.setdefault(event["turn"], set()).add(event.get("state"))
                if kind not in _TURN_KINDS:
                    continue
                turn = event.get("turn")
                if isinstance(turn, int) and not isinstance(turn, bool):
                    started.add(turn)
                    if kind == "turn_end":
                        ended.add(turn)
            self._file_size = offset
        self.last_turn = max(started, default=0)
        self.unfinished_turns = tuple(sorted(started - ended))
        self.unresolved_requests = tuple(requests)
        # A turn the human sent that its backend never took: received or queued
        # and not acknowledged (or refused, or uncertain), and not itself a
        # redelivery (one attempt per turn, so a turn that crashes the agent
        # cannot loop). The app re-sends these once the agent is back.
        self.undelivered_turns = tuple(
            (turn, human_turns[turn])
            for turn in sorted(started - ended)
            if turn in human_turns
            and input_states.get(turn, set()) & {"received", "queued"}
            and not input_states.get(turn, set()) & {"provider_ack", "uncertain", "rejected"}
            and human_turns[turn].get("redelivery_of") is None
        )
        return highest

    @property
    def current_seq(self) -> int:
        """The seq of the most recently appended event, or 0 if none yet.

        This is what a ``hello`` frame reports as its own ``seq``: the
        point in the stream a freshly connected client is starting from,
        with nothing before it to replay.
        """
        return self._seq

    def append(self, event) -> int:
        """Assign the next seq to ``event`` and record it. Returns that seq.

        ``event`` is anything with a ``to_wire()`` method returning a
        JSON-able dict (``session.base.AgentEvent`` and its subclasses);
        this module has no other dependency on that class, so a caller
        with some other object shaped the same way works identically.
        """
        wire = event.to_wire()
        if wire.get("kind") == "request_usage":
            event_id = wire.get("event_id")
            if isinstance(event_id, str):
                existing = self._request_usage.get(event_id)
                if existing is not None:
                    previous, previous_seq = existing
                    if previous != _usage_identity(wire):
                        sys.stderr.write(
                            "warning: conflicting request_usage event for %s; duplicate ignored\n"
                            % event_id
                        )
                    return previous_seq
                self._request_usage[event_id] = (_usage_identity(wire), self._seq + 1)
        self._seq += 1
        seq = self._seq
        self._ring.append((seq, wire))
        if self._fd is not None:
            line = (json.dumps({"seq": seq, "event": wire}) + "\n").encode("utf-8")
            os.write(self._fd, line)
            offset = self._file_size
            self._file_size += len(line)
        else:
            offset = 0
        self._index_history(seq, wire, offset)
        for observe in self.observers:
            try:
                observe(wire)
            except Exception as exc:
                sys.stderr.write("warning: an event log observer failed: %r\n" % (exc,))
        return seq

    def _index_history(self, seq: int, event: dict, offset: int) -> None:
        kind = event.get("kind")
        if kind == "session_reset":
            self._history_groups.clear()
            self._history_boundaries.clear()
            self._history_turns.clear()
            self._history_tools.clear()
            self._history_open.clear()
            self._pending_history.clear()
            self._history_floor = seq
            self._history_offset = offset
        request_id = event.get("request_id")
        if kind == "permission_request" and isinstance(request_id, str):
            self._pending_history[request_id] = (seq, event)
        elif kind == "permission_resolved":
            self._pending_history.pop(request_id, None)
        if kind == "tool_result":
            turn = self._history_tools.pop(event.get("tool_use_id"), None)
        elif kind in _TURN_KINDS:
            turn = event.get("turn")
            if kind == "tool_use" and isinstance(event.get("tool_use_id"), str):
                self._history_tools[event["tool_use_id"]] = turn
        else:
            return
        if not isinstance(turn, int) or isinstance(turn, bool):
            return
        if turn not in self._history_turns:
            if not self._history_open:
                self._history_groups.append((seq, offset, len(self._history_turns)))
                self._history_boundaries[seq] = len(self._history_turns)
            self._history_turns[turn] = seq
            if kind != "turn_end":
                self._history_open.add(turn)
        else:
            # A late event for an older turn overlaps every group after its
            # first event. Merge those boundaries rather than split that turn.
            first = self._history_turns[turn]
            i = bisect.bisect_right(
                self._history_groups, (first, float("inf"), float("inf"))
            ) - 1
            del self._history_groups[i + 1:]
        if kind == "turn_end":
            self._history_open.discard(turn)

    def history_pending(self) -> HistoryPage:
        """A pending-only snapshot when a transcript read is unavailable."""
        return HistoryPage(
            events=list(self._pending_history.values()),
            before=None,
            has_more=False,
            through=self._seq,
            pending_request_ids=tuple(self._pending_history),
        )

    async def history_async(self, before: Optional[int] = None, limit: int = 20) -> HistoryPage:
        """Read up to ``limit`` complete recent turns off the event loop.

        Turn boundaries and byte offsets are indexed during append/recovery,
        never by rescanning the file for a page. Overlapping turns are an
        indivisible group and may make a page larger than the requested limit.
        Everything needed to read is captured before the first await.
        """
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("history limit must be an integer from 1 to 50")
        through = self._seq
        upper = through + 1 if before is None else before
        if type(upper) is not int or not self._history_floor <= upper <= through + 1:
            raise ValueError("history cursor is outside the current conversation")
        groups = self._history_groups
        end = bisect.bisect_left(groups, (upper,))
        if before is not None and upper != self._history_floor:
            if upper not in self._history_boundaries:
                raise ValueError("history cursor is not a page boundary")
        if not groups:
            # Product notifications before the first conversation turn are
            # not a transcript, however many have accumulated in the log.
            return (
                self.history_pending()
                if before is None
                else HistoryPage([], None, False, through)
            )
        count = (
            len(self._history_turns)
            if before is None
            else self._history_boundaries.get(upper, 0)
        )
        target = max(0, count - limit)
        start = end - 1
        while start > 0 and groups[start][2] > target:
            start -= 1
        if not end:
            lower, offset = self._history_floor, self._history_offset
        elif start == 0:
            lower, offset = self._history_floor, self._history_offset
        else:
            lower, offset, _ = groups[start]
        pending = tuple(self._pending_history.items()) if before is None else ()
        ring = [(seq, event) for seq, event in self._ring if lower <= seq < upper]
        oldest = self._ring[0][0] if self._ring else through + 1
        if lower < upper and lower < oldest:
            records = await asyncio.to_thread(self._read_history, offset, lower, upper)
        else:
            records = _coalesce(ring)
        # An old unresolved request is state, not a reason to render all of
        # the intervening transcript. Retain its original seq and payload.
        extras = [record for _, record in pending if record[0] < lower]
        return HistoryPage(
            events=extras + records,
            before=lower if lower < upper else None,
            has_more=lower > self._history_floor,
            through=through,
            pending_request_ids=tuple(request_id for request_id, _ in pending),
        )

    def _read_history(self, offset: int, lower: int, upper: int) -> List[Tuple[int, dict]]:
        if self._path is None:
            raise ValueError("older history is no longer kept")
        records = []
        reached = lower - 1
        try:
            with open(self._path, "rb") as stream:
                stream.seek(offset)
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    seq, event = record.get("seq"), record.get("event")
                    if type(seq) is not int or not isinstance(event, dict):
                        continue
                    if seq >= upper:
                        break
                    if seq < lower:
                        continue
                    if seq != reached + 1:
                        raise ValueError("history has an unavailable event range")
                    reached = seq
                    records.append((seq, event))
        except OSError as exc:
            raise ValueError("history file is unavailable") from exc
        if reached != upper - 1:
            raise ValueError("history has an unavailable event range")
        return _coalesce(records)

    def replay(self, last_seq: Optional[int]) -> Replay:
        """Everything newer than ``last_seq``, in seq order, plus whether
        any of it is unavailable. ``last_seq=None`` means the client has no
        prior history at all (a first-ever connection, a reloaded page),
        treated the same as 0.

        The ring answers for what it holds. What it no longer holds, or
        never held (a restarted log starts with an empty ring: see
        ``_recover_seq``), comes from the file: the records after
        ``last_seq`` and older than the ring's oldest, with each run of one
        turn's ``text_delta`` events joined into one (``_coalesce``), so a
        long session's history is a few frames per turn rather than one per
        streamed chunk. The ring is copied and the file's bound fixed in one
        step (``_plan``), so the file's part ends exactly where the ring's
        begins whenever the file is read: an event appended meanwhile is in
        neither, and ``Replay.through`` says where the next replay starts.
        """
        plan = self._plan(last_seq)
        gap = self._read_between(plan.after, plan.before) if plan.before is not None else None
        return plan.replay(gap)

    async def replay_async(self, last_seq: Optional[int]) -> Replay:
        """``replay``, with the file read in a worker thread, so a long
        history does not stall every stream on the event loop while one tab
        connects. The file's part is bounded by the ring's oldest seq at the
        moment of the call, and every record below that is already whole in
        the file (``append`` writes the line before it returns), so appends
        while the thread reads change nothing it returns."""
        plan = self._plan(last_seq)
        gap = None
        if plan.before is not None:
            gap = await asyncio.to_thread(self._read_between, plan.after, plan.before)
        return plan.replay(gap)

    def _plan(self, last_seq: Optional[int]) -> "_ReplayPlan":
        """What a replay from ``last_seq`` is made of, fixed at one moment:
        nothing here waits, so the ring copy, the file's bound and
        ``through`` all describe the same point in the stream."""
        if last_seq is None or last_seq < 0 or last_seq > self._seq:
            # A last_seq higher than any seq this log has issued came from a
            # different run of the log (most likely a new session after a
            # restart, counting up from a lower seq): the client has none of
            # this log's events, so it is answered as a fresh one, rather
            # than with an empty reply that would read as "already caught
            # up". A negative one is no position at all.
            last_seq = 0
        ring = [(seq, wire) for seq, wire in self._ring if seq > last_seq]
        # The first seq the ring can answer for; with an empty ring, the next
        # one to be issued.
        oldest = self._ring[0][0] if self._ring else self._seq + 1
        # Past the ring's reach, everything between last_seq and oldest fell
        # off it (or was written by an earlier process). It is not silently
        # skippable: it comes from the file, and whatever the file cannot
        # supply is reported rather than handed over as a history with a
        # hole in it.
        before = oldest if last_seq < oldest - 1 else None
        return _ReplayPlan(after=last_seq, before=before, ring=ring, through=self._seq)

    def _read_between(self, after: int, before: int) -> "_ReplayGap":
        """The file's records with ``after < seq < before``, coalesced, and
        whether any sequence in that interval is unavailable.

        The file is in seq order (this class is its one writer), so reading
        stops at the first record the ring already holds. Every record below
        the ring's oldest is already in the file: ``append`` writes the line
        in the same call that puts the event in the ring.
        """
        if self._path is None:
            return _ReplayGap([], truncated=True)
        records = []
        reached = after
        truncated = False
        for record in read_records(self._path):
            seq = record["seq"]
            if seq >= before:
                break
            if seq > after:
                if seq != reached + 1:
                    truncated = True
                reached = seq
                records.append((seq, record["event"]))
        return _ReplayGap(_coalesce(records), truncated or reached != before - 1)

    def close(self) -> None:
        """Close the append-only file descriptor, if one is open.

        Idempotent, so a caller that closes explicitly and a shutdown
        path that closes again defensively do not double-close a
        descriptor number the OS may since have reissued to something
        else entirely.
        """
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


@dataclasses.dataclass(frozen=True)
class _ReplayGap:
    events: List[Tuple[int, dict]]
    truncated: bool


@dataclasses.dataclass(frozen=True)
class _ReplayPlan:
    """One replay's parts, fixed by ``EventLog._plan``: the ring's events
    after ``after``, and, when ``before`` is set, the file's records with
    ``after < seq < before`` still to read."""

    after: int
    before: Optional[int]
    ring: List[Tuple[int, dict]]
    through: int

    def replay(self, gap: Optional[_ReplayGap]) -> Replay:
        if self.before is None:
            return Replay(events=self.ring, truncated=False, through=self.through)
        return Replay(
            events=gap.events + self.ring, truncated=gap.truncated, through=self.through
        )


def _usage_identity(event: dict) -> dict:
    """Compare positional usage payloads without volatile observation time."""
    return {key: value for key, value in event.items() if key != "ts"}

def _coalesce(records: List[Tuple[int, dict]]) -> List[Tuple[int, dict]]:
    """``records`` with each run of consecutive same-turn ``text_delta``
    events joined into one, which carries the run's joined text and its last
    seq; everything else passes through untouched.

    The same merge ``viewers.ViewerRegistry``'s writer makes on a live
    connection, for the same reasons: the page appends a delta's text to its
    turn whatever size it is, so one joined delta renders exactly as the run
    did, and the last seq is the one a client may resume from, since the
    joined text already includes everything up to it.
    """
    out: List[Tuple[int, dict]] = []
    run: List[str] = []  # the texts of the delta run out[-1] stands for
    cut = [False]  # whether any thinking delta in the run was truncated

    def close_run():
        if len(run) > 1 or cut[0]:
            seq, event = out[-1]
            joined = dict(event, text="".join(run))
            if cut[0]:
                joined["truncated"] = True
            out[-1] = (seq, joined)
        run.clear()
        cut[0] = False

    for seq, event in records:
        kind = event.get("kind")
        is_delta = kind in _DELTA_KINDS
        if (run and is_delta and kind == out[-1][1].get("kind")
                and event.get("turn") == out[-1][1].get("turn")):
            run.append(event.get("text", ""))
            cut[0] = cut[0] or bool(event.get("truncated"))
            out[-1] = (seq, out[-1][1])
            continue
        close_run()
        out.append((seq, event))
        if is_delta:
            run.append(event.get("text", ""))
            cut[0] = bool(event.get("truncated"))
    close_run()
    return out


# ---------------------------------------------------------------------------
# Transcript rendering and export: turning an events.jsonl back into a
# document, and writing that document under review/.
# ---------------------------------------------------------------------------


# The two shapes a transcript can be written in: prose meant to be read, or
# the underlying event records themselves, one JSON object per line, for a
# caller that wants the wire shapes rather than a rendering of them.
TRANSCRIPT_FORMATS = ("markdown", "jsonl")

# How much of a session's history a transcript shows. "text" is the
# conversation as a person would read it: what the human sent, what the
# agent said, and which tools it named. "full" adds everything else
# events.jsonl carries: each tool's input and result, each permission
# request's outcome, and the cost of each turn.
TRANSCRIPT_INCLUDE = ("text", "full")

# The least inclusive TRANSCRIPT_INCLUDE level at which each AgentEvent kind
# appears in a transcript. A kind absent from this table never appears in a
# transcript at any level: review_changed, a product's own events (Mesh's
# models_changed), pause_changed and viewer_primary describe the browser's
# view of a running server, not the conversation, and agent_status,
# session_reset, agent_error and usage describe the session's own lifecycle
# and bookkeeping rather than anything said or done within it.
_KIND_MIN_INCLUDE = {
    "user_turn": "text",
    "text_delta": "text",
    "tool_use": "text",
    "tool_result": "full",
    "permission_request": "full",
    "permission_resolved": "full",
    "turn_end": "full",
    # Something the human did from the page, which the agent was told of.
    "upload_action": "text",
}


def _kind_kept(kind, include: str) -> bool:
    minimum = _KIND_MIN_INCLUDE.get(kind)
    if minimum is None:
        return False
    return TRANSCRIPT_INCLUDE.index(include) >= TRANSCRIPT_INCLUDE.index(minimum)


def read_records(path) -> Iterator[dict]:
    """Yield ``{"seq": int, "event": dict}`` from an ``events.jsonl``-shaped
    file, in file order.

    A missing file yields nothing rather than raising: a session with no
    events yet is an empty transcript, not an error, the same reading
    ``sessions._turn_stats`` gives an absent ``events.jsonl``. A line that
    fails to parse, or parses to something other than a seq/event pair, is
    skipped: it is the torn trailing line a process killed mid-write can
    leave (see ``EventLog._recover_seq``), and it was never delivered to a
    connected client either, so a transcript built from what a client could
    actually have seen loses nothing by skipping it too.
    """
    try:
        f = open(path, "r", encoding="utf-8")
    except OSError:
        return
    with f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            seq = record.get("seq")
            event = record.get("event")
            if isinstance(seq, int) and isinstance(event, dict):
                yield {"seq": seq, "event": event}


def _render_jsonl(kept: list) -> str:
    return "".join(json.dumps(record) + "\n" for record in kept)


def _human_lines(blocks, automation_source=None) -> List[str]:
    """Render a human or explicitly sourced automatic turn as a blockquote."""
    body: List[str] = []
    for block in blocks if isinstance(blocks, list) else ():
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            body.extend(str(block.get("text", "")).splitlines() or [""])
        elif block.get("type") == "image_path":
            body.append("[image: %s]" % block.get("path", ""))
    author = ("Automatic workflow continuation"
              if automation_source == "loom_root_continuation"
              else "Automatic turn" if automation_source else "Human")
    return ["> **%s:**" % author] + [("> " + line) if line else ">" for line in body]


def _render_markdown(kept: list, include: str, session_id, project_dir, exported_at) -> str:
    lines = ["# %s transcript" % product.current().title]
    meta = []
    if session_id is not None:
        meta.append("- session: %s" % session_id)
    if project_dir is not None:
        meta.append("- project: %s" % project_dir)
    if exported_at is not None:
        meta.append("- exported: %s" % exported_at)
    if meta:
        lines.append("")
        lines.extend(meta)

    full = include == "full"
    open_turn = None
    text_buffer: List[str] = []

    def flush_text():
        if text_buffer:
            lines.append("")
            lines.append("".join(text_buffer))
            text_buffer.clear()

    for record in kept:
        event = record["event"]
        kind = event.get("kind")
        turn = event.get("turn")
        if turn is not None and turn != open_turn:
            flush_text()
            lines.append("")
            lines.append("## Turn %s" % turn)
            open_turn = turn

        if kind == "text_delta":
            text_buffer.append(event.get("text", ""))
            continue
        flush_text()

        if kind == "user_turn" and event.get("redelivery_of") is not None:
            # The same message, re-sent after a restart; not a second thing the
            # human said.
            lines.append("")
            lines.append("(Turn %s re-sent after a restart; it is turn %s's message.)"
                         % (turn, event["redelivery_of"]))
        elif kind == "user_turn":
            lines.append("")
            lines.extend(_human_lines(event.get("blocks"), event.get("automation_source")))
        elif kind == "tool_use":
            lines.append("")
            lines.append("Tool call: %s" % event.get("name", ""))
            if full:
                lines.append("```json")
                lines.append(json.dumps(event.get("input", {}), indent=2, sort_keys=True))
                lines.append("```")
        elif kind == "tool_result":
            status = "error" if event.get("is_error") else "ok"
            lines.append("")
            lines.append("Tool result (%s):" % status)
            lines.append("```")
            lines.append(event.get("text", ""))
            lines.append("```")
        elif kind == "permission_request":
            lines.append("")
            lines.append("Permission requested: %s" % event.get("tool", ""))
        elif kind == "permission_resolved":
            lines.append("Permission resolved: %s" % event.get("outcome", ""))
        elif kind == "upload_action":
            lines.append("")
            lines.append(
                "> **Human:** used %s on %s (%s, %s)"
                % (
                    event.get("label", ""),
                    event.get("file", ""),
                    event.get("tool", ""),
                    event.get("outcome", ""),
                )
            )
            if full and event.get("text"):
                lines.append("```")
                lines.append(event["text"])
                lines.append("```")
        elif kind == "turn_end":
            cost = event.get("cost_usd")
            cost_label = "$%.4f" % cost if isinstance(cost, (int, float)) else "unknown"
            lines.append("")
            lines.append(
                "Turn %s ended: %s, cost %s" % (turn, event.get("stop_reason", ""), cost_label)
            )
            open_turn = None

    flush_text()
    return "\n".join(lines) + "\n"


def render_transcript(
    records,
    *,
    fmt: str = "markdown",
    include: str = "text",
    session_id: Optional[str] = None,
    project_dir: Optional[str] = None,
    exported_at: Optional[str] = None,
) -> str:
    """Render ``records`` (as ``read_records`` yields them) as one document.

    ``fmt="markdown"`` produces prose meant to be read: what the human sent
    (``user_turn``, quoted), the model's text, joined across the
    ``text_delta`` chunks that streamed it, and one line naming each tool
    call. At ``include="full"`` this adds each tool's input and result, each
    permission request's outcome, and each turn's cost; ``include="text"``
    leaves all of that out, showing only the conversation's words and which
    tools the model reached for.

    ``fmt="jsonl"`` instead emits the surviving records themselves, one JSON
    object per line, unmodified: the same ``TRANSCRIPT_INCLUDE`` rule decides
    which records survive, but nothing about a kept record's own fields is
    stripped, so a caller wanting the raw wire shapes gets them exactly as
    ``events.jsonl`` holds them.

    ``session_id``, ``project_dir`` and ``exported_at`` are folded into the
    markdown document's header when given, and have no effect on ``jsonl``
    output, which carries no header of its own.

    Raises ``ValueError`` if ``fmt`` is not in ``TRANSCRIPT_FORMATS`` or
    ``include`` is not in ``TRANSCRIPT_INCLUDE``.
    """
    if fmt not in TRANSCRIPT_FORMATS:
        raise ValueError("fmt must be one of %s, not %r" % (TRANSCRIPT_FORMATS, fmt))
    if include not in TRANSCRIPT_INCLUDE:
        raise ValueError("include must be one of %s, not %r" % (TRANSCRIPT_INCLUDE, include))

    kept = [r for r in records if _kind_kept(r.get("event", {}).get("kind"), include)]

    if fmt == "jsonl":
        return _render_jsonl(kept)
    return _render_markdown(kept, include, session_id, project_dir, exported_at)


# Extension a transcript is written with, keyed by TRANSCRIPT_FORMATS.
_TRANSCRIPT_EXTENSION = {"markdown": "md", "jsonl": "jsonl"}

# How many disambiguating suffixes export_transcript tries before giving up.
# Two exports of the same project in the same second, at the same fmt, is the
# only way to reach a second attempt at all, so this bounds a loop that in
# practice runs once.
EXPORT_NAME_ATTEMPTS = 20


def export_transcript(
    project_dir,
    session_id: str,
    *,
    fmt: str = "markdown",
    include: str = "text",
    now: Optional[float] = None,
) -> Path:
    """Render one session's ``events.jsonl`` and write it under ``review/``.

    Reads ``sessions.events_path(project_dir, session_id)`` through
    ``read_records``; a session with no events yet, or no directory at all,
    renders as an otherwise-empty transcript rather than failing.

    The file is named ``transcript-<stamp>.<md|jsonl>``, where ``<stamp>`` is
    ``now`` (epoch seconds, ``time.time()`` if not given) formatted as UTC
    ``YYYYMMDDTHHMMSSZ``: no colons, since a colon in a filename is hostile
    on Windows and inside an archive, and the compact form is still ISO 8601.
    A second export landing on the same stamp and the same ``fmt`` gets a
    ``-2``, ``-3``, ... suffix rather than overwriting the first.

    Writes through ``files.create_review_file``, so ``review/`` is created on
    first use and a symlinked or non-directory ``review/`` is refused. That
    refusal reaches the caller as ``OSError`` rather than a return value:
    this function has nothing sensible to do but write the file it was
    asked for, so there is no partial result to hand back instead.
    """
    if fmt not in _TRANSCRIPT_EXTENSION:
        raise ValueError("fmt must be one of %s, not %r" % (TRANSCRIPT_FORMATS, fmt))
    serve_dir = files.resolve_serve_dir(project_dir)
    records = list(read_records(sessions.events_path(serve_dir, session_id)))
    when = time.time() if now is None else now
    text = render_transcript(
        records,
        fmt=fmt,
        include=include,
        session_id=session_id,
        project_dir=str(serve_dir),
        exported_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)),
    )
    data = text.encode("utf-8")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when))
    extension = _TRANSCRIPT_EXTENSION[fmt]
    base = "transcript-%s" % stamp

    name = None
    for attempt in range(1, EXPORT_NAME_ATTEMPTS + 1):
        name = (
            "%s.%s" % (base, extension) if attempt == 1 else "%s-%d.%s" % (base, attempt, extension)
        )
        try:
            created = files.create_review_file(serve_dir, name)
        except FileExistsError:
            continue
        if created is None:
            raise OSError(
                "review/ under %s is not a directory this process can write into" % serve_dir
            )
        fd, target = created
        try:
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
        finally:
            os.close(fd)
        return target
    raise FileExistsError(name)
