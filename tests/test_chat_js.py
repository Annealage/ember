"""The chat pane (``static/chat.js``) run under Node, against a minimal fake
DOM: just enough elements for ``initChat`` to mount and for the composer, the
banner and the toast to be read back.

What these pin is what the human sees at the edges of a connection: why the
agent is down, for a page opened after it went down (the hello, since a
replayed ``agent_error`` raises no banner); and a message that will not
appear, refused or lost in a dropped connection, going back into the
composer in front of whatever was typed since, with a toast saying so. And the
state a stylesheet reads off the pane element (``data-working``,
``data-turn-started``, ``data-pending``) through a turn, its permission
requests, a reconnect, a steer and a mid-turn connect. Turn accounting and
delivery state stay out of the transcript. Same harness as ``test_review_js.py``.
"""

import json
import shutil
import subprocess

import pytest

from annealage_ember import product

STATIC = __import__("pathlib").Path(product.__file__).resolve().parent / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

HARNESS = r"""
import { pathToFileURL } from "node:url";

globalThis.window = globalThis;
globalThis.location = { hash: "#t=tok", pathname: "/", search: "", protocol: "http:", host: "x" };
globalThis.history = { replaceState() {} };
globalThis.Node = { ELEMENT_NODE: 1 };
globalThis.CSS = { escape: (s) => s };
globalThis.addEventListener = () => {};
const windowEvents = [];
globalThis.dispatchEvent = (event) => { windowEvents.push([event.type, event.detail]); return true; };

class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.hidden = false; this.textContent = ""; this.value = ""; this.listeners = {};
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
  }
  get nodeType() { return 1; }
  get childElementCount() { return this.children.length; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  appendChild(child) { this.children.push(child); return child; }
  append(...kids) { this.children.push(...kids); }
  remove() { this.removed = true; }
  before(...nodes) { (this.inserted ||= []).push(...nodes); }
  after(...nodes) { (this.inserted ||= []).push(...nodes); }
  replaceChildren(...kids) { this.children = [...kids]; }
  setAttribute() {}
  getAttribute() { return null; }
  focus() {}
  getBoundingClientRect() { return this.rect || { top: 0, bottom: 0 }; }
  blur() {}
  querySelector(sel) {
    const find = (el) => {
      for (const c of el.children) {
        if (c.tagName === sel) return c;
        const hit = find(c);
        if (hit) return hit;
      }
      return null;
    };
    return find(this);
  }
  querySelectorAll() { return this.anchorNodes || []; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html || ""; }
}
const byId = new Map();
const el = (id) => { if (!byId.has(id)) byId.set(id, new El("div")); return byId.get(id); };
globalThis.document = {
  nodeType: 9, title: "t", activeElement: null, hasFocus: () => true,
  createElement: (tag) => new El(tag),
  getElementById: el,
  querySelector: (sel) => (sel.startsWith("#") ? el(sel.slice(1)) : null),
  addEventListener() {},
};

const dir = process.argv[2];
const { initChat, usageSummary, agentActivity } = await import(pathToFileURL(dir + "/chat.js").href);
const { store } = await import(pathToFileURL(dir + "/store.js").href);

const sent = [];
let online = true;
const chat = initChat({ send: (frame) => { if (!online) return false; sent.push(frame); return true; } });
const input = el("chatInput");
const sendMessage = (text) => { input.value = text; el("chatSend").listeners.click[0](); };
const out = {};
// Before any hello nothing has said the agent is starting: signed out, refused
// or just not up yet reads "Not connected", with or without a failure banner.
const statusNow = () => ({ text: el("agentStatus").textContent, state: el("agentStatus").dataset.state });
out.statusBeforeHello = statusNow();
store.setConnection("refused");
store.setConnectionFailure({ kind: "signed_out", message: "You're signed out.", action: "sign_in" });
out.statusSignedOut = statusNow();
store.setConnectionFailure(null);
store.setConnection("connecting");

// A page opened while the agent is down: the hello says why.
chat.handleHello({ id: "s", agent: "unavailable",
                   agent_error: { remediation: "log omp in", stderr: "401 no credentials" } });
out.bannerFromHello = store.getState().chat.banner;
out.sendDisabledWhileDown = el("chatSend").disabled;

// A viewer-only run: nothing can be sent, and every disabled control says why.
chat.handleHello({ id: "viewer-only", agent: "unavailable" });
out.viewerOnly = ["chatSend", "chatInput", "chatAttachBtn", "chatInterrupt"].map(
  (id) => [el(id).disabled, el(id).title]);
chat.handleHello({ id: "s", agent: "unavailable",
                   agent_error: { remediation: "log omp in", stderr: "401 no credentials" } });
out.afterViewerOnly = [el("chatInput").disabled, el("chatAttachBtn").disabled];

// A replayed agent_error is history; a live one is news.
store.clearChatBanner();
chat.handleEvent({ kind: "agent_error", remediation: "old trouble", stderr: "" }, { replayed: true });
out.bannerFromReplay = store.getState().chat.banner;
chat.handleEvent({ kind: "agent_error", remediation: "new trouble", stderr: "" }, { replayed: false });
out.bannerFromLive = store.getState().chat.banner && store.getState().chat.banner.text;
// Why it was down stays while it starts again, and goes once it is up.
chat.handleEvent({ kind: "agent_status", status: "connecting" }, { replayed: false });
out.errorWhileStarting = store.getState().chat.banner && store.getState().chat.banner.text;
chat.handleEvent({ kind: "agent_status", status: "ready" }, { replayed: false });
out.errorOnceReady = store.getState().chat.banner;
store.clearChatBanner();

// The agent's question (attention) leaves the banner once the human answers;
// an error banner outlives the next message.
chat.handleEvent({ kind: "attention", title: "demo: circuit checkpoint", body: "OK?" }, { replayed: false });
out.attentionBanner = store.getState().chat.banner && store.getState().chat.banner.text;
chat.handleEvent({ kind: "user_turn", turn: 7, blocks: [{ type: "text", text: "yes" }] }, { replayed: false });
out.afterAnswer = store.getState().chat.banner;
chat.handleEvent({ kind: "agent_error", remediation: "still broken", stderr: "" }, { replayed: false });
chat.handleEvent({ kind: "user_turn", turn: 8, blocks: [{ type: "text", text: "and?" }] }, { replayed: false });
out.errorAfterTurn = store.getState().chat.banner && store.getState().chat.banner.text;
store.clearChatBanner();
// A tab that reconnects after the human answered from another one.
chat.handleEvent({ kind: "attention", title: "demo: schematic checkpoint", body: "Pin comments" }, { replayed: false });
chat.handleEvent({ kind: "user_turn", turn: 9, blocks: [{ type: "text", text: "done" }] }, { replayed: true });
out.afterReplayedAnswer = store.getState().chat.banner;

// Ready: a message is sent. It is a "You" row marked sending from the moment
// of sending, and is refused after the human started another.
chat.handleHello({ id: "s", agent: "ready" });
const liveRows = () => [...new Set(el("chatLog").children)]
  .filter((r) => (r.className || "").includes("pendingturn") && !r.removed);
const rowInfo = () => liveRows().map((r) => ({
  cls: r.children[0].className, text: r.children[0].children[0].textContent,
  mark: r.children[0].children[3].textContent, markTitle: r.children[0].children[3].title,
  retry: r.children[0].children[3].children.map((b) => b.textContent),
}));
const pendingStates = () => store.getState().chat.pendingUser.map((p) => p.state);
sendMessage("first");
const firstId = sent[0].client_id;
out.clearedOnSend = input.value;
out.sendingRow = rowInfo();
input.value = "draft";
chat.handleRefused("the agent is connecting, so this message was not sent", firstId);
out.afterRefusal = input.value;
out.refusalToast = el("toast").textContent;
// The row stays, marked "Not sent" with the reason and a Retry; the pane is
// no longer busy for it.
out.refusedRow = rowInfo();
out.refusedStates = pendingStates();
out.refusedBusy = el("chatSend").textContent;
// Retry sends it again under a new client id and the row is sending again.
const framesAtRetry = sent.length;
liveRows()[0].children[0].children[3].children[0].listeners.click[0]();
const retryId = sent[sent.length - 1].client_id;
out.retried = { frames: sent.length - framesAtRetry, type: sent[sent.length - 1].type,
  newId: retryId !== firstId, rows: rowInfo(), states: pendingStates() };
// The server logs it: the confirmed row replaces the pending one, once.
chat.handleEvent({ kind: "user_turn", turn: 77, blocks: [{ type: "text", text: "first" }],
  client_id: retryId, at: 5000 }, { replayed: false });
out.afterLogged = { rows: rowInfo(), states: pendingStates(), startedAt: store.getState().chat.turns.find((t) => t.turn === 77).startedAt };
chat.handleEvent({ kind: "turn_end", turn: 77, stop_reason: "end_turn", cost_usd: 0, at: 9000 }, { replayed: false });
out.endedAt = store.getState().chat.turns.find((t) => t.turn === 77).endedAt;
out.diagForMessage = windowEvents
  .filter(([type, d]) => type === "ember:diag" && (d.client_id === firstId || d.client_id === retryId))
  .map(([, d]) => d.event + ":" + (d.client_id === firstId ? "first" : "retry"));

// A message lost as the connection dropped: the next hello's replay has no
// user_turn for it, so once the first live event arrives it is sent once more
// on the new connection under the same client id.
input.value = "";
sendMessage("lost one");
const lostId = sent[sent.length - 1].client_id;
out.lostRowSending = rowInfo();
const framesBeforeDrop = sent.length;
chat.handleHello({ id: "s", agent: "ready" });
chat.handleEvent({ kind: "text_delta", turn: 1, text: "old" }, { replayed: true });
out.beforeReplayEnds = { composer: input.value, resent: sent.length - framesBeforeDrop };
chat.handleEvent({ kind: "viewer_primary" }, { replayed: false });
out.afterReplayEnds = {
  composer: input.value, toast: el("toast").textContent, states: pendingStates(),
  resent: sent.slice(framesBeforeDrop).map((f) => [f.type, f.client_id === lostId, f.blocks[0].text]),
};
// Resent once only: a second drop with it still unanswered hands it back,
// with the reason, and its row says it was not sent.
chat.handleHello({ id: "s", agent: "ready" });
chat.handleEvent({ kind: "viewer_primary" }, { replayed: false });
out.lostToast = el("toast").textContent;
out.lostComposer = input.value;
out.lostRow = rowInfo();
out.pending = pendingStates();
out.framesAfterSecondDrop = sent.length - framesBeforeDrop;

// A held message released at live goes out after the connection began: its
// hello cannot vouch for it either way, so it is not "lost".
input.value = "";
sendMessage("held one");
chat.adoptSent(sent[sent.length - 1].client_id);
chat.handleHello({ id: "s", agent: "ready" });
chat.handleEvent({ kind: "viewer_primary" }, { replayed: false });
out.heldRestored = input.value;
out.heldPending = store.getState().chat.pendingUser.filter((p) => p.state === "sending").length;

// Usage, before the agent status: from the hello, then from live events only.
const usageChip = el("agentStatus").inserted.find((n) => n.className === "chatusage");
chat.handleHello({ id: "s", agent: "ready", usage: {
  cost_usd: 0.4312,
  tokens: { input: 1200, output: 300, cache_read: 45000, cache_write: 900 },
  context: { used_tokens: 24500, window_tokens: 200000 },
} });
const readouts = () => usageChip.children.map((r) => ({
  usage: r.dataset.usage, val: r.children[0].textContent, unit: r.children[1].textContent,
  hidden: r.hidden, title: r.title }));
out.usageFromHello = { readouts: readouts(), title: usageChip.title, hidden: usageChip.hidden };
out.usageSummaryText = usageSummary({ cost_usd: 0.4312, tokens: {},
  context: { used_tokens: 24500, window_tokens: 200000 } }).text;
out.usageZeroCost = usageSummary({ cost_usd: 0, tokens: {}, context: null }).cost.value;
chat.handleEvent({ kind: "usage", cost_usd: 0.01, tokens: {}, context: null }, { replayed: true });
out.usageAfterReplay = readouts().map((r) => r.val);
chat.handleEvent({ kind: "usage", cost_usd: null,
                   tokens: { input: 5, output: null, cache_read: null, cache_write: null },
                   context: { used_tokens: 150000, window_tokens: 200000 } }, { replayed: false });
out.usageLive = { readouts: readouts(), title: usageChip.title };
chat.handleHello({ id: "s", agent: "ready", usage: null });
out.usageHiddenWhenUnknown = usageChip.hidden;
chat.handleHello({ id: "s", agent: "ready", usage: { cost_usd: 1, tokens: {}, context: null } });
chat.handleEvent({ kind: "session_reset", reason: "new" }, { replayed: false });
out.usageHiddenAfterReset = usageChip.hidden;

// A document's chip says how its action ended, and offers the action no more.
const docId = store.reserveChatDocument("sheet.pdf");
store.updateChatDocument(docId, { state: "done", upload: "u-1", bytes: 10,
  actions: [{ name: "datum", label: "Submit to Datum", accepts: "application/pdf" }] });
chat.handleEvent({ kind: "upload_action", id: "ua_1", upload: "u-1", label: "Submit to Datum",
  file: "sheet.pdf", bytes: 10, tool: "mcp__datum__submit", outcome: "done", text: "job 1" },
  { replayed: false });
const endedDoc = store.getState().chat.documents.find((d) => d.id === docId);
out.documentAfterAction = { state: endedDoc.state, message: endedDoc.message };
// A Datum upload asks for an explicit source URL and sends it with the action.
const { startUploadAction } = await import(pathToFileURL(dir + "/uploads.js").href);
const sourceUrl = "https://manufacturer.example/sheet.pdf";
let actionRequest = null;
globalThis.prompt = (label) => { out.sourceUrlPrompt = label; return sourceUrl; };
globalThis.fetch = async (url, options) => {
  actionRequest = { url, body: JSON.parse(options.body) };
  return { ok: true, json: async () => ({ ok: true }) };
};
const sourceDocId = store.reserveChatDocument("sheet.pdf");
store.updateChatDocument(sourceDocId, { state: "done", upload: "u-2", name: "sheet.pdf",
  actions: [{ name: "datum", label: "Submit to Datum", source_url_label: "Original URL" }] });
await startUploadAction(sourceDocId, "datum", "Original URL");
out.uploadActionRequest = actionRequest;

// Permission cards across connections.
const pendingOf = () => store.getState().chat.pending.map((p) => [p.request_id, p.submitted || null]);
const allowOn = (index) => el("chatPending").children[index].children[3].children[0].listeners.click[0]();
chat.handleEvent({ kind: "permission_request", request_id: "pr_a_1", tool: "t", input: {} }, { replayed: false });
online = false;
allowOn(0);
out.cardWhileOffline = { pending: pendingOf(), toast: el("toast").textContent };
online = true;
allowOn(0);
out.cardSent = pendingOf();
chat.handleHello({ id: "s", agent: "ready" });
out.cardAfterReconnect = pendingOf();
chat.handleHello({ id: "another", agent: "ready" });
out.cardsAfterNewSession = pendingOf();

// The pane's state attributes, through a turn and its permission requests.
let clock = 1000;
Date.now = () => clock;
const paneData = () => ({ ...el("chat").dataset });
const live = { replayed: false };
const replay = { replayed: true };
out.paneIdle = paneData();
chat.handleEvent({ kind: "user_turn", turn: 1, blocks: [{ type: "text", text: "go" }] }, live);
out.paneStarted = paneData();
clock = 2000;
chat.handleEvent({ kind: "text_delta", turn: 1, text: "working" }, live);
out.paneLater = paneData();
chat.handleEvent({ kind: "permission_request", request_id: "pr_b_1", tool: "t", input: {} }, live);
chat.handleEvent({ kind: "permission_request", request_id: "pr_b_2", tool: "t", input: {} }, live);
out.paneTwoOpen = paneData();
chat.handleEvent({ kind: "permission_resolved", request_id: "pr_b_1", outcome: "allow" }, live);
out.paneOneOpen = paneData();
chat.handleEvent({ kind: "permission_resolved", request_id: "pr_b_2", outcome: "deny" }, live);
// A reconnect mid-turn: the replay repeats the turn, which keeps its start.
clock = 3000;
chat.handleHello({ id: "another", agent: "ready" });
chat.handleEvent({ kind: "user_turn", turn: 1, blocks: [{ type: "text", text: "go" }] }, replay);
out.paneAfterReconnect = paneData();
chat.handleEvent({ kind: "turn_end", turn: 1, stop_reason: "end_turn", cost_usd: 0 }, live);
out.paneEnded = paneData();
// A turn the server ended as interrupted (an interrupt, or the agent gone).
clock = 4000;
chat.handleEvent({ kind: "user_turn", turn: 2, blocks: [{ type: "text", text: "again" }] }, live);
out.paneSecondStarted = paneData();
chat.handleEvent({ kind: "turn_end", turn: 2, stop_reason: "interrupted", cost_usd: 0 }, live);
out.paneInterrupted = paneData();
// A replayable steered boundary: the new turn is logged before the old one ends.
clock = 10000;
chat.handleEvent({ kind: "user_turn", turn: 3, blocks: [{ type: "text", text: "do x" }] }, live);
clock = 70000;
chat.handleEvent({ kind: "user_turn", turn: 4, blocks: [{ type: "text", text: "y too" }] }, live);
chat.handleEvent({ kind: "turn_end", turn: 3, stop_reason: "steered", cost_usd: 0 }, live);
out.paneSteered = paneData();
chat.handleEvent({ kind: "turn_end", turn: 4, stop_reason: "end_turn", cost_usd: 0.0412,
                   tokens: { input: 6120, output: 814 } }, live);
out.paneSteerEnded = paneData();
// A page that connects mid-turn: the replay is the first it hears of it.
clock = 90000;
chat.handleHello({ id: "third", agent: "ready" });
chat.handleEvent({ kind: "user_turn", turn: 1, blocks: [{ type: "text", text: "hi" }] }, replay);
chat.handleEvent({ kind: "text_delta", turn: 1, text: "on it" }, replay);
chat.handleEvent({ kind: "permission_request", request_id: "pr_c_1", tool: "t", input: {} }, replay);
out.paneMidTurnConnect = paneData();
chat.handleEvent({ kind: "permission_resolved", request_id: "pr_c_1", outcome: "allow" }, replay);
chat.handleEvent({ kind: "session_reset", reason: "new" }, live);
out.paneAfterReset = paneData();

// Queue delivery states remain visible on their own turn; a reset-tagged turn
// shows unknown attribution, never a phantom zero-cost "steered" turn.
clock = 100000;
chat.handleHello({ id: "queued", agent: "ready", steers: false });
const deliverySnapshot = () => {
  const turns = store.getState().chat.turns;
  const log = el("chatLog");
  const spans = (node) => {
    if (!node || typeof node !== "object") return [];
    return [node, ...(node.children || []).flatMap(spans)];
  };
  return turns.map((t) => {
    // The harness does not detach old rows on remove(); pick the newest row
    // for this session's turn number.
    const row = [...log.children].reverse().find((r) => r.dataset?.turn === String(t.turn));
    return {
      turn: t.turn, state: t.inputState, complete: t.complete,
      cost: t.costUsd, source: t.usageSource,
      deliveryVisible: spans(row).some((n) => (
        (n.className || "").split(" ").includes("turninputstate-" + t.inputState)
        && !!n.textContent
      )),
      detailVisible: !t.inputDetail || spans(row).some((n) => (
        (n.textContent || "").includes(t.inputDetail)
      )),
    };
  });
};
chat.handleEvent({ kind: "user_turn", turn: 1, blocks: [{ type: "text", text: "first" }] }, live);
chat.handleEvent({ kind: "input_state", input_id: "i1", turn: 1, state: "received" }, live);
chat.handleEvent({ kind: "input_state", input_id: "i1", turn: 1, state: "queued" }, live);
out.queuedDelivery = deliverySnapshot();
chat.handleEvent({ kind: "input_state", input_id: "i1", turn: 1, state: "provider_ack" }, live);
chat.handleEvent({
  kind: "turn_end", turn: 1, stop_reason: "end", cost_usd: null, tokens: null,
  usage_epoch: 1, usage_source: "counter_regression",
}, live);
out.acknowledgedDelivery = deliverySnapshot();
chat.handleEvent({
  kind: "user_turn", turn: 2, blocks: [{ type: "text", text: "uncertain" }],
}, live);
chat.handleEvent({
  kind: "input_state", input_id: "i2", turn: 2, state: "uncertain",
  detail: "pipe closed after send",
}, live);
chat.handleEvent({
  kind: "input_state", input_id: "i3", turn: 3, state: "rejected",
  detail: "backend declined the input",
}, live);
out.finalDelivery = deliverySnapshot();

// The model field: the listed models as its suggestions, emptied on focus so
// all of them are offered, and the current model back if nothing is typed.
chat.handleHello({ id: "m", agent: "ready", model: "opus", models: ["opus", "haiku"] });
const modelInput = el("chatModelInput");
const fire = (type) => { for (const fn of modelInput.listeners[type] || []) fn({ key: "" }); };
const focusModel = () => { document.activeElement = modelInput; fire("focus"); };
const blurModel = () => { document.activeElement = null; fire("blur"); fire("change"); };
out.modelOptions = modelInput.inserted.find((n) => n.id === "chatModelList").children.map((o) => o.value);
focusModel();
out.modelOnFocus = [modelInput.value, modelInput.placeholder];
blurModel();
out.modelOnBlurUntyped = modelInput.value;
sent.length = 0;
focusModel();
modelInput.value = "hai";
// The window losing and regaining focus refocuses the field mid-edit.
fire("blur"); fire("focus");
out.modelAfterRefocus = modelInput.value;
modelInput.value = "haiku";
blurModel();
out.modelSent = sent.filter((f) => f.type === "set_model").map((f) => f.model);

// Recovery requires backend acknowledgement or actual model output, never
// a send, queue entry, terminal failure, or old output replayed on reconnect.
chat.handleHello({ id: "recovery", agent: "ready" });
const failure = { kind: "agent_error", remediation: "Check credentials", stderr: "No API key found" };
const user = (turn) => ({ kind: "user_turn", turn, blocks: [{ type: "text", text: "try again" }] });
chat.handleEvent(user(1));
chat.handleEvent(failure);
chat.handleEvent({ kind: "turn_end", turn: 1, stop_reason: "error", cost_usd: null });
sendMessage("retry");
out.errorAfterSend = store.getState().chat.banner.text;
chat.handleEvent(user(2));
chat.handleEvent({ kind: "input_state", turn: 2, state: "queued" });
out.errorWhileQueued = store.getState().chat.banner.text;
chat.handleEvent({ kind: "text_delta", turn: 1, text: "old output" }, { replayed: true });
out.errorAfterOldActivity = store.getState().chat.banner.text;
chat.handleEvent({ kind: "input_state", turn: 2, state: "provider_ack" });
out.errorAfterAck = store.getState().chat.banner;
out.errorHistory = store.getState().chat.turns.find((t) => t.turn === 1).errors;
const recoveryRow = (n) => [...el("chatLog").children].reverse().find((r) => r.dataset.turn === String(n));
out.errorHistoryVisible = recoveryRow(1).children[1].children[1].children[0].children.map((n) => n.textContent);

out.activityRecovery = [];
for (const event of [
  { kind: "text_delta", turn: 2, text: "Working." },
  { kind: "tool_use", turn: 2, tool_use_id: "recover", name: "read", input: {} },
]) {
  chat.handleEvent(failure);
  chat.handleEvent(event);
  out.activityRecovery.push(store.getState().chat.banner);
}
store.setChatBanner("error", "Export failed");
chat.handleEvent({ kind: "text_delta", turn: 2, text: " Still working." });
out.unrelatedErrorSurvives = store.getState().chat.banner.text;
chat.handleEvent(failure);
chat.handleHello({ id: "recovery", agent: "ready", agent_error: null });
out.errorAfterRecoveredHello = store.getState().chat.banner;

// Rebuild the failed turn from history, without announcing the failure as
// current. New queued input must not steal errors from the executing turn.
chat.handleHello({ id: "replay", agent: "ready", agent_error: null });
chat.handleEvent(user(1), { replayed: true });
chat.handleEvent(failure, { replayed: true });
chat.handleEvent({ kind: "turn_end", turn: 1, stop_reason: "error", cost_usd: null }, { replayed: true });
chat.handleEvent(user(2), { replayed: true });
chat.handleEvent({ kind: "input_state", turn: 2, state: "provider_ack" }, { replayed: true });
chat.handleEvent(user(3), { replayed: true });
chat.handleEvent({ kind: "input_state", turn: 3, state: "queued" }, { replayed: true });
chat.handleEvent(failure, { replayed: true });
out.replayedHistory = store.getState().chat.turns.map((t) => [t.turn, t.errors.length]);
out.bannerAfterHistory = store.getState().chat.banner;

// Hundreds of calls are one initially closed disclosure inside their turn.
for (let i = 0; i < 120; i++) {
  chat.handleEvent({ kind: "tool_use", turn: 2, tool_use_id: "long-" + i, name: "read", input: { i } });
  chat.handleEvent({ kind: "tool_result", tool_use_id: "long-" + i, is_error: i === 4, text: "result " + i });
}
const segmentContainer = (n) => recoveryRow(n).children[1].children[0];
const group = segmentContainer(2).children[0];
const tools = group.children[1];
out.toolGroup = {
  tag: group.tagName, className: group.className, collapsed: !group.open,
  summary: group.children[0].textContent, count: tools.children.length,
  nextTurnTools: segmentContainer(3).children.length,
};
group.open = true;
group.listeners.toggle[0]();
tools.scrollTop = 92;
const firstCard = tools.children[0];
firstCard.open = true;
firstCard.listeners.toggle[0]();
firstCard.children[2].scrollTop = 37;
const log = el("chatLog");
log.scrollTop = 240; log.clientHeight = 200; log.scrollHeight = 2000;
chat.handleEvent({ kind: "text_delta", turn: 2, text: " More prose." });
out.inspectionPreserved = [
  group.open, firstCard.open, tools.scrollTop, firstCard.children[2].scrollTop, log.scrollTop,
];
// A deterministic geometry change above a visible anchor is compensated.
let anchorReads = 0;
log.anchorNodes = [{
  isConnected: true,
  getBoundingClientRect() {
    anchorReads++;
    return { top: anchorReads < 3 ? 20 : 80, bottom: 100 };
  },
}];
log.listeners.scroll[0]();
chat.handleEvent({ kind: "text_delta", turn: 2, text: " Anchored." });
out.anchoredScrollTop = log.scrollTop;
log.anchorNodes = [];
log.scrollTop = 1800;
log.listeners.scroll[0]();
chat.handleEvent({ kind: "text_delta", turn: 2, text: " Following." });

chat.handleEvent(user(11));
chat.handleEvent({ kind: "tool_use", turn: 11, tool_use_id: "stage-review",
  name: "supervisor_stage_review", input: {} });
chat.handleEvent({ kind: "tool_result", tool_use_id: "stage-review", is_error: false, text: "review recorded" });
const stageReviewRow = [...el("chatLog").children].find((row) => row.dataset.turn === "11");
out.singleToolSummary = stageReviewRow.children[1].children[0].children[0].children[0].textContent;
out.followingScrollTop = log.scrollTop;

// Two tool runs must stay BETWEEN their surrounding prose, not collected at
// the bottom. Empty text and result events do not split adjacent calls.
chat.handleHello({ id: "inline", agent: "ready", agent_error: null });
chat.handleEvent(user(9));
chat.handleEvent({ kind: "text_delta", turn: 9, text: "Alpha" });
chat.handleEvent({ kind: "text_delta", turn: 9, text: "." });
chat.handleEvent({ kind: "tool_use", turn: 9, tool_use_id: "a1", name: "read", input: { a: 1 } });
chat.handleEvent({ kind: "tool_result", tool_use_id: "a1", is_error: false, text: "early" });
chat.handleEvent({ kind: "text_delta", turn: 9, text: "" });
chat.handleEvent({ kind: "tool_use", turn: 9, tool_use_id: "a2", name: "read", input: { a: 2 } });
chat.handleEvent({ kind: "text_delta", turn: 9, text: "Beta." });
chat.handleEvent({ kind: "tool_use", turn: 9, tool_use_id: "b1", name: "write", input: { b: 1 } });
chat.handleEvent({ kind: "text_delta", turn: 9, text: "Gamma." });
const orderedNodes = [...segmentContainer(9).children];
const runA = orderedNodes[1];
const runB = orderedNodes[3];
runA.open = true;
runA.listeners.toggle[0]();
runB.open = true;
runB.listeners.toggle[0]();
const cardA = runA.children[1].children[0];
cardA.open = true;
cardA.listeners.toggle[0]();
runA.children[1].scrollTop = 42;
cardA.children[2].scrollTop = 17;
chat.handleEvent(user(10));
chat.handleEvent({ kind: "tool_use", turn: 10, tool_use_id: "next", name: "read", input: {} });
chat.handleEvent({ kind: "tool_result", tool_use_id: "a1", is_error: true, text: "late earlier result" });
chat.handleEvent({ kind: "tool_use", turn: 9, tool_use_id: "a1", name: "read", input: {} }, { replayed: true });
chat.handleEvent({ kind: "tool_result", tool_use_id: "a2", is_error: false, text: "second result" });
const nextRun = segmentContainer(10).children[0];
nextRun.open = true;
nextRun.listeners.toggle[0]();
out.inlineOrder = segmentContainer(9).children.map((n) => n.className === "text"
  ? "text:" + n.innerHTML
  : "tools:" + n.children[1].children.map((c) => c.dataset.toolId).join(","));
out.inlineLateResult = {
  sameNodes: segmentContainer(9).children.length === orderedNodes.length
    && orderedNodes.every((n, i) => segmentContainer(9).children[i] === n),
  sameCard: runA.children[1].children[0] === cardA,
  result: cardA.children[2].textContent,
  summaryA: runA.children[0].textContent,
  summaryB: runB.children[0].textContent,
  inspector: [runA.open, cardA.open, runA.children[1].scrollTop, cardA.children[2].scrollTop],
  nextTurn: segmentContainer(10).children[0].children[1].children.map((c) => c.dataset.toolId),
};

// Different tools get a line each; only the same tool in a row is counted.
chat.handleHello({ id: "mixed", agent: "ready", agent_error: null });
chat.handleEvent(user(12));
for (const [id, name] of [["m1", "read"], ["m2", "read"], ["m3", "edit_file"],
    ["m4", "edit_file"], ["m5", "edit_file"], ["m6", "read"]]) {
  chat.handleEvent({ kind: "tool_use", turn: 12, tool_use_id: id, name, input: {} });
  chat.handleEvent({ kind: "tool_result", tool_use_id: id, is_error: id === "m3", text: "x" });
}
out.mixedTools = segmentContainer(12).children.map((n) => n.children[0].textContent);

chat.handleHello({ id: "inline-replay", agent: "ready", agent_error: null });
for (const event of [
  user(9),
  { kind: "text_delta", turn: 9, text: "Alpha." },
  { kind: "tool_use", turn: 9, tool_use_id: "a1", name: "read", input: {} },
  { kind: "text_delta", turn: 9, text: "" },
  { kind: "tool_result", tool_use_id: "a1", is_error: false, text: "early" },
  { kind: "tool_use", turn: 9, tool_use_id: "a2", name: "read", input: {} },
  { kind: "text_delta", turn: 9, text: "Beta." },
  { kind: "tool_use", turn: 9, tool_use_id: "b1", name: "write", input: {} },
  { kind: "text_delta", turn: 9, text: "Gamma." },
  user(10),
  { kind: "tool_use", turn: 10, tool_use_id: "next", name: "read", input: {} },
  { kind: "tool_result", tool_use_id: "a1", is_error: true, text: "late earlier result" },
  { kind: "tool_use", turn: 9, tool_use_id: "a1", name: "read", input: {} },
]) chat.handleEvent(event, { replayed: true });
for (const run of segmentContainer(9).children.filter((n) => n.className === "toolgroup")) {
  run.open = true;
  run.listeners.toggle[0]();
}
const replayCard = segmentContainer(9).children[1].children[1].children[0];
replayCard.open = true;
replayCard.listeners.toggle[0]();
out.inlineReplayOrder = segmentContainer(9).children.map((n) => n.className === "text"
  ? "text:" + n.innerHTML
  : "tools:" + n.children[1].children.map((c) => c.dataset.toolId).join(","));
out.inlineReplayResult = segmentContainer(9).children[1].children[1].children[0].children[2].textContent;


// Thinking: its own closed block in event order; textContent only; it is
// not model activity, so it leaves an error banner alone.
chat.handleHello({ id: "thinking", agent: "ready", agent_error: null });
chat.handleEvent(user(30));
chat.handleEvent(failure);
chat.handleEvent({ kind: "thinking_delta", turn: 30, text: "weigh it" });
chat.handleEvent({ kind: "thinking_delta", turn: 30, text: " more", truncated: true });
chat.handleEvent({ kind: "text_delta", turn: 30, text: "answer" }, { replayed: true });
chat.handleEvent({ kind: "thinking_delta", turn: 30, text: "again" });
const thinkingNodes = [...segmentContainer(30).children];
out.thinkingBlocks = {
  order: thinkingNodes.map((n) => n.className),
  opened: !!thinkingNodes[0].open,
  body: thinkingNodes[0].children[1].textContent,
  noteHidden: thinkingNodes[0].children[2].hidden,
  bannerKept: store.getState().chat.banner && store.getState().chat.banner.text,
};
// The status line: one answer from the store, drawn in one place.
const T0 = 1800000000000;
clock = T0;
const hhmm = (ms) => new Date(ms).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const dated = (ms) => new Date(ms).toLocaleDateString([], { day: "numeric", month: "short" }) + " " + hhmm(ms);
const status = () => ({
  text: el("agentStatus").textContent, state: el("agentStatus").dataset.state, title: el("agentStatus").title,
});
const hi = [{ type: "text", text: "hi" }];
const fresh = { replayed: false };
// A message sent and not yet logged, with no turn open: starting, never the
// idle or "Finished" of the turn before it; once logged it is working.
for (const p of store.getState().chat.pendingUser) store.dropChatUserTurn(p.clientId);
chat.handleHello({ id: "st0", agent: "ready" });
chat.handleEvent({ kind: "user_turn", turn: 1, blocks: hi, at: T0 - 9000 }, { replayed: false });
chat.handleEvent({ kind: "turn_end", turn: 1, stop_reason: "end_turn", cost_usd: 0, at: T0 - 1000 }, { replayed: false });
out.statusAfterPrevious = status();
sendMessage("again");
out.statusSending = status();
chat.handleEvent({ kind: "user_turn", turn: 2, blocks: hi, at: T0, client_id: sent[sent.length - 1].client_id },
  { replayed: false });
out.statusLogged = status();
store.clearChatBanner();
chat.handleHello({ id: "st1", agent: "ready" });
out.statusNoLog = status();
chat.handleEvent({ kind: "user_turn", turn: 1, blocks: hi, at: T0 - 252000 }, fresh);
out.statusWorking = status();
out.statusTurnStarted = el("chat").dataset.turnStarted;
chat.handleEvent({ kind: "tool_use", turn: 1, tool_use_id: "t1", name: "mcp__loom__stage_review", input: {} }, fresh);
out.statusTool = status().text;
chat.handleEvent({ kind: "tool_result", tool_use_id: "t1", is_error: false, text: "ok" }, fresh);
out.statusToolDone = status().text;
clock = T0 + 60000;
chat.handleEvent({ kind: "text_delta", turn: 1, text: "x" }, fresh);
out.statusLater = status().text;
chat.handleEvent({ kind: "permission_request", request_id: "pr_s", tool: "t", input: {} }, fresh);
out.statusWaiting = status();
chat.handleEvent({ kind: "permission_resolved", request_id: "pr_s", outcome: "allow" }, fresh);
store.setPaused(true);
out.statusPaused = status();
store.setPaused(false);
store.setStarting(true);
out.statusStarting = status();
store.setStarting(false);
out.statusBackToWorking = status().state;
chat.handleEvent({ kind: "turn_end", turn: 1, stop_reason: "end_turn", cost_usd: 0, at: T0 + 5000 }, fresh);
out.statusFinished = { ...status(), expected: "Finished " + hhmm(T0 + 5000), paneWorking: "working" in el("chat").dataset };
clock = T0 + 3 * 86400000;
store.setStarting(true);
store.setStarting(false);
out.statusFinishedOtherDay = { text: status().text, expected: "Finished " + dated(T0 + 5000) };

// A job that ended while the transcript still holds a turn open is never working.
clock = T0;
chat.handleHello({ id: "st2", agent: "ready" });
chat.handleEvent({ kind: "user_turn", turn: 1, blocks: hi, at: T0 - 5000 }, fresh);
store.setHostedJob({ state: "succeeded", reason: null, at: T0 + 9000 });
out.statusJobEndedOpenTurn = {
  ...status(), expected: "Finished " + hhmm(T0 + 9000), paneWorking: "working" in el("chat").dataset,
};
store.setHostedJob({ state: "failed", reason: "ran out of credit", at: T0 + 9000 });
out.statusJobFailed = { ...status(), expected: "Stopped " + hhmm(T0 + 9000) + ": ran out of credit" };
store.setHostedJob({ state: "lost", reason: null, at: null });
out.statusJobLost = status().text;
store.setHostedJob({ state: "running", reason: null, at: null });
out.statusJobRunning = status().state;
store.setHostedJob({ state: "none", reason: null, at: null });

// The agent closing: an error stops it, the ordinary reasons do not.
chat.handleHello({ id: "st3", agent: "unavailable" });
chat.handleEvent({ kind: "agent_closed", reason: "child_exited", detail: "exit code 1", at: T0 + 2000 }, fresh);
out.statusClosedError = { ...status(), expected: "Stopped " + hhmm(T0 + 2000) + ": exit code 1" };
chat.handleEvent({ kind: "agent_closed", reason: "idle", detail: null, at: T0 + 3000 }, fresh);
out.statusClosedNormal = status();
chat.handleHello({ id: "viewer-only", agent: "unavailable" });
out.statusViewerOnly = status();

// No combination of inputs shows an idle or stopped agent with a timer, or a
// working one with no open turn, bar a start (the process starting is shown as working, never as its own state).
const base = store.getState();
const openTurn = { turn: 1, complete: false, segments: [], startedAt: 1000, endedAt: null };
const doneTurn = { turn: 1, complete: true, segments: [], startedAt: 1000, endedAt: 2000 };
const violations = [];
for (const job of ["none", "starting", "running", "succeeded", "failed", "lost"]) {
  for (const turns of [[], [openTurn], [doneTurn]]) {
    for (const starting of [false, true]) {
      for (const paused of [false, true]) {
        for (const agentStatus of ["connecting", "ready", "unavailable", "idle", "closed"]) {
          const a = agentActivity({ ...base, starting, paused, hostedJob: { state: job, reason: null, at: 3000 },
            chat: { ...base.chat, turns, agentStatus, pending: [], banner: null, closed: null } });
          const label = JSON.stringify([job, turns.length && turns[0].complete, starting, paused, agentStatus, a.state]);
          if (a.state !== "working" && a.startedAt !== null) violations.push("timer " + label);
          if (a.state === "working" && !starting && job !== "starting" && agentStatus !== "connecting" && !turns.some((t) => !t.complete)) violations.push("no turn " + label);
          if (["succeeded", "failed", "lost"].includes(job) && !starting && a.state === "working") violations.push("ended " + label);
        }
      }
    }
  }
}
out.activityViolations = violations;
out.connectingBeforeAndAfterHello = [false, true].map((agentKnown) => agentActivity({
  ...base, chat: { ...base.chat, turns: [], agentStatus: "connecting", agentKnown, banner: null, closed: null },
})).map((a) => [a.state, !!a.notConnected]);

// A failure the page cannot recover from is the banner, with its action.
chat.handleHello({ id: "st4", agent: "ready" });
store.clearChatBanner();
out.bannerBefore = el("chatBanner").hidden;
store.setConnectionFailure({ kind: "unreachable", message: "Can't reach the agent. Retrying\u2026", action: "retry" });
const failureButton = el("chatBannerText").inserted[0];
out.failureBanner = {
  hidden: el("chatBanner").hidden, kind: el("chatBanner").dataset.kind, text: el("chatBannerText").textContent,
  action: failureButton.textContent, actionHidden: failureButton.hidden, closeHidden: el("chatBannerClose").hidden,
};
failureButton.listeners.click[0]();
out.failureActions = windowEvents.filter(([type]) => type === "ember:failure-action").map(([, d]) => d.action);
store.setConnectionFailure({ kind: "offline", message: "You're offline. Messages send when you're back.", action: "none" });
out.offlineBanner = { text: el("chatBannerText").textContent, actionHidden: failureButton.hidden };
store.setConnectionFailure(null);
out.failureCleared = {
  hidden: el("chatBanner").hidden, actionHidden: failureButton.hidden, closeHidden: el("chatBannerClose").hidden,
};

sent.length = 0;
const modelLessRoot = {
  nodeType: 9,
  querySelector(selector) {
    return selector === "#chatModelInput" ? null : document.querySelector(selector);
  },
};
initChat({
  send: (frame) => { sent.push(frame); return true; },
  root: modelLessRoot,
  modelControl: false,
});
out.restrictedModelControl = {
  mountedWithoutInput: true,
  sent: sent.filter((f) => f.type === "set_model").map((f) => f.model),
};
console.log(JSON.stringify(out));
process.exit(0);
"""


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    harness = tmp_path_factory.mktemp("chat-js") / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(STATIC)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_page_opened_while_the_agent_is_down_shows_why(observed):
    banner = observed["bannerFromHello"]
    assert (banner["kind"], banner["text"]) == ("error", "log omp in")
    assert "401 no credentials" in json.dumps(banner)
    # Sending is what starts a closed or failed agent again.
    assert observed["sendDisabledWhileDown"] is False
    # Viewer-only: Send, the box, Attach and Interrupt are off, each with a reason.
    assert all(disabled and title for disabled, title in observed["viewerOnly"])
    assert observed["afterViewerOnly"] == [False, False]


