"""Tests for ``codex_direct.transport``: the one-shot POST and the SSE decoder."""

import threading
import time

import pytest
from codex_direct_fakes import (
    FakeCodex,
    RawServer,
    closed_port,
    completed_event,
    drain,
    event,
    ok_stream,
    read_request,
    respond_json,
    respond_sse,
)

from annealage_ember.codex_direct import (
    NotSent,
    SseEvent,
    SseLimitExceeded,
    SseParser,
    post_sse,
    transport,
)
from annealage_ember.codex_direct.transport import MAX_ERROR_BODY, parse_url

HEADERS = {
    "Authorization": "Bearer not-a-real-token",
    "content-type": "application/json",
    "accept-encoding": "identity",
}


class Probe:
    def __init__(self):
        self.log = []
        self.chunks = []
        self.events = []

    def on_sent(self):
        self.log.append("sent")

    def on_chunk(self, t_ms, data):
        self.log.append("chunk")
        self.chunks.append((t_ms, data))


def post(url, *, body=b"{}", headers=HEADERS, probe=None, **kwargs):
    probe = probe or Probe()
    kwargs.setdefault("connect_timeout", 5)
    kwargs.setdefault("read_timeout", 5)
    result = post_sse(
        url + "/codex/responses",
        headers,
        body,
        on_sent=probe.on_sent,
        on_chunk=probe.on_chunk,
        on_event=probe.events.append,
        **kwargs,
    )
    return result, probe


STREAM = (
    b'event: a\r\ndata: {"type":"a","n":1}\r\n\r\n'
    b": a keepalive comment\n"
    b"data: line one\ndata: line two\n\n"
    b'event: b\ndata:{"type":"b"}\n\n'
    b"data: [DONE]\n\n"
    + 'data: {"type":"u","s":"\u00e9\u4e2d"}\n\n'.encode()
    + b'data: {"type":"x"}\r\r'
    b"data: tail"
)

EXPECTED = [
    SseEvent("a", '{"type":"a","n":1}', {"type": "a", "n": 1}, "a"),
    SseEvent(None, "line one\nline two", None, None),
    SseEvent("b", '{"type":"b"}', {"type": "b"}, "b"),
    SseEvent(None, "[DONE]", None, "stream.done"),
    SseEvent(None, '{"type":"u","s":"\u00e9\u4e2d"}', {"type": "u", "s": "\u00e9\u4e2d"}, "u"),
    SseEvent(None, '{"type":"x"}', {"type": "x"}, "x"),
    SseEvent(None, "tail", None, None),
]


def parse(*pieces):
    parser = SseParser()
    events = []
    for piece in pieces:
        events.extend(parser.feed(piece))
    events.extend(parser.finish())
    return events


def test_parser_handles_crlf_lf_cr_comments_multiline_done_and_eof_event():
    assert parse(STREAM) == EXPECTED


def test_parser_gives_the_same_events_for_every_two_way_split():
    for cut in range(len(STREAM) + 1):
        assert parse(STREAM[:cut], STREAM[cut:]) == EXPECTED, cut


def test_parser_gives_the_same_events_fed_a_byte_at_a_time():
    assert parse(*[STREAM[i : i + 1] for i in range(len(STREAM))]) == EXPECTED


def test_parser_holds_a_trailing_cr_until_it_knows_whether_an_lf_follows():
    parser = SseParser()
    assert parser.feed(b'data: {"type":"a"}\r') == []
    assert parser.feed(b"\n") == []  # CRLF: still one line end, not a blank line
    assert [e.type for e in parser.feed(b"\r\n")] == ["a"]


def test_parser_uses_the_event_name_when_the_payload_has_no_type():
    [only] = parse(b'event: error\ndata: {"message":"x"}\n\n')
    assert only.type == "error" and only.payload == {"message": "x"}
    [text] = parse(b"event: error\ndata: not json\n\n")
    assert text.type == "error" and text.payload is None


def test_parser_does_not_dispatch_a_blank_event():
    assert parse(b"\n\n: only comments\n\nevent: x\n\n") == []


BOM_STREAM = b"\xef\xbb\xbf" + b'data: {"type":"a"}\n\ndata: {"type":"b"}\n\n'


