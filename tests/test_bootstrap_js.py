"""The shared page bootstrap and hosted admission, exercised under Node.

admission.js is imported as shipped. bootstrap.js is shipped source with only
its imports replaced by recording stand-ins, so what is checked is its own
wiring: how many WebSocket controllers it makes, when it asks for admission,
and what it shows."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from annealage_ember import product

STATIC = Path(product.__file__).resolve().parent / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

HARNESS = r"""
import { readFileSync } from "node:fs";
const dir = process.argv[2];
const { ensureHostedSession } = await import("file://" + dir + "/admission.js");

function reply(status, body, headers = {}) {
  return {
    status, ok: status >= 200 && status < 300,
    headers: { get: (k) => headers[k] ?? headers[k.toLowerCase()] ?? null },
    clone() { return this; }, json: async () => { if (body === undefined) throw new Error("no body"); return body; },
  };
}
function script(steps) {
  const calls = [];
  const fetcher = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET" });
    const step = steps.shift();
    if (!step) throw new Error("unexpected request " + calls.length);
    if (step instanceof Error) throw step;
    return step;
  };
  return { calls, fetcher };
}
const noJob = () => reply(404, { error: { type: "no_job" } });
const ended = () => reply(503, { error: { type: "job_ended" } });
const starting = (retry) => reply(503, { error: { type: "worker_unavailable" } }, retry ? { "Retry-After": retry } : {});
const fastClock = () => { let t = 0; return { now: () => t, sleep: async (ms) => { t += ms; (fastClock.slept ||= []).push(ms); } }; };

async function outcome(steps, extra = {}) {
  const s = script(steps);
  const clock = fastClock();
  fastClock.slept = [];
  let error = null;
  try { await ensureHostedSession({ url: "/p/a/agent/session", fetcher: s.fetcher, ...clock, ...extra }); }
  catch (e) { error = e.message; }
  return { calls: s.calls.map((c) => c.method + " " + c.url), error, slept: fastClock.slept, left: steps.length };
}
const results = {};

// admit once, then read at the prefixed URL until the worker is up
results.admit = await outcome([noJob(), reply(202, {}), starting("2"), reply(200, {})]);
// an ended job is admitted again the same way
results.ended = await outcome([ended(), reply(202, {}), reply(200, {})]);
// a live session is only read
results.existing = await outcome([reply(200, {})]);
// a POST that fails ambiguously is never repeated
results.ambiguous = await outcome([noJob(), new Error("network")]);
// a refusal (even on the POST) ends it, nothing mutated on a GET refusal
results.refusedGet = await outcome([reply(401, {})]);
results.forbiddenPost = await outcome([noJob(), reply(403, {})]);
// not-202 admission is an error, not retried
results.badAdmission = await outcome([noJob(), reply(500, {})]);
// polling is bounded in count and by the deadline, and honours Retry-After
results.bounded = await outcome(Array.from({ length: 50 }, () => starting("0.01")), { pollLimit: 3 });
results.deadline = await outcome(Array.from({ length: 50 }, () => starting("4")), { deadlineMs: 9000 });

// Retry-After is honoured in full, not shortened to a cap
results.longWait = await outcome([starting("10"), reply(200, {})]);
// one longer than the deadline allows is refused up front, without polling early
results.tooLong = await outcome([starting("60")]);
// an HTTP date counts too, measured on the page's clock
{
  const base = Date.now(); let offset = 0; const slept = [];
  const when = new Date(base + 7000).toUTCString();
  const s = script([starting(when), reply(200, {})]);
  await ensureHostedSession({ url: "/u", fetcher: s.fetcher, now: () => base + offset,
    sleep: async (ms) => { offset += ms; slept.push(ms); } });
  results.dateWait = { slept, calls: s.calls.length };
}
// a request that never answers ends at the deadline, with the signal aborted
async function hung(steps, deadlineMs) {
  const signals = [];
  const calls = [];
  const fetcher = (url, init = {}) => {
    calls.push(init.method || "GET"); signals.push(init.signal);
    const step = steps.shift();
    return step === "hang" ? new Promise(() => {}) : Promise.resolve(step);
  };
  const t0 = Date.now(); let error = null;
  try { await ensureHostedSession({ url: "/u", fetcher, deadlineMs }); } catch (e) { error = e.message; }
  return { calls, error, ms: Date.now() - t0, aborted: signals.map((x) => !!x && x.aborted) };
}
results.hungGet = await hung(["hang"], 60);
results.hungPost = await hung([noJob(), "hang"], 60);
results.hungPoll = await hung([starting("0.1"), "hang"], 400);
{
  let seen = null;
  await ensureHostedSession({ url: "/u", fetcher: async (u, init) => { seen = init.signal; return reply(200, {}); } });
  results.signalGiven = seen instanceof AbortSignal;
}

