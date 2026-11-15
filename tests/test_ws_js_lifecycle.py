"""The socket client's page lifecycle and failure model (``static/ws.js``),
run under Node with a fake ``WebSocket``, a virtual clock for ``setTimeout``
and ``Date.now``, and a recording stand-in for the page's store.

What these pin: a page that comes back (visible, shown, resumed, online)
replaces a socket that is not open, or has gone quiet for twice the server's
ping interval, at once and without showing anything; a healthy one is left
alone. A failure is entered only after 10 s of visible time without being
live (hidden time does not count), or at once for a definite one (signed out,
protocol moved on), and clears on recovery or a closed gate. The retry action
reconnects now. The diagnostic ``ember:diag`` window events carry the page
lifecycle and each turn frame's write. The same harness shape as
``test_ws_js.py``: a scripted run printing one JSON object.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from annealage_ember import product

STATIC = Path(product.__file__).resolve().parent / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

# The store is replaced by a recorder: this file is about what ws.js tells it,
# not about what the real store does with it.
STORE_STUB = r"""
export const log = [];
let state = { connection: "connecting" };
let subscribers = [];
export function reset() { state = { connection: "connecting" }; subscribers = []; log.length = 0; }
export const store = {
  getState: () => state,
  subscribe(key, fn) { subscribers.push([key, fn]); },
  setConnection(value) {
    state = { ...state, connection: value };
    log.push(["conn", value]);
    for (const [key, fn] of subscribers) if (key === "connection") fn(state);
  },
  setConnectionFailure(failure) { log.push(["fail", failure]); },
  batch: (fn) => fn(),
};
"""

HARNESS = r"""
import { pathToFileURL } from "node:url";
import path from "node:path";

const dir = path.dirname(process.argv[2]);
globalThis.location = { hash: "#t=tok", pathname: "/", search: "", protocol: "http:", host: "x" };
globalThis.history = { replaceState() {} };
globalThis.window = globalThis; // replaced per scenario; ws.js reads it at import (tab id)

// A virtual clock: timers run only when the script advances it.
let clock = 0;
let timers = [];
let nextId = 0;
globalThis.setTimeout = (fn, ms = 0) => { const id = ++nextId; timers.push({ id, at: clock + ms, fn }); return id; };
globalThis.clearTimeout = (id) => { timers = timers.filter((t) => t.id !== id); };
Date.now = () => clock;
Math.random = () => 0.5; // backoff jitter of exactly 1
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
async function advance(ms) {
  const end = clock + ms;
  for (;;) {
    await flush();
    const due = timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    timers = timers.filter((t) => t !== due);
    clock = Math.max(clock, due.at);
    due.fn();
  }
  clock = end;
  await flush();
}

const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor() { this.readyState = 0; this.sent = []; this.on = {}; this.bufferedAmount = 0; sockets.push(this); }
  addEventListener(type, fn) { (this.on[type] ||= []).push(fn); }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() { if (this.readyState === 3) return; this.readyState = 3; (this.on.close || []).forEach((f) => f({ code: 1006 })); }
  open() { this.readyState = 1; (this.on.open || []).forEach((f) => f({})); }
  deliver(frame) { (this.on.message || []).forEach((f) => f({ data: JSON.stringify({ v: 2, ...frame }) })); }
}
globalThis.WebSocket = FakeWebSocket;
let fetchStatus = 400;
globalThis.fetch = async () => ({ status: fetchStatus });

const { store, log, reset } = await import(pathToFileURL(dir + "/store.js").href);
const { initWs } = await import(pathToFileURL(process.argv[2]).href);