def test_a_leading_byte_order_mark_is_dropped_however_it_is_split():
    assert [e.type for e in parse(BOM_STREAM)] == ["a", "b"]
    for cut in range(len(BOM_STREAM) + 1):
        assert [e.type for e in parse(BOM_STREAM[:cut], BOM_STREAM[cut:])] == ["a", "b"], cut
    assert [e.type for e in parse(b"\xef", b"\xbb", b"\xbf", BOM_STREAM[3:])] == ["a", "b"]


def test_only_the_first_byte_order_mark_is_dropped_and_a_partial_one_is_data():
    # A second BOM is part of the first field's name, so no `data` field is seen.
    assert parse(b"\xef\xbb\xbf\xef\xbb\xbfdata: x\n\n") == []
    assert [e.data for e in parse(b"\xef\xbbdata: x\n\ndata: y\n\n")] == ["y"]


def test_an_unterminated_line_over_the_limit_raises_even_when_fed_in_pieces():
    parser = SseParser(max_line=100)
    for _ in range(10):
        parser.feed(b"x" * 10)
    with pytest.raises(SseLimitExceeded):
        parser.feed(b"x")
    assert len(parser._buffer) <= 101


def test_a_terminated_line_over_the_limit_raises():
    with pytest.raises(SseLimitExceeded):
        SseParser(max_line=100).feed(b"data: " + b"x" * 200 + b"\n")


def test_repeated_data_lines_without_a_blank_line_hit_the_event_limit():
    parser = SseParser(max_line=100, max_event=1000)
    with pytest.raises(SseLimitExceeded):
        for _ in range(100):
            parser.feed(b"data: " + b"y" * 50 + b"\n")
    # A blank line resets the count: many events under the limit are fine.
    ok = SseParser(max_line=100, max_event=1000)
    for _ in range(100):
        ok.feed(b"data: " + b"y" * 50 + b"\n\n")


def test_the_parser_limits_default_to_the_module_constants(monkeypatch):
    monkeypatch.setattr(transport, "MAX_LINE", 50)
    with pytest.raises(SseLimitExceeded):
        SseParser().feed(b"x" * 51)


def test_streams_a_200_response_in_chunks_with_receive_timestamps():
    chunks = ok_stream("hello world")
    with FakeCodex(lambda r, h: respond_sse(h, chunks, delay=0.02)) as server:
        result, probe = post(server.url)
    assert result.status == 200 and result.error is None
    assert ("content-type", "text/event-stream") in [(k.lower(), v) for k, v in result.headers]
    assert [e.type for e in probe.events] == [
        "response.created",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.completed",
    ]
    assert result.terminal is probe.events[-1] and result.event_count == 4
    assert b"".join(data for _, data in probe.chunks) == b"".join(chunks)
    times = [t for t, _ in probe.chunks]
    assert times == sorted(times) and times[0] >= 0
    assert times[-1] - times[0] >= 20  # the server paced its writes by 20 ms
    assert probe.log[0] == "sent" and set(probe.log[1:]) == {"chunk"}
    assert len(server.requests) == 1


def test_the_request_goes_out_exactly_as_given():
    body = b'{"model":"m"}' + b" " * (3 * 1024 * 1024)
    with FakeCodex(lambda r, h: respond_json(h, 200, b"{}")) as server:
        post_sse(
            server.url + "/base/codex/responses?x=1",
            {**HEADERS, "x-extra": "1"},
            body,
            connect_timeout=5,
            read_timeout=5,
            on_sent=lambda: None,
        )
    [received] = server.requests
    assert received.method == "POST" and received.path == "/base/codex/responses?x=1"
    assert received.body == body
    assert received.headers["content-length"] == str(len(body))
    assert received.headers["authorization"] == HEADERS["Authorization"]
    assert received.headers["x-extra"] == "1"
    assert received.headers["host"].startswith("127.0.0.1:")
    # One accept-encoding, ours: http.client's own default is suppressed.
    assert received.count("accept-encoding") == 1
    assert received.headers["accept-encoding"] == "identity"