def test_only_a_live_agent_error_raises_the_banner(observed):
    assert observed["bannerFromReplay"] is None
    assert observed["bannerFromLive"] == "new trouble"
    assert observed["errorWhileStarting"] == "new trouble"
    assert observed["errorOnceReady"] is None


def test_the_agents_question_leaves_the_banner_when_the_human_answers(observed):
    assert observed["attentionBanner"] == "demo: circuit checkpoint: OK?"
    assert observed["afterAnswer"] is None
    assert observed["errorAfterTurn"] == "still broken"
    assert observed["afterReplayedAnswer"] is None


def test_a_refused_message_goes_back_in_front_of_what_was_typed_since(observed):
    assert observed["clearedOnSend"] == ""
    assert observed["afterRefusal"] == "first\n\ndraft"
    assert observed["refusalToast"] == (
        "the agent is connecting, so this message was not sent; it is back in the box"
    )


def test_a_message_is_a_row_marked_sending_from_the_moment_of_sending(observed):
    sending = {
        "cls": "msg user sending",
        "text": "first",
        "mark": "Sending",
        "markTitle": "",
        "retry": [],
    }
    assert observed["sendingRow"] == [sending]
    # Refused: the row stays as "Not sent" with the reason and a Retry, and the
    # message is also back in the composer.
    assert observed["refusedRow"] == [
        {
            "cls": "msg user notsent",
            "text": "first",
            "mark": "Not sent ",
            "markTitle": "the agent is connecting, so this message was not sent",
            "retry": ["Retry"],
        }
    ]
    assert observed["refusedStates"] == ["failed"]