let win, doc, diags;
function env({ online = true } = {}) {
  clock = 1000000;
  timers = [];
  sockets.length = 0;
  fetchStatus = 400;
  reset();
  win = new EventTarget();
  win.innerWidth = 800;
  win.innerHeight = 600;
  doc = new EventTarget();
  doc.visibilityState = "visible";
  doc.getElementById = () => ({ textContent: "", style: {}, dataset: {} });
  globalThis.window = win;
  globalThis.document = doc;
  Object.defineProperty(globalThis, "navigator", { value: { onLine: online }, configurable: true, writable: true });
  diags = [];
  win.addEventListener("ember:diag", (e) => diags.push(e.detail));
}
const hello = (extra = {}) => ({ type: "hello", seq: 0, protocol: 2, session: { id: "s1", paused: false }, ...extra });
const goLive = (sock) => { sock.open(); sock.deliver(hello()); };
const setVisibility = (state) => { doc.visibilityState = state; doc.dispatchEvent(new Event("visibilitychange")); };
const failures = () => log.filter((l) => l[0] === "fail").map((l) => l[1]);
const conns = () => log.filter((l) => l[0] === "conn").map((l) => l[1]);
const out = {};

// A frozen tab: 2 minutes pass with no timer running. Coming back replaces
// the socket at once, shows nothing, and the new connection resumes.
env();
initWs({ indicator: null });
goLive(sockets[0]);
await advance(2000);
clock += 120000;
setVisibility("visible");
out.frozen = {
  sockets: sockets.length,
  oldClosed: sockets[0].readyState === 3,
  failures: failures(),
  conns: conns(),
};
goLive(sockets[1]);
out.frozen.afterHello = { conns: conns(), failures: failures(), lastSeq: sockets[1].sent[0].last_seq };

// A healthy socket (a frame heard within twice the ping interval) is left alone.
env();
initWs({ indicator: null });
goLive(sockets[0]);
await advance(4000);
sockets[0].deliver({ type: "ping" });
win.dispatchEvent(new Event("pageshow"));
win.dispatchEvent(new Event("online"));
setVisibility("visible");
out.healthy = { sockets: sockets.length };

// An open socket that has heard nothing for 11 s (under the 15 s liveness
// timer, so nothing else would have noticed yet) is replaced.
env();
initWs({ indicator: null });
goLive(sockets[0]);
await advance(11000);
win.dispatchEvent(new Event("pageshow"));
out.quiet = { sockets: sockets.length, oldClosed: sockets[0].readyState === 3 };

// A socket that is down and waiting out its backoff is replaced at once by
// the network coming back, and the old backoff timer does not add a third.
env();
let fallbacks = 0;
initWs({ indicator: null, onFallback: () => fallbacks++ });
goLive(sockets[0]);
sockets[0].close();
win.dispatchEvent(new Event("online"));
out.down = { sockets: sockets.length };
await advance(600);
out.down.afterBackoff = sockets.length;
out.down.fallbacks = fallbacks;

// Hidden time does not count toward the 10 s. 9 s visible, a minute hidden,
// then 1 s visible: the failure is shown only at the end.
env();
initWs({ indicator: null });
await advance(9000);
setVisibility("hidden");
await advance(60000);
const beforeReturn = failures().length;
setVisibility("visible");
await advance(900);
const early = failures().length;
await advance(200);
out.unreachable = { beforeReturn, early, failures: failures(), conns: conns() };
goLive(sockets[sockets.length - 1]);
out.unreachable.recovered = { last: failures().at(-1), lastConn: conns().at(-1), count: failures().length };

// Offline for 10 s visible says so; the network coming back clears it and
// starts the 10 s again, after which it is unreachable instead.
env({ online: false });
initWs({ indicator: null });
await advance(10100);
const offline = failures().slice();
navigator.onLine = true;
win.dispatchEvent(new Event("online"));
const afterOnline = { failures: failures().length, sockets: sockets.length, conn: conns().at(-1) };
await advance(9000);
const stillQuiet = failures().length;
await advance(1100);
out.offline = { offline, afterOnline, stillQuiet, final: failures().at(-1) };

// Sign-in refused: the probe's 403 is at once and final.
env();
fetchStatus = 403;
initWs({ indicator: null });
sockets[0].close();
await flush();
const refused = { failures: failures(), conns: conns() };
await advance(60000);
refused.sockets = sockets.length;
out.probe403 = refused;

