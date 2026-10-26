"""Paged chat through the actual transport, store and pane, under Node."""

import json
import shutil
import subprocess

import pytest
from test_chat_js import HARNESS as CHAT_HARNESS
from test_chat_js import STATIC

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

# Reuse the pane tests' browser globals and DOM, without running their scenarios.
HARNESS = (
    CHAT_HARNESS.split("const sent = [];")[0]
    + r"""
const frames = new Map();
let nextFrame = 1;
globalThis.requestAnimationFrame = (fn) => { const id = nextFrame++; frames.set(id, fn); return id; };
const flush = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach((fn) => fn()); };
const attach = (parent, child, at = parent.children.length) => {
  child.remove(); parent.children.splice(at, 0, child); child.parentNode = parent; return child;
};
El.prototype.appendChild = function(child) { return attach(this, child); };
El.prototype.append = function(...children) { children.forEach((child) => attach(this, child)); };
El.prototype.remove = function() {
  if (this.parentNode) {
    const siblings = this.parentNode.children;
    const i = siblings.indexOf(this); if (i !== -1) siblings.splice(i, 1);
    this.parentNode = null;
  }
};
El.prototype.before = function(...children) {
  // The shared fake document's topbar elements are detached; native before()
  // is a no-op there. Transcript rows have parents and use real insertion.
  if (!this.parentNode) return;
  for (const child of children) attach(this.parentNode, child, this.parentNode.children.indexOf(this));
};
El.prototype.replaceChildren = function(...children) {
  [...this.children].forEach((child) => child.remove()); this.append(...children);
};
Object.defineProperty(El.prototype, "isConnected", { get() { return !!this.parentNode; } });
Object.defineProperty(El.prototype, "textContent", {
  get() { return this._text || ""; },
  set(value) { this._text = value; this.textWrites = (this.textWrites || 0) + 1; },
});
const sockets = [];
class Socket {
  static OPEN = 1;
  constructor() { this.readyState = 0; this.sent = []; this.listeners = {}; sockets.push(this); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  send(data) { this.sent.push(JSON.parse(data)); }
  open() { this.readyState = 1; this.listeners.open.forEach((fn) => fn({})); }
  close() { this.readyState = 3; this.listeners.close.forEach((fn) => fn({code:1006})); }
  deliver(frame) { this.listeners.message.forEach((fn) => fn({data:JSON.stringify({v:2,...frame})})); }
}
globalThis.WebSocket = Socket;
globalThis.fetch = async () => ({status:400});
const { initWs } = await import(pathToFileURL(dir + "/ws.js").href);
let transport;
const chat = initChat({send: (frame) => transport.send(frame)});
transport = initWs({indicator:null, onHello:chat.handleHello, onAgentEvent:chat.handleEvent});
let socket = sockets[0]; socket.open();
const hello = (id, seq) => ({type:"hello", protocol:2, seq, session:{id,agent:"ready",model:"current",usage:{cost_usd:2,tokens:{}}}});
const event = (seq, event) => ({type:"event",seq,event});
const user = (turn) => ({kind:"user_turn",turn,blocks:[{type:"text",text:"user " + turn}]});
const text = (turn, text) => ({kind:"text_delta",turn,text});
const end = (turn) => ({kind:"turn_end",turn,stop_reason:"end"});
const entry = (seq,event) => ({seq,event});
const page = (initial, before, events, extra = {}) => ({
  type:"history_page",session_id:"s",initial,before,events,has_more:true,
  ...(initial ? {through:100,pending_request_ids:["pending"]} : {requested_before:90}), ...extra,
});
const log = el("chatLog");
log.clientHeight = 100; log.scrollTop = 0;
const rows = () => log.children.filter((node) => node.className === "turn");
Object.defineProperty(log,"scrollHeight",{get:() => rows().length * 100 + 40});
log.querySelectorAll = () => rows();
const originalCreate = document.createElement;
document.createElement = (tag) => {
  const node = originalCreate(tag);
  node.getBoundingClientRect = () => {
    if (node.className !== "turn") return {top:0,bottom:0};
    const top = 40 + rows().indexOf(node) * 100 - log.scrollTop;
    return {top,bottom:top+100};
  };
  return node;
};
const row = (turn) => rows().find((node) => node.dataset.turn === String(turn));
const control = log.children.find((node) => node.className === "chathistory");
const clickHistory = () => control.listeners.click[0]();
const out = {historyTurns:socket.sent[0].history_turns};
socket.deliver(hello("s",100));
let notifications = 0;
store.subscribe("chat", () => notifications++);
socket.deliver(page(true,90,[
  entry(2,{kind:"permission_request",request_id:"pending",tool:"write",input:{}}),
  entry(90,user(21)),entry(91,text(21,"recent")),
  entry(92,{kind:"tool_use",turn:21,tool_use_id:"read",name:"read",input:{}}),
  entry(93,{kind:"tool_result",tool_use_id:"read",text:"result"}),entry(94,end(21)),
  entry(95,user(22)),entry(96,text(22,"latest")),entry(97,end(22)),
  entry(98,{kind:"permission_request",request_id:"resolved",tool:"old",input:{}}),
]));
out.initialBatch = {notifications,frames:frames.size,rowsBeforePaint:rows().length};
flush();
out.recent = store.getState().chat.turns.map((t) => t.turn);
out.pending = store.getState().chat.pending.map((p) => p.request_id);
const group = row(21).children[1].children[0].children[1];
group.open = true; group.listeners.toggle[0]();
const card = group.children[1].children[0];
card.open = true; card.listeners.toggle[0]();
card.children[2].scrollTop = 37;
log.scrollTop = 50; log.listeners.scroll[0]();
out.scrollRequest = socket.sent.at(-1);
socket.deliver(event(101,text(23,"live")));
socket.deliver(page(false,58,[
  entry(58,{kind:"input_state",turn:18,state:"queued"}),
  entry(60,user(19)),entry(61,text(19,"older")),entry(62,end(19)),
  entry(63,{kind:"permission_request",request_id:"resolved",tool:"old",input:{}}),
  entry(64,{kind:"agent_error",remediation:"old error",stderr:"history"}),
  entry(70,user(20)),entry(71,text(20,"middle")),entry(72,end(20)),
]));
flush();
out.prepend = {
  turns:store.getState().chat.turns.map((t) => t.turn),
  rows:rows().map((r) => Number(r.dataset.turn)),scrollTop:log.scrollTop,
  sameGroup:row(21).children[1].children[0].children[1] === group,
  inspector:[group.open,card.open,card.children[2].scrollTop],
  pending:store.getState().chat.pending.map((p) => p.request_id),
  banner:store.getState().chat.banner,
  running:store.getState().chat.turns.filter((t) => !t.complete).map((t) => t.turn),
};
const oldWrites = row(19).children[0].children[1].textWrites;
socket.deliver(event(102,text(23,"!"))); flush();
out.oldRowUntouched = oldWrites === row(19).children[0].children[1].textWrites;
// A duplicate or stale page must not append the same historical text again.
socket.deliver(page(false,58,[entry(61,text(19,"older"))]));
out.oldText = store.getState().chat.turns.find((t) => t.turn === 19).segments[0].text;
clickHistory();
socket.deliver({type:"history_error",session_id:"s",requested_before:58,reason:"Disk busy."});
out.retry = {text:control.textContent,disabled:control.disabled};
clickHistory(); out.retryFrame = socket.sent.at(-1);
socket.deliver(page(false,null,[],{requested_before:58,has_more:false}));
out.exhausted = {text:control.textContent,disabled:control.disabled};
// Current pending approvals remain actionable after scrollback.
const pendingCard = el("chatPending").children[0];
pendingCard.children.find((node) => node.className === "pactions").children[0].listeners.click[0]();
out.permissionFrame = socket.sent.at(-1);
// Same-session reconnect catches up, without replacing the recent page.
socket.close(); await new Promise((resolve) => setTimeout(resolve,1200));
socket = sockets[1]; socket.open(); out.reconnect = socket.sent[0];
socket.deliver(hello("s",103));
socket.deliver(event(102,text(23,"!")));
socket.deliver(event(103,text(23," caught up"))); flush();
out.caughtUp = store.getState().chat.turns.find((t) => t.turn === 23).segments[0].text;
// Reset invalidates in-flight pages from this same session.
socket.deliver(event(104,{kind:"session_reset",reason:"fresh"}));
socket.deliver(page(false,1,[entry(1,user(1))],{requested_before:58})); flush();
out.afterReset = {turns:store.getState().chat.turns.length,pending:store.getState().chat.pending.length};
// Another session never accepts a page from the previous one.
socket.deliver(hello("next",200));
socket.deliver(page(true,90,[entry(90,user(21))]));
out.staleSession = store.getState().chat.turns.length;
// Initial failure is retryable while live events continue independently.
socket.deliver({type:"history_page",session_id:"next",initial:true,through:200,before:null,has_more:false,
  pending_request_ids:["waiting"],events:[entry(150,{kind:"permission_request",request_id:"waiting",tool:"write",input:{}})]});
socket.deliver({type:"history_error",session_id:"next",initial:true,requested_before:null,through:200,reason:"Read failed."});
socket.deliver(event(201,text(9,"new"))); flush();
out.liveDuringFailure = store.getState().chat.turns.find((t) => t.turn === 9).segments[0].text;
clickHistory(); out.initialRetryFrame = socket.sent.at(-1);
socket.deliver({type:"history_page",session_id:"next",initial:true,through:201,before:198,has_more:false,
  pending_request_ids:["waiting"],events:[entry(198,user(9)),entry(199,text(9,"old")),entry(201,text(9,"new")),
    entry(150,{kind:"permission_request",request_id:"waiting",tool:"write",input:{}})]}); flush();
out.initialRetry = {text:store.getState().chat.turns.find((t) => t.turn === 9).segments[0].text,
  pending:store.getState().chat.pending.map((p) => p.request_id),model:store.getState().chat.model,
  usage:store.getState().chat.usage.cost_usd};

// A long collapsed run allocates neither cards nor payload bodies. Opening the
// run reveals ordered summaries; opening one card alone formats its payload.
let seq = 201;
let formatted = 0;
for (let i = 0; i < 300; i++) {
  socket.deliver(event(++seq,{kind:"tool_use",turn:10,tool_use_id:"lazy-" + i,name:"read",input:{payload:{i}}}));
  const tool = store.getState().chat.turns.find((t) => t.turn === 10).segments[0].tools.at(-1);
  tool.input.payload.toJSON = () => { formatted++; return {value:"large input"}; };
  socket.deliver(event(++seq,{kind:"tool_result",tool_use_id:"lazy-" + i,is_error:i === 4,text:"x".repeat(100000)}));
}
flush();
const lazyRun = row(10).children[1].children[0].children[0];
const lazyTools = lazyRun.children[1];
out.lazyCollapsed = {cards:lazyTools.children.length,formatted,summary:lazyRun.children[0].textContent};
lazyRun.open = true; lazyRun.listeners.toggle[0]();
out.lazyRunOpen = {cards:lazyTools.children.length,formatted,
  summariesOnly:lazyTools.children.every((c) => c.children.length === 1),
  order:lazyTools.children.map((c) => c.dataset.toolId)};
const lazyCard = lazyTools.children[0];
lazyCard.open = true; lazyCard.listeners.toggle[0]();
out.lazyCardOpen = {formatted,children:lazyCard.children.length,resultLength:lazyCard.children[2].textContent.length};
lazyTools.scrollTop = 77; lazyCard.children[2].scrollTop = 31; log.scrollTop = 75;
socket.deliver(event(++seq,{kind:"tool_result",tool_use_id:"lazy-0",text:"live result"})); flush();
out.lazyLiveResult = {sameCard:lazyTools.children[0] === lazyCard,open:lazyCard.open,formatted,
  result:lazyCard.children[2].textContent,scroll:[lazyTools.scrollTop,lazyCard.children[2].scrollTop,log.scrollTop]};
lazyCard.open = false; lazyCard.listeners.toggle[0]();
socket.deliver(event(++seq,{kind:"tool_result",tool_use_id:"lazy-0",text:"deferred result"})); flush();
out.lazyClosedResult = lazyCard.children[2].textContent;
lazyCard.open = true; lazyCard.listeners.toggle[0]();
out.lazyReopenedResult = lazyCard.children[2].textContent;
lazyRun.open = false; lazyRun.listeners.toggle[0]();
socket.deliver(event(++seq,{kind:"tool_use",turn:10,tool_use_id:"lazy-300",name:"read",input:{payload:{new:true}}})); flush();
out.lazyClosedRunUpdate = {cards:lazyTools.children.length,summary:lazyRun.children[0].textContent};
lazyRun.open = true; lazyRun.listeners.toggle[0]();
out.lazyReopenedRun = {cards:lazyTools.children.length,sameCard:lazyTools.children[0] === lazyCard,
  open:lazyCard.open,last:lazyTools.children.at(-1).dataset.toolId,formatted};
console.log(JSON.stringify(out)); process.exit(0);
"""
)


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    harness = tmp_path_factory.mktemp("chat-history-js") / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(STATIC)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_recent_history_is_batched_and_keeps_only_current_permissions(observed):
    assert observed["historyTurns"] == 1
    assert observed["initialBatch"] == {"notifications": 1, "frames": 1, "rowsBeforePaint": 0}
    assert observed["recent"] == [21, 22]
    assert observed["pending"] == ["pending"]


