"""One HTTP POST that streams a Server-Sent Events response, once.

``post_sse`` makes exactly one request on exactly one connection and reports
what happened. It does not retry, sleep on ``Retry-After``, follow a
redirect, honour ``HTTP_PROXY``/``HTTPS_PROXY`` or fall back to another
encoding or transport. A 3xx or 5xx is a result, not a cue to try again.
Anything that sends a second request is a new attempt, and the caller has to
make it.

The module exists because the line between "sent" and "not sent" decides
what a caller may assume about billing. ``http.client`` is used directly so
the connection (DNS, TCP and the TLS handshake) is established by an explicit
``connect()`` before any request is composed. A failure there raises
``NotSent``: no byte of the request has left the process. ``on_sent`` is then
called from inside the connection's first socket write, immediately before
the first request byte goes out, so a caller that records "sent" in a durable
journal does so before the provider can possibly have seen the request. If
``on_sent`` raises, nothing is written.

Every failure after that point, including a reset while the body is still
being sent, is returned in ``TransportResult.error`` rather than raised,
because from there the request may have reached the provider and the caller
must treat it as sent.

The response body bytes are handed to ``on_chunk`` as they arrive, with a
``time.monotonic()`` offset in milliseconds from the moment ``on_sent``
returned. They are the entity body as ``http.client`` decodes it from the
wire (chunked framing removed, no content decoding), delivered untouched. An
incremental ``SseParser`` turns them into events for convenience; the raw
bytes remain the evidence.

A 2xx stream is bounded. Events go to ``on_event`` and are not retained. A
line or event data over ``MAX_LINE``/``MAX_EVENT`` (4 MiB each), or a body
over ``MAX_STREAM`` (64 MiB), ends the read with ``error_phase`` ``limit`` and
no terminal event, after the bytes already received have gone to ``on_chunk``.
The connection is then closed; nothing is retried. A leading UTF-8 byte order
mark is ignored by the parser.
"""

import http.client
import json
import re
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .request import check_header

# Stream types that end a response. `response.cancelled` and `response.done`
# are the reference client's names for a cancelled and a completed response.
TERMINAL_TYPES = frozenset(
    {
        "response.completed",
        "response.done",
        "response.failed",
        "response.incomplete",
        "response.cancelled",
        "error",
    }
)

# Cap on the body kept from a non-2xx reply. A rejection body is read for its
# message, so a larger one is a misbehaving server, not more information.
MAX_ERROR_BODY = 64 * 1024

# Limits on a 2xx stream. The provider's lines and events are a few KiB and a
# whole response well under a MiB, so these are generous ceilings that stop a
# misbehaving peer from growing memory (and the raw file) without bound.
MAX_LINE = 4 * 1024 * 1024
MAX_EVENT = 4 * 1024 * 1024
MAX_STREAM = 64 * 1024 * 1024

_BOM = b"\xef\xbb\xbf"

_READ_SIZE = 64 * 1024
_EOL = re.compile(rb"\r\n|\r|\n")
# What http.client refuses in a request target. Checked up front so the
# refusal happens before a connection is opened, not after.
_TARGET_CONTROL = re.compile(r"[\x00-\x20\x7f]")


class SseLimitExceeded(Exception):
    """The stream broke a size limit: an over-long line, event or response."""


class NotSent(Exception):
    """The request failed before its first byte was written.

    ``detail`` names the error class and message of the connect failure. It
    cannot contain a credential: nothing but the host and port had been used.
    """

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class SseEvent:
    """One dispatched SSE event.

    ``name`` is the ``event:`` field, ``data`` the joined ``data:`` lines,
    ``payload`` the data decoded as a JSON object (else ``None``) and ``type``
    the payload's ``type``, falling back to ``name``. A literal ``[DONE]``
    has type ``stream.done``.
    """

    name: str | None
    data: str
    payload: dict | None
    type: str | None


