"""The chat slice of the page's store (``static/store.js``), run under Node.

A turn's human side comes from its ``user_turn`` event and nothing else, so
it shows once in every tab and on every replay; a message this tab sent is
held as pending, by its ``client_id``, only until the server logs it or
refuses it, and a refused one is never shown against a later turn. The same
harness shape as ``test_review_js.py``: a scripted run printing one JSON
object of observations.
"""

import json
import shutil
import subprocess

import pytest

from annealage_ember import product

STORE_JS = __import__("pathlib").Path(product.__file__).resolve().parent / "static" / "store.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

HARNESS = r"""
import { pathToFileURL } from "node:url";

const { store } = await import(pathToFileURL(process.argv[2]).href);
const text = (t) => [{ type: "text", text: t }];
const chat = () => store.getState().chat;
const turn = (n) => chat().turns.find((t) => t.turn === n) || null;
const out = {};

// This tab sends, and the server refuses it (the agent was still starting).
store.queueChatUserTurn("c-refused", text("first try"));
out.pendingAfterSend = chat().pendingUser.length;
out.handedBack = store.dropChatUserTurn("c-refused");
out.pendingAfterRefusal = chat().pendingUser.length;
out.droppedTwice = store.dropChatUserTurn("c-refused");

// The next turn is another tab's: its events alone decide what turn 1 shows.
store.setChatUserTurn(1, text("from the phone"), "c-phone");
store.appendChatTextDelta(1, "Sure.");
store.endChatTurn(1, "end", 0.0);
out.turn1User = turn(1).user;

// This tab sends again; the reply's events may arrive before or after the
// server's echo of the message, and the message shows once either way.
store.queueChatUserTurn("c-mine", text("second try"));
store.setChatUserTurn(2, text("second try"), "c-mine");
out.pendingAfterEcho = chat().pendingUser.length;
store.appendChatTextDelta(2, "On it.");
out.turn2 = { user: turn(2).user, segments: turn(2).segments };

// A turn with no user_turn (a history from before the server logged them)
// shows no human side, rather than borrowing a pending message.
store.queueChatUserTurn("c-late", text("never answered"));
store.appendChatTextDelta(3, "old reply");
out.turn3User = turn(3).user;

// A new connection's replay shows the late message never arrived: it is
// handed back once, and nothing is left pending.
out.lateHandedBack = store.dropChatUserTurn("c-late");
out.pendingAfterSettling = chat().pendingUser.length;

// Canonical chronology: results are updates, not newly appended activity.
store.appendChatTextDelta(4, "Alpha");
store.appendChatTextDelta(4, ".");
store.addChatToolUse(4, "a1", "read", { file: "first" });
store.setChatToolResult("a1", false, "early");
store.appendChatTextDelta(4, "");
store.addChatToolUse(4, "a2", "read", { file: "second" });
store.appendChatTextDelta(4, "Beta.");
store.addChatToolUse(4, "b1", "write", { file: "third" });
store.appendChatTextDelta(4, "Gamma.");
store.addChatToolUse(5, "next-turn", "read", {});
const before = turn(4).segments;
const otherTurn = turn(5);
store.setChatToolResult("a1", true, "late earlier result");
store.addChatToolUse(4, "a1", "read", { file: "duplicate must not move" });
const after = turn(4).segments;
out.orderedSegments = after;
out.originalResultUnchanged = before[1].tools[0].result;
out.onlyOriginalRunUpdated = after.map((s, i) => s === before[i]);
out.otherTurnUnchanged = turn(5) === otherTurn;
out.frozenSegments = Object.isFrozen(after) && after.every((s) => (
  Object.isFrozen(s) && (s.kind !== "tools" || Object.isFrozen(s.tools)
    && s.tools.every((t) => Object.isFrozen(t) && (!t.result || Object.isFrozen(t.result))))
));

// Thinking merges with adjacent thinking only, and truncation sticks.
store.appendChatThinkingDelta(6, "a");
store.appendChatThinkingDelta(6, "b", true);
store.appendChatTextDelta(6, "Answer.");
store.appendChatThinkingDelta(6, "later");
out.thinkingSegments = turn(6).segments;

// A turn carries the server's times; older logs have none.
store.setChatUserTurn(7, text("timed"), "c-timed", null, null, 1700000000000);
store.endChatTurn(7, "end", 0, null, null, null, 1700000009000);
store.setChatUserTurn(8, text("untimed"), null);
out.turnTimes = [7, 8].map((n) => [turn(n).startedAt, turn(n).endedAt, turn(n).clientId]);

// Every other feed item carries the time of its own event: a segment that
// merges pieces keeps the first piece's, and an event with none (an older log)
// or a bad one carries null.
store.appendChatTextDelta(9, "one ", 1700000001000);
store.appendChatTextDelta(9, "two", 1700000002000);
store.appendChatThinkingDelta(9, "hm", false, 1700000003000);
store.addChatToolUse(9, "t9", "read", {}, 1700000004000);
store.addChatToolUse(9, "t10", "read", {}, 1700000005000);
store.setChatToolResult("t9", false, "ok", 1700000006000);
store.addChatTurnError(9, "boom", "", 1700000007000);
store.appendChatTextDelta(9, "bad", "yesterday");
store.addChatPermissionRequest("pr9", "t", {}, [], true, null, 1700000008000);
store.addChatPermissionRequest("pr10", "t", {});
store.addChatActionResult({ id: "ua9", label: "L", file: "f", bytes: 1, tool: "t", outcome: "done", at: 1700000009000 });
store.addChatActionResult({ id: "ua10", label: "L", file: "f", bytes: 1, tool: "t", outcome: "done" });
out.itemTimes = {
  segments: turn(9).segments.map((s) => (
    s.kind === "tools" ? s.tools.map((t) => [t.at, t.result ? t.result.at : "no result"]) : s.at)),
  errors: turn(9).errors.map((e) => e.at),
  permissions: chat().pending.map((p) => [p.request_id, p.at]),
  actions: chat().actionResults.map((r) => [r.id, r.at]),
};

// A refusal keeps the entry as failed, handing the blocks back once; a resend
// is marked; only failed entries are cleared together.
store.queueChatUserTurn("c-fail", text("will fail"));
out.queuedEntry = (({ clientId, state, reason, resent }) => ({ clientId, state, reason, resent }))(chat().pendingUser[0]);
out.failHandedBack = store.failChatUserTurn("c-fail", "no agent");
out.failedAgain = store.failChatUserTurn("c-fail", "again");
out.failedEntry = (({ state, reason }) => ({ state, reason }))(chat().pendingUser[0]);
store.queueChatUserTurn("c-keep", text("keep"));
store.markChatUserTurnResent("c-keep");
store.clearChatFailedUserTurns();
out.afterClearFailed = chat().pendingUser.map((p) => [p.clientId, p.state, p.resent]);
store.dropChatUserTurn("c-keep");

// The inputs the status line and the banner read.
const seen = [];
store.subscribe("hostedJob", () => seen.push("job"));
store.subscribe("starting", () => seen.push("starting"));
store.subscribe("connectionFailure", () => seen.push("failure"));
out.initialInputs = [store.getState().hostedJob, store.getState().starting, store.getState().connectionFailure];
store.setHostedJob({ state: "failed", reason: "no credit", at: 1700000000000 });
store.setHostedJob({ state: "failed", reason: "no credit", at: 1700000000000 });
store.setHostedJob({ state: "bogus", at: "soon" });
out.jobInputs = [store.getState().hostedJob];
store.setStarting(true);
store.setStarting(true);
store.setConnectionFailure({ kind: "offline", message: "You're offline.", action: "none" });
store.setConnectionFailure({ kind: "offline", message: "You're offline.", action: "none" });
out.failureInput = store.getState().connectionFailure;
store.setConnectionFailure(null);
out.notified = seen;
out.closed = (() => {
  store.setChatClosed({ reason: "child_exited", detail: "exit code 1", at: 5 });
  const set = chat().closed;
  store.setChatClosed(null);
  return [set, chat().closed];
})();

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    harness = tmp_path_factory.mktemp("chat-store-js") / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(STORE_JS)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_refused_message_is_handed_back_once_and_stops_being_pending(observed):
    assert observed["pendingAfterSend"] == 1
    assert observed["handedBack"] == [{"type": "text", "text": "first try"}]
    assert observed["pendingAfterRefusal"] == 0
    assert observed["droppedTwice"] is None


def test_a_refused_message_never_pairs_with_the_next_turn(observed):
    assert observed["turn1User"] == [{"type": "text", "text": "from the phone"}]


def test_the_server_s_echo_retires_this_tab_s_pending_copy(observed):
    assert observed["pendingAfterEcho"] == 0
    assert observed["turn2"] == {
        "user": [{"type": "text", "text": "second try"}],
        "segments": [{"kind": "text", "text": "On it.", "at": None}],
    }


def test_a_turn_without_a_user_turn_borrows_no_pending_message(observed):
    assert observed["turn3User"] is None


def test_a_message_that_never_arrived_is_handed_back(observed):
    assert observed["lateHandedBack"] == [{"type": "text", "text": "never answered"}]
    assert observed["pendingAfterSettling"] == 0


def test_segments_keep_text_and_contiguous_tool_runs_in_event_order(observed):
    segments = observed["orderedSegments"]
    assert [s["kind"] for s in segments] == ["text", "tools", "text", "tools", "text"]
    assert [s["text"] for s in segments if s["kind"] == "text"] == [
        "Alpha.",
        "Beta.",
        "Gamma.",
    ]
    assert [[t["tool_use_id"] for t in s["tools"]] for s in segments if s["kind"] == "tools"] == [
        ["a1", "a2"],
        ["b1"],
    ]
    assert segments[1]["tools"][0]["input"] == {"file": "first"}
    assert segments[1]["tools"][0]["result"] == {
        "isError": True,
        "text": "late earlier result",
        "at": None,
    }
    assert observed["originalResultUnchanged"] == {"isError": False, "text": "early", "at": None}
    assert observed["onlyOriginalRunUpdated"] == [True, False, True, True, True]
    assert observed["otherTurnUnchanged"] is True
    assert observed["frozenSegments"] is True


def test_thinking_is_its_own_segment_kept_in_event_order(observed):
    segments = observed["thinkingSegments"]
    assert [s["kind"] for s in segments] == ["thinking", "text", "thinking"]
    assert segments[0] == {"kind": "thinking", "text": "ab", "truncated": True, "at": None}
    assert segments[2]["truncated"] is False


def test_every_feed_item_carries_the_time_of_its_event_or_null(observed):
    times = observed["itemTimes"]
    # Text keeps its first piece's time, and a bad one is null.
    assert times["segments"] == [
        1700000001000,
        1700000003000,
        [[1700000004000, 1700000006000], [1700000005000, "no result"]],
        None,
    ]
    assert times["errors"] == [1700000007000]
    assert times["permissions"] == [["pr9", 1700000008000], ["pr10", None]]
    assert times["actions"] == [["ua9", 1700000009000], ["ua10", None]]


def test_a_turn_carries_the_server_s_times_when_the_log_has_them(observed):
    assert observed["turnTimes"] == [[1700000000000, 1700000009000, "c-timed"], [None, None, None]]


def test_a_failed_message_stays_as_an_entry_and_is_handed_back_once(observed):
    assert observed["queuedEntry"] == {
        "clientId": "c-fail",
        "state": "sending",
        "reason": None,
        "resent": False,
    }
    assert observed["failHandedBack"] == [{"type": "text", "text": "will fail"}]
    assert observed["failedAgain"] is None
    assert observed["failedEntry"] == {"state": "failed", "reason": "no agent"}
    assert observed["afterClearFailed"] == [["c-keep", "sending", True]]


def test_the_status_and_banner_inputs_notify_only_when_they_change(observed):
    assert observed["initialInputs"] == [{"state": "none", "reason": None, "at": None}, False, None]
    assert observed["jobInputs"] == [{"state": "none", "reason": None, "at": None}]
    assert observed["failureInput"] == {
        "kind": "offline",
        "message": "You're offline.",
        "action": "none",
    }
    assert observed["notified"] == ["job", "job", "starting", "failure", "failure"]


def test_an_agent_close_is_kept_until_cleared(observed):
    assert observed["closed"] == [
        {"reason": "child_exited", "detail": "exit code 1", "at": 5},
        None,
    ]
