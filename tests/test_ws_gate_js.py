"""A hosted worker's absence must not cause doomed socket/probe requests."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from annealage_ember import product


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_worker_gate_tracks_idle_running_and_failed_status(tmp_path):
    harness = tmp_path / "gate.mjs"
    harness.write_text(
        r"""
import { pathToFileURL } from 'node:url';
globalThis.window = globalThis;
globalThis.location = { hash: '', pathname: '/', search: '', protocol: 'https:', host: 'x' };
globalThis.history = { replaceState() {} };
const banner = { textContent: '', style: {}, dataset: {} };
globalThis.document = { getElementById: () => banner };
let probes = 0;
globalThis.fetch = async () => { probes++; return { status: 400 }; };
let timers = new Map(), timerId = 0;
globalThis.setTimeout = (fn) => { timers.set(++timerId, fn); return timerId; };
globalThis.clearTimeout = (id) => timers.delete(id);
const sockets = [];
class Socket {
  static OPEN = 1;
  constructor() { this.readyState = 0; this.handlers = {}; sockets.push(this); }
  addEventListener(kind, fn) { this.handlers[kind] = fn; }
  close() { this.handlers.close({ code: 1006 }); }
}
globalThis.WebSocket = Socket;
const { initWs } = await import(pathToFileURL(process.argv[2]).href);
const flush = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
const tick = async () => { const pending = [...timers.values()]; timers.clear(); for(const fn of pending) fn(); await flush(); };
let running = false, calls = 0, fallbacks = 0, gated = 0, failure = null;
const events = [];
initWs({ indicator: null, beforeConnect: async () => {
  calls++; if (failure) throw failure; return running;
}, onFallback: () => fallbacks++, onGated: () => gated++,
  onDiagnostic: (d) => events.push(d.event) });
await flush();
await tick();
const idle = { sockets: sockets.length, probes, fallbacks, gated, calls, events: [...events] };
running = true;
await tick();
const started = sockets.length;
// The running worker ends before the handshake. A probe may diagnose that
// failed attempt, but subsequent retries must check the gate again.
sockets[0].close(); await flush(); running = false; await tick();
const ended = { sockets: sockets.length, calls };
failure = new Error('Status permission denied'); await tick();
const failed = { sockets: sockets.length, message: banner.textContent };
failure = null; running = true; await tick();
console.log(JSON.stringify({ idle, started, ended, failed,
  recovered: { sockets: sockets.length, message: banner.textContent, display: banner.style.display } }));
""",
        encoding="utf-8",
    )
    ws = Path(product.__file__).resolve().parent / "static" / "ws.js"
    result = subprocess.run(
        ["node", str(harness), str(ws)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    # A closed gate is a steady state: no fallback poll, one gate_closed report per episode.
    assert data["idle"] == {
        "sockets": 0,
        "probes": 0,
        "fallbacks": 0,
        "gated": 2,
        "calls": 2,
        "events": ["gate_closed"],
    }
    assert data["started"] == 1
    assert data["ended"]["sockets"] == 1
    assert data["ended"]["calls"] > data["idle"]["calls"]
    assert data["failed"] == {
        "sockets": 1,
        "message": "Live connection unavailable: Status permission denied",
    }
    assert data["recovered"] == {"sockets": 2, "message": "", "display": "none"}
