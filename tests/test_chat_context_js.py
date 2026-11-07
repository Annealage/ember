"""Context attachments in the chat pane (``static/chat.js``), under Node with
the pane tests' fake DOM: a product's chip in the composer goes out as a
``context`` block with the message, a product's own composer sends through
the same path, a sent message's chip and the composer's both hand the data
back to the page, the conversation's chips are reported for the page's
markers, and a refused message brings its chips back.
"""

import json
import shutil
import subprocess

import pytest
from test_chat_js import HARNESS as CHAT_HARNESS
from test_chat_js import STATIC

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

HARNESS = (
    CHAT_HARNESS.split("const sent = [];")[0]
    + r"""
const sent = [];
const picked = [];
const reported = [];
const domEvents = [];
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init.detail; } };
document.dispatchEvent = (event) => domEvents.push({ type: event.type, detail: event.detail });
const chat = initChat({
  send: (frame) => { sent.push(frame); return true; },
  onContextClick: (data, meta) => picked.push({ data, meta }),
  onContexts: (list) => reported.push(list),
});
const input = el("chatInput");
const out = {};
chat.handleHello({ id: "s", agent: "ready" });

const data = { surface: "sheet", sheet: "Drivers", parts: ["U5"] };
chat.setContext("selection", { label: "U5 on Drivers", data });
chat.setContext("selection", { label: "U5, C23 on Drivers", data: { ...data, parts: ["U5", "C23"] } });
const attached = store.getState().chat.attachments;
out.composer = attached.map((a) => ({ kind: a.kind, key: a.key, label: a.label }));
// The composer's chip shows its items on the page too.
const strip = el("chatAttachStrip");
strip.children[0].children[0].onclick();
out.composerPick = picked.pop();
// Images are still capped at four beside a context chip.
for (let i = 0; i < 4; i++) store.reserveChatAttachment("upload");
out.fifthImage = store.reserveChatAttachment("upload");
store.clearChatAttachments();
chat.setContext("selection", { label: "U5, C23 on Drivers", data: { ...data, parts: ["U5", "C23"] } });

input.value = "put these left of U4";
el("chatSend").listeners.click[0]();
out.sentFromComposer = sent[sent.length - 1].blocks;
out.composerAfterSend = store.getState().chat.attachments.length;

chat.removeContext("nothing-there");
chat.setContext("other", { label: "point (120.6, 88.9) on Drivers", data: { surface: "sheet" } });
chat.removeContext("other");
out.afterRemove = store.getState().chat.attachments.length;

// The product's own composer, through the same frame.
out.sendMessage = chat.sendMessage("rotate it", [{ label: "U5 on Drivers", data }]);
out.sentFromProduct = sent[sent.length - 1].blocks;
out.emptyMessage = chat.sendMessage("   ", []);

// The server's user_turn draws the message with its chip, and the page hears of it.
chat.handleEvent({ kind: "user_turn", turn: 3, blocks: out.sentFromProduct,
                   client_id: sent[sent.length - 1].client_id }, { replayed: false });
out.reported = reported[reported.length - 1];
const row = el("chatLog").children.find((c) => c.dataset && c.dataset.turn === "3");
const feedChip = row.children[0].children[3].children[0];
out.feedChip = { label: feedChip.textContent, cls: feedChip.className };
feedChip.listeners.click[0]();
out.feedPick = picked.pop();
const reports = reported.length;
chat.handleEvent({ kind: "text_delta", turn: 3, text: "on it" }, { replayed: false });
out.reportsAfterDelta = reported.length - reports;

// A refused message gives its chips back to the composer.
chat.sendMessage("and this", [{ label: "C23 on Drivers", data: { parts: ["C23"] } }]);
chat.handleRefused("Drivers is not in the current build", sent[sent.length - 1].client_id);
out.restored = store.getState().chat.attachments.map((a) => ({ kind: a.kind, label: a.label }));
out.restoredText = input.value;

out.domEvents = domEvents;
console.log(JSON.stringify(out));
process.exit(0);
"""
)


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    harness = tmp_path_factory.mktemp("chat-context-js") / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(STATIC)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_chip_under_one_key_is_replaced_in_place(observed):
    assert observed["composer"] == [
        {"kind": "context", "key": "selection", "label": "U5, C23 on Drivers"}
    ]
    assert observed["afterRemove"] == 0


def test_a_context_chip_does_not_count_against_the_image_cap(observed):
    assert observed["fifthImage"] is None


def test_send_carries_the_composer_s_chip_as_a_context_block(observed):
    assert observed["sentFromComposer"] == [
        {"type": "text", "text": "put these left of U4"},
        {
            "type": "context",
            "label": "U5, C23 on Drivers",
            "data": {"surface": "sheet", "sheet": "Drivers", "parts": ["U5", "C23"]},
        },
    ]
    assert observed["composerAfterSend"] == 0


def test_a_product_composer_sends_through_the_same_frame(observed):
    assert observed["sendMessage"] is True
    assert observed["sentFromProduct"] == [
        {"type": "text", "text": "rotate it"},
        {
            "type": "context",
            "label": "U5 on Drivers",
            "data": {"surface": "sheet", "sheet": "Drivers", "parts": ["U5"]},
        },
    ]
    assert observed["emptyMessage"] is False


def test_the_conversation_s_chips_are_reported_once_per_change(observed):
    assert observed["reported"] == [
        {
            "turn": 3,
            "label": "U5 on Drivers",
            "data": {"surface": "sheet", "sheet": "Drivers", "parts": ["U5"]},
        }
    ]
    assert observed["reportsAfterDelta"] == 0


def test_a_chip_click_hands_its_data_to_the_page_both_ways(observed):
    pick = observed["composerPick"]
    assert pick["meta"] == {"label": "U5, C23 on Drivers", "turn": None}
    assert pick["data"]["parts"] == ["U5", "C23"]
    assert observed["domEvents"][0]["type"] == "ember:context"
    assert observed["domEvents"][0]["detail"]["data"]["parts"] == ["U5", "C23"]


def test_a_sent_message_shows_its_chip_and_a_click_names_the_turn(observed):
    assert observed["feedChip"] == {"label": "U5 on Drivers", "cls": "contextchip"}
    assert observed["feedPick"]["meta"] == {"label": "U5 on Drivers", "turn": 3}
    assert observed["feedPick"]["data"]["parts"] == ["U5"]


def test_a_refused_message_brings_its_chips_back(observed):
    assert observed["restored"] == [{"kind": "context", "label": "C23 on Drivers"}]
    assert observed["restoredText"] == "and this"