// The front's 401 is at once, and the retries go on beneath it.
env();
fetchStatus = 401;
initWs({ indicator: null });
sockets[0].close();
await flush();
const signedOut = { failures: failures() };
await advance(1000);
signedOut.sockets = sockets.length;
signedOut.conns = conns();
out.probe401 = signedOut;

// A protocol mismatch is at once and says reload.
env();
initWs({ indicator: null });
sockets[0].open();
sockets[0].deliver(hello({ protocol: 3 }));
out.protocol = { failures: failures(), conns: conns() };

// A gate refused with 401 is a sign-out; a closed gate then clears it and
// is no failure however long it stays closed.
env();
let gate = "denied";
let gated = 0;
initWs({
  indicator: null,
  onGated: () => gated++,
  beforeConnect: async () => {
    if (gate === "denied") throw Object.assign(new Error("front"), { status: 401 });
    return false;
  },
});
await flush();
const gateDenied = { failures: failures(), sockets: sockets.length };
gate = "closed";
await advance(600);
const gateClosed = { failures: failures(), gated };
await advance(60000);
out.gate = { denied: gateDenied, closed: gateClosed, long: failures().length, sockets: sockets.length };

// The retry action reconnects now; other actions are not this module's.
env();
initWs({ indicator: null });
await advance(10100);
const shown = failures().length;
win.dispatchEvent(new CustomEvent("ember:failure-action", { detail: { action: "sign_in" } }));
const other = sockets.length;
win.dispatchEvent(new CustomEvent("ember:failure-action", { detail: { action: "retry" } }));
out.retry = { shown, other, sockets: sockets.length, oldClosed: sockets[0].readyState === 3 };

// Lifecycle diagnostics, and the write of a turn frame.
env();
const transport = initWs({ indicator: null });
goLive(sockets[0]);
setVisibility("hidden");
win.dispatchEvent(new Event("pagehide"));
setVisibility("visible");
win.dispatchEvent(new Event("pageshow"));
doc.dispatchEvent(new Event("freeze"));
doc.dispatchEvent(new Event("resume"));
win.dispatchEvent(new Event("offline"));
win.dispatchEvent(new Event("online"));
out.lifecycle = diags.map((d) => d.event);
diags.length = 0;
sockets[0].bufferedAmount = 7;
transport.send({ type: "turn", client_id: "c-1", blocks: [] });
transport.send({ type: "pause", paused: true });
sockets[0].close();
transport.send({ type: "turn", client_id: "c-2", blocks: [] });
out.written = diags.filter((d) => d.event === "msg_written");

// Recovery has no label, only an idle or refused session does.
env();
const indicator = { textContent: "x", title: "", dataset: {} };
initWs({ indicator });
const labels = { connecting: indicator.textContent };
for (const state of ["live", "polling", "idle", "failed"]) {
  store.setConnection(state);
  labels[state] = indicator.textContent;
}
out.labels = labels;