class SseParser:
    """Incremental SSE decoder: feed bytes in any split, receive whole events.

    Lines end in LF, CRLF or a lone CR; a CR at the end of a chunk is held
    until the next byte shows whether it was half of a CRLF. One leading UTF-8
    byte order mark is dropped, even when its bytes arrive in separate chunks.
    Comment lines are ignored, as are fields other than ``event`` and
    ``data``. ``finish`` dispatches an event still open at end of stream, as
    the reference client did.

    A line longer than ``max_line`` bytes, or an event whose ``data`` exceeds
    ``max_event`` bytes, raises ``SseLimitExceeded`` (defaults ``MAX_LINE`` and
    ``MAX_EVENT``, 4 MiB each): the provider's events are small, so either is
    a misbehaving peer, and the parser must not grow without bound for it.
    """

    def __init__(self, *, max_line=None, max_event=None):
        self._max_line = MAX_LINE if max_line is None else max_line
        self._max_event = MAX_EVENT if max_event is None else max_event
        self._buffer = b""
        self._scanned = 0  # the buffer before this offset holds no line end
        self._start = True
        self._data = []
        self._data_size = 0
        self._name = None

    def feed(self, chunk):
        self._buffer += chunk
        if self._start:
            if len(self._buffer) < len(_BOM) and _BOM.startswith(self._buffer):
                return []
            self._start = False
            if self._buffer.startswith(_BOM):
                self._buffer = self._buffer[len(_BOM) :]
        end = len(self._buffer) - 1 if self._buffer.endswith(b"\r") else len(self._buffer)
        events, pos = [], 0
        for match in _EOL.finditer(self._buffer, self._scanned, end):
            self._line(self._buffer[pos : match.start()], events)
            pos = match.end()
        self._buffer = self._buffer[pos:]
        self._scanned = end - pos
        if len(self._buffer) > self._max_line:
            raise SseLimitExceeded("SSE line longer than %d bytes" % self._max_line)
        return events

    def finish(self):
        events = []
        if self._buffer:
            self._line(self._buffer.rstrip(b"\r"), events)
            self._buffer = b""
        if self._data:
            events.append(self._dispatch())
        return events

    def _line(self, raw, events):
        if len(raw) > self._max_line:
            raise SseLimitExceeded("SSE line longer than %d bytes" % self._max_line)
        if not raw:
            if self._data:
                events.append(self._dispatch())
            self._name = None
        elif not raw.startswith(b":"):
            name, _, value = raw.partition(b":")
            text = (value[1:] if value.startswith(b" ") else value).decode("utf-8", "replace")
            if name == b"data":
                self._data_size += len(value) + 1
                if self._data_size > self._max_event:
                    raise SseLimitExceeded("SSE event longer than %d bytes" % self._max_event)
                self._data.append(text)
            elif name == b"event":
                self._name = text

    def _dispatch(self):
        data, name = "\n".join(self._data), self._name
        self._data, self._name, self._data_size = [], None, 0
        if data == "[DONE]":
            return SseEvent(name, data, None, "stream.done")
        try:
            payload = json.loads(data)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return SseEvent(name, data, None, name)
        kind = payload.get("type")
        return SseEvent(name, data, payload, kind if isinstance(kind, str) else name)


@dataclass
class TransportResult:
    """What came back from one request.

    ``status`` is ``None`` when no response status line was read. ``headers``
    keeps every response header in order, duplicates included. ``body`` is the
    bounded error body of a non-2xx reply (empty for a 2xx, whose bytes went
    to ``on_chunk``). ``event_count`` counts the events decoded; the events
    themselves went to ``on_event``. ``error`` and ``error_phase`` ("send",
    "response", "body", "stream" or "limit") describe a failure after the
    request was sent; ``error`` holds an error class and message only, and
    "limit" means a size limit below ended the read. ``terminal`` is the event
    that ended the stream, or ``None`` if the stream ended without one.
    """

    status: int | None = None
    reason: str = ""
    headers: list = field(default_factory=list)
    body: bytes = b""
    body_truncated: bool = False
    event_count: int = 0
    terminal: SseEvent | None = None
    error: str | None = None
    error_phase: str | None = None


class _Hooked:
    """Run ``before_first_write`` once, immediately before the first socket write."""

    before_first_write = None
    sent = False

    def send(self, data):
        hook, self.before_first_write = self.before_first_write, None
        if hook is not None:
            hook()
            self.sent = True
        super().send(data)


class _HttpConnection(_Hooked, http.client.HTTPConnection):
    pass


class _HttpsConnection(_Hooked, http.client.HTTPSConnection):
    pass


def describe(error):
    """An error class and message, short enough for a journal field."""
    return ("%s: %s" % (type(error).__name__, error))[:300]