def test_retry_sends_the_message_again_and_the_server_s_user_turn_replaces_its_row(observed):
    retried = observed["retried"]
    assert (retried["frames"], retried["type"], retried["newId"]) == (1, "turn", True)
    assert retried["rows"] == [
        {
            "cls": "msg user sending",
            "text": "first",
            "mark": "Sending",
            "markTitle": "",
            "retry": [],
        }
    ]
    assert retried["states"] == ["sending"]
    # Logged: the confirmed row takes its place, no duplicate pending row, and
    # the turn carries the server's own times.
    assert observed["afterLogged"] == {"rows": [], "states": [], "startedAt": 5000}
    assert observed["endedAt"] == 9000


def test_the_hops_of_a_message_are_reported_by_client_id_and_never_its_text(observed):
    assert observed["diagForMessage"] == [
        "msg_send:first",
        "msg_shown:first",
        "msg_send:retry",
        "msg_shown:retry",
        "msg_logged:retry",
    ]


def test_a_message_lost_in_a_drop_is_sent_once_more_on_the_new_connection(observed):
    assert observed["lostRowSending"][0]["text"] == "lost one"
    assert observed["beforeReplayEnds"] == {"composer": "", "resent": 0}
    after = observed["afterReplayEnds"]
    # The same client id, so the server can drop a duplicate; nothing restored.
    assert after["resent"] == [["turn", True, "lost one"]]
    assert after["composer"] == "" and after["states"] == ["sending"]
    assert "never arrived" not in after["toast"]