// bootstrap.js, imports replaced
const source = readFileSync(dir + "/bootstrap.js", "utf8").replace(/^import .*;$/gm, "");
const log = { ws: 0, admissions: [], chat: 0 };
function element(id) {
  const listeners = {};
  return { id, hidden: false, disabled: false, textContent: "", dataset: {}, listeners,
    addEventListener(n, f) { listeners[n] = f; }, click() { return listeners.click && listeners.click({ target: this }); } };
}
const elements = Object.fromEntries(["chat", "err", "sessionStart", "pauseBtn", "chatWorkingTime", "chatMore"].map((i) => [i, element(i)]));
globalThis.document = { addEventListener() {}, querySelector: (s) => elements[s.slice(1)] || null };
globalThis.MutationObserver = class { observe() {} };
const handlersOf = {};
const stubs = {
  initChat: () => { log.chat++; return { handleHello() {}, handleEvent() {}, handleRefused() {} }; },
  initPause: () => ({ setPausedFromServer() {} }),
  initReview: () => ({ onEvent: {}, onLive() {}, onFallback() {} }),
  initSettings: () => ({ load() {} }),
  appUrl: (r) => "/p/a/" + r,
  initWs: (h) => { log.ws++; Object.assign(handlersOf, h); return { send() {} }; },
  ensureHostedSession: async ({ url }) => { log.admissions.push(url); if (stubs.fail) throw new Error(stubs.fail); },
};
const names = Object.keys(stubs);
globalThis.__stubs = stubs;
const re = await import("data:text/javascript;base64," + Buffer.from(
  "const {" + names.join(",") + "} = globalThis.__stubs;\n" + source).toString("base64"));
const snap = () => ({ text: elements.sessionStart.textContent, hidden: elements.sessionStart.hidden, err: elements.err.textContent });
const flush = () => new Promise((r) => setTimeout(r, 0));

const page = re.bootstrapAgentPage({ hosted: true, liveWaitMs: 80 });
results.hostedStart = { ...snap(), ws: log.ws, admissions: log.admissions.length };
elements.sessionStart.click(); elements.sessionStart.click(); // a double click
await flush();
results.hostedPending = { ...snap(), disabled: elements.sessionStart.disabled };
results.hostedConnected = { ...snap(), ws: log.ws, admissions: [...log.admissions] };
handlersOf.onFallback();                                      // the finite worker ended
results.hostedFellBack = snap();
elements.sessionStart.click(); await flush();                 // resume
results.hostedResumePending = { ...snap(), disabled: elements.sessionStart.disabled };
await new Promise((r) => setTimeout(r, 150));
results.hostedNeverLive = { ...snap(), disabled: elements.sessionStart.disabled };
results.hostedResumed = { ...snap(), ws: log.ws, admissions: log.admissions.length };
handlersOf.onLive();
results.hostedLive = snap();
stubs.fail = "refused";
handlersOf.onFallback(); elements.sessionStart.click(); await flush();
results.hostedRefused = { ...snap(), ws: log.ws };