console.log(JSON.stringify(out));
process.exit(0);
"""


@pytest.fixture(scope="module")
def observed(tmp_path_factory):
    work = tmp_path_factory.mktemp("ws-lifecycle")
    for name in ("ws.js", "ui.js", "url.js"):
        shutil.copy(STATIC / name, work / name)
    (work / "store.js").write_text(STORE_STUB, encoding="utf-8")
    harness = work / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(work / "ws.js")], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


UNREACHABLE = {
    "kind": "unreachable",
    "message": "Can't reach the agent. Retrying…",
    "action": "retry",
}
OFFLINE = {
    "kind": "offline",
    "message": "You're offline. Messages send when you're back.",
    "action": "none",
}
SIGNED_OUT = {"kind": "signed_out", "message": "You're signed out.", "action": "sign_in"}


def test_a_frozen_tab_is_replaced_at_once_and_shows_nothing(observed):
    frozen = observed["frozen"]
    assert frozen["sockets"] == 2
    assert frozen["oldClosed"] is True
    assert frozen["failures"] == []
    assert frozen["conns"] == ["live"]
    assert frozen["afterHello"] == {"conns": ["live", "live"], "failures": [], "lastSeq": 0}


def test_a_healthy_socket_is_left_alone_when_the_page_returns(observed):
    assert observed["healthy"] == {"sockets": 1}


def test_a_socket_quiet_for_twice_the_ping_interval_is_replaced(observed):
    assert observed["quiet"] == {"sockets": 2, "oldClosed": True}


def test_the_network_returning_replaces_a_waiting_socket_once(observed):
    down = observed["down"]
    assert down["sockets"] == 2
    # the old backoff timer was cancelled, and polling still began silently
    assert down["afterBackoff"] == 2
    assert down["fallbacks"] == 1


def test_a_failure_waits_for_ten_seconds_of_visible_time(observed):
    unreachable = observed["unreachable"]
    assert unreachable["beforeReturn"] == 0
    assert unreachable["early"] == 0
    assert unreachable["failures"] == [UNREACHABLE]
    assert unreachable["conns"][-1] == "failed"


def test_going_live_clears_the_failure(observed):
    assert observed["unreachable"]["recovered"] == {"last": None, "lastConn": "live", "count": 2}


def test_offline_is_named_after_ten_visible_seconds_and_clears_when_online(observed):
    offline = observed["offline"]
    assert offline["offline"] == [OFFLINE]
    assert offline["afterOnline"] == {"failures": 2, "sockets": 2, "conn": "connecting"}
    assert offline["stillQuiet"] == 2
    assert offline["final"] == UNREACHABLE


def test_a_probe_403_is_an_immediate_final_sign_out(observed):
    probe = observed["probe403"]
    assert probe["failures"] == [SIGNED_OUT]
    assert probe["conns"] == ["refused"]
    assert probe["sockets"] == 1


def test_a_front_401_is_an_immediate_sign_out_and_retries_continue(observed):
    probe = observed["probe401"]
    assert probe["failures"] == [SIGNED_OUT]
    assert probe["sockets"] == 2
    assert "refused" not in probe["conns"]


def test_a_protocol_mismatch_says_reload(observed):
    protocol = observed["protocol"]
    assert protocol["failures"] == [
        {"kind": "protocol", "message": "Loom has been updated.", "action": "reload"}
    ]
    assert protocol["conns"] == ["polling"]


def test_a_closed_gate_clears_a_sign_out_and_is_never_a_failure(observed):
    gate = observed["gate"]
    assert gate["denied"] == {"failures": [SIGNED_OUT], "sockets": 0}
    assert gate["closed"] == {"failures": [SIGNED_OUT, None], "gated": 1}
    assert gate["long"] == 2
    assert gate["sockets"] == 0


def test_the_retry_action_reconnects_now(observed):
    retry = observed["retry"]
    assert retry["shown"] == 1
    assert retry["other"] == 1
    assert retry["sockets"] == 2
    assert retry["oldClosed"] is True


def test_page_lifecycle_is_reported_once_per_change(observed):
    assert observed["lifecycle"] == [
        "page_hidden",
        "page_shown",
        "page_frozen",
        "page_hidden",
        "page_resumed",
        "page_shown",
        "page_offline",
        "page_online",
    ]


def test_each_turn_write_reports_its_socket(observed):
    first, second = observed["written"]
    assert first["client_id"] == "c-1"
    assert (first["ready_state"], first["buffered"], first["ok"]) == (1, 7, True)
    assert second["client_id"] == "c-2"
    assert (second["ready_state"], second["ok"]) == (3, False)
    assert len(observed["written"]) == 2


def test_recovery_states_carry_no_label(observed):
    assert observed["labels"] == {
        "connecting": "",
        "live": "",
        "polling": "",
        "idle": "Idle",
        "failed": "",
    }
