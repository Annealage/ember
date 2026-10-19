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
        "segments": [{"kind": "text", "text": "On it."}],
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
        "Alpha.", "Beta.", "Gamma.",
    ]
    assert [[t["tool_use_id"] for t in s["tools"]] for s in segments if s["kind"] == "tools"] == [
        ["a1", "a2"], ["b1"],
    ]
    assert segments[1]["tools"][0]["input"] == {"file": "first"}
    assert segments[1]["tools"][0]["result"] == {
        "isError": True, "text": "late earlier result",
    }
    assert observed["originalResultUnchanged"] == {"isError": False, "text": "early"}
    assert observed["onlyOriginalRunUpdated"] == [True, False, True, True, True]
    assert observed["otherTurnUnchanged"] is True
    assert observed["frozenSegments"] is True