def test_scrolling_loads_full_older_turns_and_preserves_reader_inspectors(observed):
    assert observed["scrollRequest"] == {
        "type": "history",
        "session_id": "s",
        "before": 90,
        "limit": 1,
        "v": 2,
    }
    assert observed["prepend"] == {
        "turns": [18, 19, 20, 21, 22, 23],
        "rows": [18, 19, 20, 21, 22, 23],
        "scrollTop": 350,
        "sameGroup": True,
        "inspector": [True, True, 37],
        "pending": ["pending"],
        "banner": None,
        "running": [23],
    }
    assert observed["oldRowUntouched"] is True
    assert observed["oldText"] == "older"


def test_history_failure_has_an_explicit_retry_and_exhaustion_state(observed):
    assert "Disk busy." in observed["retry"]["text"]
    assert "Retry" in observed["retry"]["text"]
    assert observed["retry"]["disabled"] is False
    assert observed["retryFrame"]["before"] == 58
    assert observed["exhausted"] == {"text": "Beginning of conversation", "disabled": True}
    assert observed["permissionFrame"]["type"] == "permission"
    assert observed["permissionFrame"]["request_id"] == "pending"


def test_reconnect_cursor_is_not_regressed_by_history_and_reset_rejects_stale_pages(observed):
    assert observed["reconnect"]["last_seq"] == 102
    assert observed["reconnect"]["session_id"] == "s"
    assert observed["caughtUp"] == "live! caught up"
    assert observed["afterReset"] == {"turns": 0, "pending": 0}
    assert observed["staleSession"] == 0