def test_a_message_still_unanswered_after_its_resend_goes_back_with_a_not_sent_row(observed):
    assert observed["lostComposer"] == "lost one"
    assert "never arrived; it is back in the box" in observed["lostToast"]
    assert observed["framesAfterSecondDrop"] == 1, "resent once, not on every connection"
    assert observed["pending"] == ["failed"]
    assert observed["lostRow"] == [
        {
            "cls": "msg user notsent",
            "text": "lost one",
            "mark": "Not sent ",
            "markTitle": "The connection dropped before it arrived",
            "retry": ["Retry"],
        }
    ]


def test_the_status_line_says_what_the_agent_is_doing_and_nothing_else_writes_it(observed):
    assert observed["statusNoLog"]["text"] == "Idle"
    assert observed["statusNoLog"]["state"] == "idle"
    # An open turn: the elapsed time runs from the server's time for it, and
    # the running tool is the detail.
    assert (observed["statusWorking"]["text"], observed["statusWorking"]["state"]) == (
        "Working 4:12",
        "working",
    )
    assert observed["statusTurnStarted"] == str(1800000000000 - 252000)
    assert observed["statusTool"] == "Working 4:12 \u00b7 loom: stage_review"
    assert observed["statusToolDone"] == "Working 4:12"
    assert observed["statusLater"] == "Working 5:12"
    assert (observed["statusWaiting"]["text"], observed["statusWaiting"]["state"]) == (
        "Waiting for you",
        "waiting",
    )
    assert (observed["statusPaused"]["text"], observed["statusPaused"]["state"]) == (
        "Paused",
        "paused",
    )
    assert (observed["statusStarting"]["text"], observed["statusStarting"]["state"]) == (
        "Working",
        "working",
    )
    assert observed["statusBackToWorking"] == "working"