def test_on_sent_runs_before_any_byte_leaves_and_a_failing_on_sent_sends_nothing():
    with RawServer(drain) as server:

        def refuse():
            raise RuntimeError("journal unavailable")

        with pytest.raises(RuntimeError, match="journal unavailable"):
            post_sse(
                server.url + "/codex/responses",
                HEADERS,
                b"{}",
                connect_timeout=5,
                read_timeout=5,
                on_sent=refuse,
            )
        connection = server.wait_for_connection()
        assert connection.done.wait(5)
        assert bytes(connection.data) == b""  # connected, but not one request byte was written


def test_an_oserror_from_on_sent_is_not_mistaken_for_a_send_failure():
    with RawServer(drain) as server:

        def disk_full():
            raise OSError(28, "No space left on device")

        with pytest.raises(OSError, match="No space"):
            post_sse(
                server.url + "/codex/responses",
                HEADERS,
                b"{}",
                connect_timeout=5,
                read_timeout=5,
                on_sent=disk_full,
            )
        connection = server.wait_for_connection()
        assert connection.done.wait(5)
        assert bytes(connection.data) == b""


def test_connect_failure_raises_not_sent_and_never_calls_on_sent():
    probe = Probe()
    with pytest.raises(NotSent) as caught:
        post("http://127.0.0.1:%d" % closed_port(), probe=probe)
    assert probe.log == []
    assert "ConnectionRefusedError" in caught.value.detail


def test_a_failed_tls_handshake_is_not_sent():
    # A plain-HTTP listener answering a TLS ClientHello: the handshake fails inside connect().
    def behaviour(sock, connection):
        sock.recv(4096)
        sock.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")

    probe = Probe()
    with RawServer(behaviour) as server:
        with pytest.raises(NotSent) as caught:
            post(server.url.replace("http://", "https://"), probe=probe)
        assert server.wait_for_connection().done.wait(5)
    assert probe.log == [] and "SSLError" in caught.value.detail


def test_a_stalled_connection_setup_times_out_as_not_sent():
    release = threading.Event()
    probe = Probe()
    with RawServer(lambda sock, connection: release.wait(10)) as server:
        with pytest.raises(NotSent) as caught:
            post(server.url.replace("http://", "https://"), probe=probe, connect_timeout=0.3)
        release.set()
    assert probe.log == [] and "timed out" in caught.value.detail


def test_non_2xx_is_a_result_with_status_headers_and_a_bounded_body():
    big = b"e" * (MAX_ERROR_BODY + 5000)
    with FakeCodex(lambda r, h: respond_json(h, 500, big)) as server:
        result, probe = post(server.url)
    assert result.status == 500 and result.error is None
    assert len(result.body) == MAX_ERROR_BODY and result.body_truncated
    assert probe.chunks == [] and probe.events == [] and result.terminal is None
    assert len(server.requests) == 1


def test_an_error_body_within_the_cap_is_returned_whole():
    with FakeCodex(
        lambda r, h: respond_json(h, 429, b'{"e":1}', [("Retry-After", "30")])
    ) as server:
        started = time.monotonic()
        result, _ = post(server.url)
    assert result.status == 429 and result.body == b'{"e":1}' and not result.body_truncated
    assert ("Retry-After", "30") in result.headers
    assert time.monotonic() - started < 5  # Retry-After is reported, not slept on
    assert len(server.requests) == 1


def test_a_redirect_is_a_result_and_is_not_followed():
    with FakeCodex(lambda r, h: respond_json(h, 200)) as elsewhere:
        target = elsewhere.url + "/elsewhere"

        def redirect(received, handler):
            respond_json(handler, 302, b"", [("Location", target)])

        with FakeCodex(redirect) as origin:
            result, _ = post(origin.url)
    assert result.status == 302 and ("Location", target) in result.headers
    assert len(origin.requests) == 1 and elsewhere.requests == []


def test_the_stream_stops_at_the_terminal_event_without_waiting_for_the_server_to_close():
    release = threading.Event()

    def script(received, handler):
        respond_sse(handler, [event("response.created"), event("response.failed", response={})])
        release.wait(10)

    with FakeCodex(script) as server:
        started = time.monotonic()
        result, _ = post(server.url)
        elapsed = time.monotonic() - started
        release.set()
    assert result.terminal.type == "response.failed" and elapsed < 5


