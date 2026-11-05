"""Loopback stand-ins shared by the ``codex_direct`` client tests.

``FakeCodex`` is an ``http.server`` listening on 127.0.0.1 that records every
request it receives and answers through a script, so a test can state both
what the client sent and how many times. ``RawServer`` is a bare TCP listener
for the cases where the test needs the bytes of a connection, or an answer
HTTP framing would get in the way of. ``FakeSource`` is a
``CredentialSource`` that returns scripted leases. Nothing here touches the
network beyond loopback or reads a real credential.
"""

import hashlib
import http.server
import json
import socket
import threading
import time
from dataclasses import dataclass, field

from annealage_ember.codex_direct import CredentialLease, CredentialUnavailable

TOKEN = "eyJhbGciOiJub25lIn0.cd-test-token-" + "A" * 40
ACCOUNT = "acct-00000000-test"
USAGE = {
    "input_tokens": 120,
    "output_tokens": 7,
    "total_tokens": 127,
    "input_tokens_details": {"cached_tokens": 64},
    "output_tokens_details": {"reasoning_tokens": 3},
}

# Only the magic bytes matter to the client; the rest is arbitrary but fixed.
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(255, -1, -1)) * 8
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(range(256)) * 4
GIF = b"GIF89a" + bytes(range(256)) * 4


def make_lease(token=TOKEN, *, account_id=ACCOUNT, credential_id=7, generation=3):
    return CredentialLease(
        access_token=token,
        account_id=account_id,
        credential_id=credential_id,
        identity_key="email:tester@example.com",
        expires_at_ms=2_000_000_000_000,
        generation=generation,
        token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        obtained_monotonic=time.monotonic(),
        server_now_ms=1_900_000_000_000,
    )


class FakeSource:
    """Returns each scripted item in turn (a lease, or an exception to raise); the last repeats."""

    def __init__(self, *items):
        self.items = list(items) or [make_lease()]
        self.calls = []

    def lease(self, *, min_remaining_ms, wait_s):
        self.calls.append((min_remaining_ms, wait_s))
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if isinstance(item, Exception):
            raise item
        return item


def unavailable(reason="no_match", detail="no credential for that identity"):
    return CredentialUnavailable(reason, detail)


@dataclass
class Received:
    method: str
    path: str
    header_pairs: list
    body: bytes
    headers: dict = field(init=False)

    def __post_init__(self):
        self.headers = {k.lower(): v for k, v in self.header_pairs}

    def count(self, name):
        return sum(1 for k, _ in self.header_pairs if k.lower() == name.lower())


class FakeCodex:
    """A loopback HTTP server; ``script(received, handler)`` writes each response."""

    def __init__(self, script):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                received = Received(self.command, self.path, list(self.headers.items()), body)
                outer.requests.append(received)
                try:
                    script(received, self)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client closed early, as it does after a terminal event

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def __enter__(self):
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def event(kind, **fields):
    """One SSE event whose JSON payload carries ``type``."""
    payload = json.dumps({"type": kind, **fields})
    return ("event: %s\ndata: %s\n\n" % (kind, payload)).encode("utf-8")


def completed_event(text="hello", *, usage=USAGE, model="gpt-served", status="completed", **extra):
    output = (
        [{"type": "message", "content": [{"type": "output_text", "text": text}]}] if text else []
    )
    response = {"id": "resp_1", "status": status, "model": model, "output": output, **extra}
    if usage is not None:
        response["usage"] = usage
    return event("response.completed", response=response)


def ok_stream(text="hello", usage=USAGE):
    half = len(text) // 2
    return [
        event("response.created", response={"id": "resp_1", "status": "in_progress"}),
        event("response.output_text.delta", delta=text[:half]),
        event("response.output_text.delta", delta=text[half:]),
        completed_event(text, usage=usage),
    ]


def respond_json(handler, status, body=b'{"error":{"message":"nope"}}', headers=()):
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def respond_sse(handler, chunks, status=200, delay=0.0):
    handler.send_response(status)
    handler.send_header("content-type", "text/event-stream")
    handler.end_headers()
    for chunk in chunks:
        handler.wfile.write(chunk)
        handler.wfile.flush()
        if delay:
            time.sleep(delay)


class _Connection:
    def __init__(self):
        self.data = bytearray()
        self.done = threading.Event()


class RawServer:
    """A TCP listener; ``behaviour(sock, connection)`` handles each accepted socket."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.connections = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.url = "http://127.0.0.1:%d" % self.port

    def __enter__(self):
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def settle(self):
        """Give the accept thread time to register connections the kernel has queued."""
        time.sleep(0.1)

    def wait_for_connection(self):
        deadline = time.monotonic() + 5
        while not self.connections and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.connections[0]

    def __exit__(self, *exc):
        self.listener.close()

    def _accept(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            connection = _Connection()
            self.connections.append(connection)
            threading.Thread(target=self._serve, args=(sock, connection), daemon=True).start()

    def _serve(self, sock, connection):
        try:
            self.behaviour(sock, connection)
        except OSError:
            pass
        finally:
            sock.close()
            connection.done.set()


def drain(sock, connection):
    """Read until the peer closes, recording every byte."""
    while True:
        data = sock.recv(65536)
        if not data:
            return
        connection.data.extend(data)


def read_request(sock):
    """Read one whole HTTP request (head and content-length body); return its bytes."""
    data = b""
    while b"\r\n\r\n" not in data:
        piece = sock.recv(65536)
        if not piece:
            return data
        data += piece
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(rest) < length:
        piece = sock.recv(65536)
        if not piece:
            break
        rest += piece
    return head + b"\r\n\r\n" + rest


def closed_port():
    """A loopback port nothing listens on."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