def test_a_finished_turn_shows_its_end_as_a_local_time_with_the_date_when_not_today(observed):
    finished = observed["statusFinished"]
    assert finished["text"] == finished["expected"] and finished["state"] == "idle"
    assert finished["paneWorking"] is False
    other = observed["statusFinishedOtherDay"]
    assert other["text"] == other["expected"]
    assert other["text"] != finished["text"]


def test_a_message_not_yet_logged_is_working_never_idle_or_the_previous_turns_finish(observed):
    assert observed["statusAfterPrevious"]["text"].startswith("Finished ")
    assert (observed["statusSending"]["text"], observed["statusSending"]["state"]) == (
        "Working",
        "working",
    )
    # Logged: its turn is open, so it is working.
    assert observed["statusLogged"]["state"] == "working"


def test_the_agent_is_not_starting_before_anything_has_said_so(observed):
    for case in ("statusBeforeHello", "statusSignedOut"):
        assert observed[case] == {"text": "Not connected", "state": "idle"}, case
    # Only a hello or an agent_status that says "connecting" is the agent starting.
    assert observed["connectingBeforeAndAfterHello"] == [["idle", True], ["working", False]]


def test_a_job_that_ended_never_reads_as_working_whatever_the_transcript_holds(observed):
    ended = observed["statusJobEndedOpenTurn"]
    assert ended["text"] == ended["expected"] and ended["state"] == "idle"
    assert ended["paneWorking"] is False
    failed = observed["statusJobFailed"]
    assert failed["text"] == failed["expected"] and failed["state"] == "stopped"
    assert observed["statusJobLost"] == "Stopped: the agent was lost"
    assert observed["statusJobRunning"] == "working"
    assert observed["activityViolations"] == []