@pytest.mark.parametrize("kind", ["response.completed", "response.incomplete", "error"])
def test_each_terminal_type_ends_the_stream(kind):
    chunks = [
        event("response.created"),
        event(kind),
        event("response.output_text.delta", delta="x"),
    ]
    with FakeCodex(lambda r, h: respond_sse(h, chunks, delay=0.05)) as server:
        result, probe = post(server.url)
    assert result.terminal.type == kind
    assert probe.events[-1] is result.terminal  # nothing after it is parsed
    assert result.event_count == len(probe.events)


def test_a_clean_close_without_a_terminal_event_has_no_terminal_and_no_error():
    chunks = [event("response.created"), event("response.output_text.delta", delta="x")]
    with FakeCodex(lambda r, h: respond_sse(h, chunks)) as server:
        result, probe = post(server.url)
    assert result.status == 200 and result.terminal is None and result.error is None
    assert result.event_count == 2 and len(probe.events) == 2


def test_a_truncated_chunked_stream_reports_a_stream_error_with_the_events_so_far():
    first = event("response.created")

    def behaviour(sock, connection):
        read_request(sock)
        sock.sendall(
            b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
            b"transfer-encoding: chunked\r\n\r\n" + b"%x\r\n" % len(first) + first + b"\r\n"
        )
        # Close without the terminating zero-length chunk.

    with RawServer(behaviour) as server:
        result, probe = post(server.url)
    assert result.status == 200 and result.terminal is None
    assert result.error_phase == "stream" and "IncompleteRead" in result.error
    assert [e.type for e in probe.events] == ["response.created"]
    assert b"".join(data for _, data in probe.chunks) == first


def test_a_connection_closed_without_an_answer_reports_no_status():
    with RawServer(lambda sock, connection: read_request(sock)) as server:
        result, probe = post(server.url)
    assert result.status is None and result.error_phase == "response"
    assert probe.log == ["sent"]


def test_a_quiet_server_hits_the_read_timeout_after_the_request_was_sent():
    release = threading.Event()

    def behaviour(sock, connection):
        read_request(sock)
        release.wait(10)

    with RawServer(behaviour) as server:
        result, probe = post(server.url, read_timeout=0.3)
        release.set()
    assert result.status is None and result.error_phase == "response"
    assert "timed out" in result.error and probe.log == ["sent"]


def test_a_stream_that_goes_quiet_keeps_the_events_it_had():
    release = threading.Event()
    first = event("response.created")

    def behaviour(sock, connection):
        read_request(sock)
        sock.sendall(b"HTTP/1.0 200 OK\r\ncontent-type: text/event-stream\r\n\r\n" + first)
        release.wait(10)

    with RawServer(behaviour) as server:
        result, probe = post(server.url, read_timeout=0.3)
        release.set()
    assert result.status == 200 and result.terminal is None
    assert result.error_phase == "stream" and "timed out" in result.error
    assert result.event_count == 1 and len(probe.events) == 1


def test_a_server_that_answers_and_hangs_up_mid_upload_is_not_an_exception():
    def behaviour(sock, connection):
        sock.recv(1024)
        sock.sendall(b"HTTP/1.0 401 Unauthorized\r\ncontent-length: 2\r\n\r\n{}")

    with RawServer(behaviour) as server:
        result, probe = post(server.url, body=b"x" * (6 * 1024 * 1024))
    # Depending on timing the 401 is read or the reset wins; either way the request
    # counts as sent, nothing is raised and nothing is repeated.
    assert probe.log == ["sent"]
    server.settle()
    assert len(server.connections) == 1
    assert result.status == 401 or (
        result.status is None and result.error_phase in ("send", "response")
    )


def test_proxy_environment_variables_are_ignored(monkeypatch):
    with RawServer(drain) as proxy, FakeCodex(lambda r, h: respond_json(h, 200, b"{}")) as server:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, proxy.url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        result, _ = post(server.url)
        assert result.status == 200 and len(server.requests) == 1
        proxy.settle()
        assert proxy.connections == []


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1/x",
        "http:///nohost",
        "http://user:pass@127.0.0.1/x",
        "http://127.0.0.1/with space",
        "/codex/responses",
    ],
)
def test_a_malformed_url_is_refused_before_any_connection(url):
    probe = Probe()
    with pytest.raises(ValueError):
        post_sse(url, HEADERS, b"{}", connect_timeout=1, read_timeout=1, on_sent=probe.on_sent)
    assert probe.log == []