def test_initial_read_retry_merges_older_deltas_before_live_output(observed):
    assert observed["liveDuringFailure"] == "new"
    assert observed["initialRetryFrame"]["before"] is None
    assert observed["initialRetry"] == {
        "text": "oldnew",
        "pending": ["waiting"],
        "model": "current",
        "usage": 2,
    }


def test_long_collapsed_runs_do_not_materialize_cards_or_format_payloads(observed):
    assert observed["lazyCollapsed"] == {
        "cards": 0,
        "formatted": 0,
        "summary": "300 tool calls · 1 failed",
    }
    assert observed["lazyRunOpen"] == {
        "cards": 300,
        "formatted": 0,
        "summariesOnly": True,
        "order": [f"lazy-{i}" for i in range(300)],
    }
    assert observed["lazyCardOpen"] == {
        "formatted": 1,
        "children": 3,
        "resultLength": 100000,
    }


def test_lazy_disclosures_keep_identity_live_results_and_reading_state(observed):
    assert observed["lazyLiveResult"] == {
        "sameCard": True,
        "open": True,
        "formatted": 1,
        "result": "live result",
        "scroll": [77, 31, 75],
    }
    assert observed["lazyClosedResult"] == "live result"
    assert observed["lazyReopenedResult"] == "deferred result"
    assert observed["lazyClosedRunUpdate"] == {
        "cards": 300,
        "summary": "301 tool calls · 1 running · 1 failed",
    }
    assert observed["lazyReopenedRun"] == {
        "cards": 301,
        "sameCard": True,
        "open": True,
        "last": "lazy-300",
        "formatted": 1,
    }