def parse_url(url):
    """Split ``url`` into ``(scheme, host, port, request target)`` or raise ``ValueError``."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be http:// or https:// with a host")
    if parts.username or parts.password:
        raise ValueError("url must not carry credentials")
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    if _TARGET_CONTROL.search(target):
        raise ValueError("url contains control characters or spaces")
    return parts.scheme, parts.hostname, parts.port, target


def _read_error_body(response):
    chunks, total, error = [], 0, None
    try:
        while total <= MAX_ERROR_BODY:
            piece = response.read1(min(_READ_SIZE, MAX_ERROR_BODY + 1 - total))
            if not piece:
                break
            chunks.append(piece)
            total += len(piece)
    except (OSError, http.client.HTTPException) as failure:
        error = describe(failure)
    body = b"".join(chunks)
    return body[:MAX_ERROR_BODY], total > MAX_ERROR_BODY, error


def post_sse(
    url, headers, body, *, connect_timeout, read_timeout, on_sent, on_chunk=None, on_event=None
):
    """POST ``body`` to ``url`` once and stream the SSE reply; see the module docstring.

    ``connect_timeout`` bounds DNS, TCP and TLS; ``read_timeout`` bounds each
    wait for response bytes, so a quiet stream is cut off but a slow one that
    keeps producing is not. ``on_sent()`` is called once, before the first
    request byte. ``on_chunk(t_ms, data)`` is called for each piece of a 2xx
    body and ``on_event(event)`` for each decoded event up to and including
    the terminal one; events are not retained. Raises ``NotSent`` if the
    connection could not be established and ``ValueError`` for a malformed URL
    or header, before anything is opened.
    """
    scheme, host, port, target = parse_url(url)
    for name, value in headers.items():
        check_header(name, value)
    if scheme == "https":
        conn = _HttpsConnection(
            host, port, timeout=connect_timeout, context=ssl.create_default_context()
        )
    else:
        conn = _HttpConnection(host, port, timeout=connect_timeout)
    # No automatic reconnect: a second connection could carry a second copy
    # of the request.
    conn.auto_open = 0
    t0 = []
    try:
        try:
            conn.connect()
        except (OSError, UnicodeError) as failure:
            raise NotSent(describe(failure)) from None
        conn.sock.settimeout(read_timeout)

        def before_first_write():
            on_sent()
            t0.append(time.monotonic())

        conn.before_first_write = before_first_write
        send_error = None
        try:
            conn.putrequest("POST", target, skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
        except OSError as failure:
            if not conn.sent:
                raise
            # The server may have answered and closed while the body was
            # still going out; read the answer if there is one.
            send_error = describe(failure)
        result = TransportResult(error=send_error, error_phase="send" if send_error else None)
        try:
            response = conn.getresponse()
        except (OSError, http.client.HTTPException, ValueError) as failure:
            result.error = result.error or describe(failure)
            result.error_phase = result.error_phase or "response"
            return result
        result.status, result.reason = response.status, response.reason
        result.headers = response.getheaders()
        if not 200 <= response.status < 300:
            body_bytes, truncated, failure = _read_error_body(response)
            result.body, result.body_truncated = body_bytes, truncated
            if failure:
                result.error, result.error_phase = failure, "body"
            return result
        _stream(response, result, t0[0], on_chunk, on_event)
        return result
    finally:
        conn.close()


def _stream(response, result, t0, on_chunk, on_event):
    parser = SseParser()
    total = 0
    while True:
        try:
            chunk = response.read1(_READ_SIZE)
        except (OSError, http.client.HTTPException) as failure:
            result.error, result.error_phase = describe(failure), "stream"
            return
        try:
            if chunk:
                if on_chunk is not None:
                    on_chunk((time.monotonic() - t0) * 1000.0, chunk)
                total += len(chunk)
                if total > MAX_STREAM:
                    raise SseLimitExceeded("stream longer than %d bytes" % MAX_STREAM)
                events = parser.feed(chunk)
            else:
                events = parser.finish()
        except SseLimitExceeded as limit:
            # Stop reading; the bytes already handed to on_chunk stay on record.
            result.error, result.error_phase = describe(limit), "limit"
            return
        for event in events:
            result.event_count += 1
            if on_event is not None:
                on_event(event)
            if event.type in TERMINAL_TYPES:
                result.terminal = event
                return
        if not chunk:
            return