def test_a_malformed_header_is_refused_before_connecting_and_is_not_echoed():
    secret = "Bearer SECRET-VALUE\r\nInjected: 1"
    with RawServer(drain) as server:
        with pytest.raises(ValueError) as caught:
            post(server.url, headers={**HEADERS, "Authorization": secret})
        assert "SECRET-VALUE" not in str(caught.value) and "SECRET-VALUE" not in repr(caught.value)
        server.settle()
        assert server.connections == []


def test_parse_url_returns_the_target_with_its_query():
    assert parse_url("https://h.example:8443/a/b?c=d") == ("https", "h.example", 8443, "/a/b?c=d")
    assert parse_url("http://127.0.0.1") == ("http", "127.0.0.1", None, "/")


def test_completed_event_helper_is_a_terminal_event():
    # Guards the shared fixture the client tests depend on.
    [done] = parse(completed_event("x"))
    assert done.type == "response.completed" and done.payload["response"]["status"] == "completed"


def test_a_byte_order_mark_does_not_lose_the_first_event_of_a_real_stream():
    first = event("response.created").split(b"\n", 1)[
        1
    ]  # data line only: no `event:` to fall back on
    chunks = [b"\xef\xbb\xbf", first, completed_event("x")]
    with FakeCodex(lambda r, h: respond_sse(h, chunks, delay=0.02)) as server:
        result, probe = post(server.url)
    assert [e.type for e in probe.events] == ["response.created", "response.completed"]
    assert result.terminal.type == "response.completed" and result.error is None


def test_events_are_handed_over_not_retained():
    chunks = ok_stream("a long enough answer")
    with FakeCodex(lambda r, h: respond_sse(h, chunks)) as server:
        result, _ = post(server.url)
    assert not hasattr(result, "events") and result.event_count == 4


def test_an_unterminated_line_ends_the_read_with_a_limit_error(monkeypatch):
    monkeypatch.setattr(transport, "MAX_LINE", 1000)
    chunks = [event("response.created"), b"data: " + b"x" * 600, b"x" * 600, event("error")]
    with FakeCodex(lambda r, h: respond_sse(h, chunks, delay=0.05)) as server:
        result, probe = post(server.url)
    assert result.error_phase == "limit" and "SseLimitExceeded" in result.error
    assert result.terminal is None and [e.type for e in probe.events] == ["response.created"]
    # The bytes received before the limit are still handed on for the raw file.
    assert b"".join(d for _, d in probe.chunks).startswith(chunks[0] + b"data: xxx")
    assert len(server.requests) == 1


def test_repeated_data_lines_end_the_read_with_a_limit_error(monkeypatch):
    monkeypatch.setattr(transport, "MAX_EVENT", 1000)
    chunks = [b"data: " + b"y" * 100 + b"\n"] * 20
    with FakeCodex(lambda r, h: respond_sse(h, chunks)) as server:
        result, probe = post(server.url)
    assert result.error_phase == "limit" and result.terminal is None
    assert probe.events == [] and len(server.requests) == 1


def test_a_stream_over_the_total_limit_ends_the_read(monkeypatch):
    monkeypatch.setattr(transport, "MAX_STREAM", 300)
    chunks = [event("response.output_text.delta", delta="z" * 100)] * 6 + [event("error")]
    with FakeCodex(lambda r, h: respond_sse(h, chunks, delay=0.02)) as server:
        result, probe = post(server.url)
    assert result.error_phase == "limit" and result.terminal is None
    assert 300 < sum(len(d) for _, d in probe.chunks) < sum(len(c) for c in chunks)


def test_the_default_limits_end_a_four_mebibyte_line():
    chunks = [b"data: "] + [b"x" * (1024 * 1024)] * 5
    with FakeCodex(lambda r, h: respond_sse(h, chunks)) as server:
        result, _ = post(server.url)
    assert result.error_phase == "limit" and "4194304" in result.error