def test_an_agent_that_closed_with_an_error_is_stopped_and_an_ordinary_close_is_not(observed):
    closed = observed["statusClosedError"]
    assert closed["text"] == closed["expected"] and closed["state"] == "stopped"
    assert (observed["statusClosedNormal"]["text"], observed["statusClosedNormal"]["state"]) == (
        "Idle",
        "idle",
    )
    assert observed["statusViewerOnly"]["text"] == "Idle"
    assert observed["statusViewerOnly"]["title"].startswith("This server runs viewer-only")


def test_a_failure_shows_in_the_banner_with_its_action_and_clears_with_the_failure(observed):
    assert observed["bannerBefore"] is True
    assert observed["failureBanner"] == {
        "hidden": False,
        "kind": "error",
        "text": "Can't reach the agent. Retrying\u2026",
        "action": "Retry now",
        "actionHidden": False,
        "closeHidden": True,
    }
    # The pane only tells the window; the page acts on it.
    assert observed["failureActions"] == ["retry"]
    assert observed["offlineBanner"] == {
        "text": "You're offline. Messages send when you're back.",
        "actionHidden": True,
    }
    assert observed["failureCleared"] == {
        "hidden": True,
        "actionHidden": True,
        "closeHidden": False,
    }


def test_the_header_shows_the_conversation_s_usage_from_the_hello_and_live_events(observed):
    tokens_fact = "Tokens: 1,200 in, 300 out, 45,000 read from the cache, 900 written to it"
    assert observed["usageFromHello"] == {
        "readouts": [
            {
                "usage": "context",
                "val": "12%",
                "unit": "context",
                "hidden": False,
                "title": "Context window: 24,500 of 200,000 tokens (12%)",
            },
            {
                "usage": "cost",
                "val": "$0.431",
                "unit": "cost",
                "hidden": False,
                "title": "Cost so far: $0.4312\n" + tokens_fact,
            },
        ],
        "title": "Context window: 24,500 of 200,000 tokens (12%)\n"
        "Cost so far: $0.4312\n" + tokens_fact,
        "hidden": False,
    }
    # A replayed usage event is older than the hello; each is the whole
    # conversation's, so the live one replaces what was shown.
    assert observed["usageAfterReplay"] == ["12%", "$0.431"]
    live = observed["usageLive"]
    assert [(r["usage"], r["val"], r["hidden"]) for r in live["readouts"]] == [
        ("context", "75%", False),
        ("cost", "$0.431", True),
    ]
    assert live["title"] == (
        "Context window: 150,000 of 200,000 tokens (75%)\n"
        "Cost so far: unknown\n"
        "Tokens: 5 in, unknown out, unknown read from the cache, unknown written to it"
    )
    assert observed["usageSummaryText"] == "12% context, $0.431 cost"
    assert observed["usageZeroCost"] == "$0.000", "a zero cost is a figure, not unknown"
    assert observed["usageHiddenWhenUnknown"] is True
    assert observed["usageHiddenAfterReset"] is True, (
        "a new conversation's usage is not the old one's"
    )