// standalone: connects at once, never admits, no button
log.ws = 0; log.admissions.length = 0; elements.sessionStart.hidden = false;
stubs.fail = null;
re.bootstrapAgentPage({});
await flush();
results.standalone = { ws: log.ws, admissions: log.admissions.length, hidden: elements.sessionStart.hidden, text: elements.sessionStart.textContent };
handlersOf.onLive();
results.standaloneLive = elements.sessionStart.hidden;
console.log(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    path = tmp_path_factory.mktemp("js") / "h.mjs"
    path.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(path), str(STATIC)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_a_missing_session_is_admitted_once_and_then_only_read(results):
    r = results["admit"]
    assert r["calls"] == [
        "GET /p/a/agent/session",
        "POST /p/a/agent/session",
        "GET /p/a/agent/session",
        "GET /p/a/agent/session",
    ]
    assert r["error"] is None and r["slept"] == [2000]
    assert results["ended"]["calls"][:2] == ["GET /p/a/agent/session", "POST /p/a/agent/session"]
    assert results["ended"]["error"] is None


def test_a_session_that_exists_is_not_admitted(results):
    assert results["existing"]["calls"] == ["GET /p/a/agent/session"]


def test_an_ambiguous_admission_is_never_repeated(results):
    r = results["ambiguous"]
    assert r["calls"] == ["GET /p/a/agent/session", "POST /p/a/agent/session"]
    assert "not repeated" in r["error"]
    assert results["badAdmission"]["calls"].count("POST /p/a/agent/session") == 1
    assert "500" in results["badAdmission"]["error"]


def test_a_refusal_ends_it_before_anything_is_changed(results):
    assert results["refusedGet"]["calls"] == ["GET /p/a/agent/session"]
    assert "401" in results["refusedGet"]["error"]
    assert "403" in results["forbiddenPost"]["error"]
    assert results["forbiddenPost"]["calls"].count("POST /p/a/agent/session") == 1


def test_waiting_for_a_worker_is_bounded(results):
    assert len([c for c in results["bounded"]["calls"] if c.startswith("GET")]) == 4
    assert "unavailable" in results["bounded"]["error"]
    assert "still starting" in results["deadline"]["error"]
    assert results["deadline"]["slept"] == [4000, 4000]


def test_hosted_start_waits_for_the_button_and_admits_once_for_a_double_click(results):
    assert results["hostedStart"] == {
        "text": "Start session",
        "hidden": False,
        "err": "",
        "ws": 0,
        "admissions": 0,
    }
    assert results["hostedConnected"]["admissions"] == ["/p/a/agent/session"]
    assert results["hostedConnected"]["ws"] == 1


def test_the_button_stays_until_the_socket_is_live_not_just_admitted(results):
    # admitted, but the (new or reused) socket has not said live yet: a send
    # now would be refused as not connected, so no resumed-ready state shows
    assert results["hostedPending"] == {
        "text": "Starting…",
        "hidden": False,
        "err": "",
        "disabled": True,
    }
    assert results["hostedResumePending"] == {
        "text": "Starting…",
        "hidden": False,
        "err": "",
        "disabled": True,
    }
    # a controller that never reports live (it will not report downtime twice)
    # does not leave a dead button: Resume comes back after the wait
    assert results["hostedNeverLive"] == {
        "text": "Resume session",
        "hidden": False,
        "err": "",
        "disabled": False,
    }
    assert results["hostedLive"]["hidden"] is True


def test_a_finite_worker_ending_offers_resume_on_the_same_websocket_controller(results):
    assert results["hostedFellBack"] == {"text": "Resume session", "hidden": False, "err": ""}
    assert results["hostedResumed"]["admissions"] == 2
    assert results["hostedResumed"]["ws"] == 1
    assert results["hostedLive"]["hidden"] is True


def test_a_refused_resume_shows_why_and_keeps_one_controller(results):
    r = results["hostedRefused"]
    assert r["err"] == "refused" and r["text"] == "Resume connection" and r["ws"] == 1


def test_standalone_connects_at_once_with_no_admission(results):
    # connects without admission, and like hosted shows Starting until live
    assert results["standalone"] == {"ws": 1, "admissions": 0, "hidden": False, "text": "Starting…"}
    assert results["standaloneLive"] is True


def test_retry_after_is_honoured_in_full_never_shortened(results):
    assert results["longWait"]["slept"] == [10000] and results["longWait"]["error"] is None
    # 60 s cannot fit a 30 s deadline: refused before any wait or early poll
    assert results["tooLong"]["slept"] == [] and results["tooLong"]["calls"] == [
        "GET /p/a/agent/session"
    ]
    assert "still starting" in results["tooLong"]["error"]
    assert (
        results["dateWait"]["slept"] == [pytest.approx(7000, abs=1000)]
        and results["dateWait"]["calls"] == 2
    )


def test_a_request_that_never_answers_ends_at_the_deadline(results):
    get = results["hungGet"]
    assert get["calls"] == ["GET"] and "still starting" in get["error"] and get["ms"] < 1500
    assert get["aborted"] == [True]
    post = results["hungPost"]
    # an aborted admission is ambiguous: reported, never repeated
    assert post["calls"] == ["GET", "POST"] and "not repeated" in post["error"]
    assert post["aborted"] == [False, True]
    poll = results["hungPoll"]
    assert (
        poll["calls"] == ["GET", "GET"] and "still starting" in poll["error"] and poll["ms"] < 1500
    )
    assert results["signalGiven"] is True