def test_a_document_s_chip_says_how_its_action_ended(observed):
    assert observed["documentAfterAction"] == {
        "state": "ended",
        "message": "Submit to Datum: sent",
    }


def test_a_datum_upload_prompts_for_and_sends_the_source_url(observed):
    assert observed["sourceUrlPrompt"] == "Original URL"
    assert observed["uploadActionRequest"] == {
        "url": "/upload/action?t=tok",
        "body": {
            "upload": "u-2",
            "action": "datum",
            "source_url": "https://manufacturer.example/sheet.pdf",
        },
    }


def test_a_card_whose_decision_could_not_be_sent_stays_answerable(observed):
    assert observed["cardWhileOffline"]["pending"] == [["pr_a_1", None]]
    assert "Not sent" in observed["cardWhileOffline"]["toast"]
    assert observed["cardSent"] == [["pr_a_1", "allow"]]


def test_a_new_connection_reconciles_the_cards_shown(observed):
    # The same conversation: the card may be answered again (the replay
    # retires it if the decision did land).
    assert observed["cardAfterReconnect"] == [["pr_a_1", None]]
    # Another conversation: its cards are not this one's.
    assert observed["cardsAfterNewSession"] == []


def test_the_pane_root_says_whether_a_turn_runs_since_when_and_what_waits(observed):
    idle = {"pending": "0"}
    running = {"working": "", "turnStarted": "1000", "pending": "0"}
    assert observed["paneIdle"] == idle
    # The start is when the live user_turn arrived, and stays put.
    assert observed["paneStarted"] == running
    assert observed["paneLater"] == running
    assert observed["paneTwoOpen"] == {**running, "pending": "2"}
    assert observed["paneOneOpen"] == {**running, "pending": "1"}
    assert observed["paneAfterReconnect"] == running
    assert observed["paneEnded"] == idle
    assert observed["paneSecondStarted"] == {**running, "turnStarted": "4000"}
    assert observed["paneInterrupted"] == idle
    # A steer carries on the work the steered turn started.
    assert observed["paneSteered"] == {**running, "turnStarted": "10000"}
    assert observed["paneSteerEnded"] == idle
    # Connected mid-turn: the start is when this page first saw the turn.
    assert observed["paneMidTurnConnect"] == {
        "working": "",
        "turnStarted": "90000",
        "pending": "1",
    }
    assert observed["paneAfterReset"] == idle


def test_delivery_tracking_does_not_add_metadata_to_the_transcript(observed):
    queued = observed["queuedDelivery"]
    assert [(turn["turn"], turn["state"], turn["complete"]) for turn in queued] == [
        (1, "queued", False)
    ]
    acknowledged = observed["acknowledgedDelivery"]
    assert [(turn["turn"], turn["state"], turn["complete"]) for turn in acknowledged] == [
        (1, "provider_ack", True)
    ]
    final = observed["finalDelivery"]
    assert [(turn["turn"], turn["state"]) for turn in final] == [
        (1, "provider_ack"),
        (2, "uncertain"),
        (3, "rejected"),
    ]
    assert all(not turn["deliveryVisible"] for turn in final)
    assert all(turn["cost"] is None for turn in final)
    assert final[0]["source"] == "counter_regression"


def test_the_model_field_offers_the_listed_models_from_an_empty_field(observed):
    assert observed["modelOptions"] == ["opus", "haiku"]
    # Emptied on focus, so the datalist offers every model; the current one
    # stays visible as the placeholder and comes back if nothing is typed.
    assert observed["modelOnFocus"] == ["", "opus"]
    assert observed["modelOnBlurUntyped"] == "opus"


def test_a_half_typed_model_survives_a_refocus_and_is_sent(observed):
    assert observed["modelAfterRefocus"] == "hai"
    assert observed["modelSent"] == ["haiku"]


def test_products_can_omit_the_live_model_picker(observed):
    assert observed["restrictedModelControl"] == {
        "mountedWithoutInput": True,
        "sent": [],
    }


def test_recovery_needs_live_backend_activity_and_preserves_error_history(observed):
    assert observed["errorAfterSend"] == "Check credentials"
    assert observed["errorWhileQueued"] == "Check credentials"
    assert observed["errorAfterOldActivity"] == "Check credentials"
    assert observed["errorAfterAck"] is None
    assert observed["activityRecovery"] == [None, None]
    assert observed["errorAfterRecoveredHello"] is None
    assert observed["unrelatedErrorSurvives"] == "Export failed"
    assert observed["errorHistory"] == [
        {"remediation": "Check credentials", "stderr": "No API key found"}
    ]
    assert observed["errorHistoryVisible"] == ["Check credentials", "No API key found"]
    assert observed["replayedHistory"] == [[1, 1], [2, 1], [3, 0]]
    assert observed["bannerAfterHistory"] is None


def test_long_tool_stream_is_grouped_in_its_turn_and_keeps_inspection_state(observed):
    assert observed["toolGroup"] == {
        "tag": "details",
        "className": "toolgroup",
        "collapsed": True,
        "summary": "read (120), 1 failed",
        "count": 0,
        "nextTurnTools": 0,
    }
    assert observed["inspectionPreserved"] == [True, True, 92, 37, 240]
    assert observed["anchoredScrollTop"] == 300
    assert observed["followingScrollTop"] == 2000


def test_single_tool_run_shows_tool_name_on_the_collapsed_row(observed):
    assert observed["singleToolSummary"] == "supervisor_stage_review"


def test_interleaved_prose_stays_around_its_own_inline_tool_run(observed):
    assert observed["inlineOrder"] == [
        "text:Alpha.",
        "tools:a1,a2",
        "text:Beta.",
        "tools:b1",
        "text:Gamma.",
    ]
    assert observed["inlineLateResult"] == {
        "sameNodes": True,
        "sameCard": True,
        "result": "late earlier result",
        "summaryA": "read (2), 1 failed",
        "summaryB": "write, 1 running",
        "inspector": [True, True, 42, 17],
        "nextTurn": ["next"],
    }
    assert observed["inlineReplayOrder"] == observed["inlineOrder"]
    assert observed["inlineReplayResult"] == "late earlier result"


def test_different_tools_get_their_own_line_and_only_repeats_are_counted(observed):
    assert observed["mixedTools"] == [
        "read (2)",
        "edit_file (3), 1 failed",
        "read",
    ]


def test_thinking_is_a_closed_block_in_event_order_and_never_raises_activity(observed):
    thinking = observed["thinkingBlocks"]
    assert thinking["order"] == ["thinking", "text", "thinking"]
    assert thinking["opened"] is False
    assert thinking["body"] == "weigh it more"
    assert thinking["noteHidden"] is False
    assert thinking["bannerKept"] == "Check credentials"


def test_a_held_message_released_at_live_is_not_restored_as_lost(observed):
    assert observed["heldRestored"] == ""
    assert observed["heldPending"] == 1
